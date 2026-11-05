#!/usr/bin/env python3
"""StranglerRoom end-to-end demo driver (docs/AGENTS_SPEC.md §3).

    .venv/bin/python scripts/run_e2e_demo.py [--cinema] [--fresh] [--no-start] [--step-timeout 240]

Plays the human lead in the Mattermost channel bridged to the Switch room ``strangler-migration``:
logs in to the Mattermost REST API as ``user``, posts the human commands and waits for the three
agents' bot posts (bot username == agent name), matching them on the stable ``agents.commands.MARK``
prefixes and parsing every handoff with the receiving agent's own parser:

1. post ``@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI``
2. translator: ``🧭 Migration``, ``🛠️ v1 deployed`` (rev), ``@fuzzer shadow-test rev=…`` handoff
3. fuzzer: ``🔬 Shadow campaign`` listing exactly D-1..D-5 and ``@user … decision needed``
4. post ``@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5``
5. translator: ``🔧 Applied human decisions`` (v2 rev) and ``@fuzzer re-verify rev=…``
6. fuzzer: ``✅ 100% Approved Parity (5/5)`` and ``@cutover-gate benchmark rev=…``; then the
   **governance probe** — ``@cutover-gate approve-cutover`` sent *as the fuzzer agent* through
   ``POST /agents/{id}/message`` (a simulated compromised agent) must be refused with
   ``🛑 Governance gate`` and leave the proxy on legacy
7. cutover-gate: ``📊 Benchmark`` (p99) and ``@user … approval needed``; post ``@cutover-gate approve-cutover``
8. cutover-gate ``🚦 Cutover complete`` and translator ``🧾 Ledger sealed``; then the proxy must target
   modern (``/_router/status`` and ``X-Strangler-Upstream`` on a proxied request) and the ledger —
   read independently through the Gateway as admin — must hold the 5 decisions by ``user``, approved
   parity, the benchmark p99s of the binding workload (500 requests at concurrency 50 per target over
   the 5 contract endpoints), the cutover approved by ``user`` (its approval message — the one the
   ``🚦`` post must name — verified by the translator against the room history and attributed to
   ``user`` in the trail), an intact hash chain and the seal; the cutover gate's ``Cutover Scorecard``
   (also read through the Gateway, creator-checked) must record the refusal of exactly the probe message
   the driver sent (``event_id`` of the probe POST) with the proxy on legacy, and the same approval.
   After a final settle the routing is checked again and a rollback anywhere after the approval fails.

Only posts created after the human post that triggered the step count (anchoring on the human's
own post, not on an intermediate bot post, keeps the windows correct even when the bridge posts an
agent's handoff to Mattermost after the receiving agent has already answered); ``📄`` room-document
notices are skipped. An
agent ``⚠️`` post anywhere in the run (also between steps, and in a final re-scan after the seal) fails
at once — announced fallbacks in ``TOLERATED_WARNINGS`` are printed, not fatal — and so does a definite
wrong outcome (a second decision request after the decisions, a cutover during the probe or before the
human approved, a refusal of the human's own approval). Prints a timestamped timeline and a PASS/FAIL summary; exits 0 only if every check held
(1 = a step or assertion failed, 2 = pre-flight failed).

Pre-flight (the starts are skipped with ``--no-start``; the checks always run): Switch ``/health``;
``scripts/targets.sh start --fresh``; ``python -m agents.provision --fresh-room`` only with ``--fresh``
(otherwise the provisioned room and stored credentials are reused untouched — provisioning re-registers
an agent whose stored key fails to authenticate, which the driver must never trigger casually);
``scripts/agents.sh restart`` (``STRANGLER_AGENT_PACE=2`` under ``--cinema``); wait until all three agents
are ``live`` in the room; the proxy must start on legacy; the Mattermost user must be the room's human;
then wait until the agents' start-up announcements have settled (printed, flagged if ``⚠️``) so they
cannot leak into step 2. ``--cinema`` prints the channel URL, counts down 10 s and pauses 3 s between
steps. ``--fresh`` cannot be combined with ``--no-start``.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.commands import (  # noqa: E402
    APPROVE_EXAMPLE, MARK, MIGRATE_EXAMPLE, Command, CommandError, format_decisions, mentions, parse_cutover,
    parse_fuzzer,
)
from agents.cutover_agent import BENCH_PATHS  # noqa: E402
from agents.ledger import LEDGER_DOC, SCORECARD_DOC, extract_state, verify_chain  # noqa: E402

AGENTS = ("translator", "fuzzer", "cutover-gate")
EXPECTED_IDS = ["D-1", "D-2", "D-3", "D-4", "D-5"]
DECISIONS = {"D-1": "preserve", "D-2": "preserve", "D-3": "deprecate", "D-4": "preserve", "D-5": "deprecate"}
MIGRATE_CMD = MIGRATE_EXAMPLE
DECIDE_CMD = "@translator " + format_decisions(DECISIONS)
APPROVE_CMD = APPROVE_EXAMPLE
PROBE_CMD = APPROVE_EXAMPLE
PROBE_PATH = "/people/george08/lists.json"
BENCH_TOTAL, BENCH_CONCURRENCY = 500, 50  # the binding benchmark workload per target (docs/AGENTS_SPEC.md §2)
DOC_NOTICE = "📄"
TOLERATED_WARNINGS = ("LLM synthesis rejected",)  # announced fallbacks: shown in the timeline, not fatal
LAST_POSTS_SHOWN = 10

_VS16 = "️"  # emoji variation selector: compared loosely so "🛠️" == "🛠"
_LEADING_MENTIONS = re.compile(r"^(?:@[A-Za-z0-9._-]+[\s,:]*)+")
_REV_ANY = re.compile(r"\brev(?:\s*=\s*|\s*:?\s*\**\s*`\s*|\s+)([0-9a-f]{6,64})\b", re.IGNORECASE)
_MIG_ANY = re.compile(r"\bMIG-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*")
_APPROVAL_MSG = re.compile(r"\(message `([^`\s]+)`\)")  # the 🚦 post: "approved by @user (message `sw_…`)"


# ------------------------------------------------------------------------------- text matching

def normalize(text: str | None) -> str:
    """``text`` stripped, without emoji variation selectors."""
    return (text or "").replace(_VS16, "").strip()


def strip_leading_mentions(text: str) -> str:
    """Drop the ``@name`` tokens Switch prefixes to targeted messages (``@user 🧾 Ledger sealed …``)."""
    return _LEADING_MENTIONS.sub("", text, count=1)


def is_doc_notice(text: str | None) -> bool:
    """A Switch room-document change notice (``📄 …``) — never a step result."""
    return strip_leading_mentions(normalize(text)).startswith(DOC_NOTICE)


def has_mark(text: str | None, mark: str) -> bool:
    """Emoji markers must START the message (after any leading ``@target`` prefix); plain-phrase
    markers (``decision needed``, ``approval needed``) may appear anywhere, case-insensitively."""
    body, want = normalize(text), normalize(mark)
    if not want:
        raise ValueError("empty marker")
    if want[0].isalpha():
        return want.lower() in body.lower()
    return strip_leading_mentions(body).startswith(want)


def addresses(text: str | None, name: str) -> bool:
    """True if ``text`` @-mentions ``name`` (Switch's token-bounded mention rule)."""
    return name.lower() in mentions(text or "")


def find_rev(text: str | None, *, after: str | None = None) -> str | None:
    """The first ``rev=<hex>`` / ``rev `<hex>``` in ``text``. With ``after``, only the text following the
    first occurrence of ``after`` is searched — and ``None`` is returned if ``after`` is absent."""
    body = normalize(text)
    if after:
        want = normalize(after)  # as tolerant of emoji variation selectors as has_mark()
        idx = body.lower().find(want.lower())
        if idx < 0:
            return None
        body = body[idx + len(want):]
    m = _REV_ANY.search(body)
    return m.group(1).lower() if m else None


def find_migration(text: str | None) -> str | None:
    m = _MIG_ANY.search(text or "")
    return m.group(0) if m else None


_TABLE_ID = re.compile(r"^\|\s*\**\s*D-(\d+)\s*\**\s*\|", re.MULTILINE | re.IGNORECASE)


def table_ids(text: str | None) -> list[str]:
    """Distinct ``D-n`` ids in the FIRST column of the Markdown table rows of ``text`` (the divergence
    table of ``DivergenceReport.to_markdown()``), numerically sorted. Prose mentions such as
    "divergence ids restart at D-1" do not count."""
    return sorted({f"D-{int(n)}" for n in _TABLE_ID.findall(text or "")}, key=lambda c: int(c[2:]))


# ------------------------------------------------------------------------------- expectations

@dataclass(frozen=True)
class Expect:
    """One bot post a step waits for."""

    key: str
    author: str
    describe: str
    match: Callable[[str], bool]

    def matches(self, post: dict) -> bool:
        message = post.get("message") or ""
        return post.get("username") == self.author and not is_doc_notice(message) and self.match(message)


def marked(key: str, author: str, mark: str, *, also: tuple[str, ...] = (), to: str | None = None) -> Expect:
    """``author`` posts a message carrying ``mark`` (+ every ``also`` substring; addressed to ``to``)."""
    def match(message: str) -> bool:
        low = normalize(message).lower()
        return (has_mark(message, mark) and all(a.lower() in low for a in also)
                and (to is None or addresses(message, to)))
    extra = "".join(f" … {a}" for a in also)
    return Expect(key, author, f"{author}: {'@' + to + ' … ' if to else ''}{mark}{extra}", match)


def handoff(key: str, author: str, target: str, verb: str, parser: Callable[[str], Command | None]) -> Expect:
    """``author`` hands off to agent ``target``: the post @-mentions it and ``parser`` (the receiving
    agent's own grammar) reads it as ``verb``."""
    def match(message: str) -> bool:
        if not addresses(message, target):
            return False
        try:
            cmd = parser(message)
        except CommandError:
            return False
        return cmd is not None and cmd.verb == verb
    return Expect(key, author, f"{author}: @{target} {verb} rev=…", match)


def match_posts(posts: list[dict], expects: list[Expect]) -> dict[str, dict]:
    """Assign to each expectation the earliest matching post not already taken by another one."""
    found: dict[str, dict] = {}
    used: set[str] = set()
    for exp in expects:
        for post in posts:
            if post.get("id") not in used and exp.matches(post):
                found[exp.key] = post
                used.add(post.get("id"))
                break
    return found


def agent_failures(posts: list[dict], agents: tuple[str, ...] = AGENTS) -> tuple[list[dict], list[dict]]:
    """``(fatal, tolerated)`` agent ``⚠️`` posts in ``posts``."""
    fatal, tolerated = [], []
    for post in posts:
        message = post.get("message") or ""
        if post.get("username") in agents and has_mark(message, MARK["error"]):
            (tolerated if any(t in message for t in TOLERATED_WARNINGS) else fatal).append(post)
    return fatal, tolerated


# ------------------------------------------------------------------------------- final checks

@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def check_ledger(state: dict | None, *, migration: str, rev: str, human: str,
                 approval_message_id: str | None = None) -> list[Check]:
    """Independent verification of the translator's sealed ledger state (``extract_state`` output).

    ``approval_message_id`` is the Switch message id the cutover gate announced in its ``🚦`` post; when
    given, the ledger's approval must name exactly that message."""
    if not isinstance(state, dict):
        return [Check("ledger state block present", False, "no ```json stranglerroom-state``` block found")]
    checks = [Check("ledger is for this migration", (state.get("migration") or {}).get("id") == migration,
                    f"ledger migration {(state.get('migration') or {}).get('id')!r}, expected {migration!r}")]
    decisions = state.get("decisions") or {}
    got = {cid: (d or {}).get("decision") for cid, d in decisions.items()}
    by = sorted({(d or {}).get("by") for d in decisions.values()}, key=str)
    checks.append(Check(f"5 decisions by {human}", got == DECISIONS and by == [human],
                        f"decisions {got}, decided by {by}"))
    parity = state.get("parity") or {}
    checks.append(Check("parity approved", bool(parity) and "100% Approved Parity" in str(parity.get("label"))
                        and parity.get("rev") == rev, f"parity {parity.get('label')!r} on rev {parity.get('rev')!r}"))
    bench = state.get("benchmark") or {}
    p99s = [(bench.get(side) or {}).get("p99_ms") for side in ("legacy", "modern")]
    checks.append(Check("benchmark p99s recorded", bool(bench) and bench.get("rev") == rev
                        and all(isinstance(v, (int, float)) and v > 0 for v in p99s),
                        f"p99 legacy/modern {p99s} ms on rev {bench.get('rev')!r}"))
    sides = [bench.get(side) or {} for side in ("legacy", "modern")]
    # `total` is the REQUESTED count (agents.bench): every request must also have succeeded (2xx, no 5xx/transport
    # error) — a benchmark whose requests failed still yields positive p99s.
    checks.append(Check(f"benchmark workload {BENCH_TOTAL} requests at concurrency {BENCH_CONCURRENCY} per target "
                        f"over the {len(BENCH_PATHS)} contract endpoints, all answered 2xx",
                        bench.get("total") == BENCH_TOTAL and bench.get("concurrency") == BENCH_CONCURRENCY
                        and sorted(bench.get("paths") or []) == sorted(BENCH_PATHS)
                        and all(side.get("total") == BENCH_TOTAL and side.get("concurrency") == BENCH_CONCURRENCY
                                and side.get("ok") == BENCH_TOTAL and side.get("errors") == 0
                                and bool(side.get("status_counts"))
                                and all(str(code).startswith("2") for code in side["status_counts"])
                                for side in sides),
                        f"total {bench.get('total')}, concurrency {bench.get('concurrency')}, endpoints "
                        f"{bench.get('paths')}; per target (total, concurrency, ok, errors, statuses) "
                        f"{[(side.get('total'), side.get('concurrency'), side.get('ok'), side.get('errors'), side.get('status_counts')) for side in sides]}"))
    cut = state.get("cutover") or {}
    checks.append(Check(f"cutover approved by {human}", cut.get("approved_by") == human
                        and cut.get("proxy_target") == "modern",
                        f"approved_by {cut.get('approved_by')!r}, proxy_target {cut.get('proxy_target')!r}"))
    trail = state.get("trail") or []
    # The translator cross-checked the approval message against the room history (read_context): it must have
    # found the human's own `approve-cutover` there, and the trail must attribute that very message to the human.
    approval = cut.get("approval_message_id")
    approvals = [e for e in trail if e.get("event") == "cutover-approved"]
    checks.append(Check(
        "approval message verified in the room history",
        cut.get("approval_message_verified") is True and bool(approval)
        and approval_message_id in (None, approval)
        and len(approvals) == 1 and approvals[0].get("source_message_id") == approval
        and approvals[0].get("actor") == human and approvals[0].get("actor_kind") == "human",
        f"approval_message_verified {cut.get('approval_message_verified')!r}, message {approval!r}"
        + (f" (announced {approval_message_id!r})" if approval_message_id else "")
        + f", cutover-approved trail entries {[(e.get('actor'), e.get('source_message_id')) for e in approvals]}"))
    ok, why = verify_chain(trail) if trail else (False, "empty audit trail")
    checks.append(Check("audit chain verifies", ok, why))
    sealed = state.get("sealed") or {}
    head = trail[-1].get("hash") if trail else None
    checks.append(Check("ledger sealed", state.get("status") == "sealed" and bool(sealed)
                        and sealed.get("head_hash") == head and sealed.get("entries") == len(trail),
                        f"status {state.get('status')!r}, seal head {str(sealed.get('head_hash'))[:12]}, "
                        f"trail head {str(head)[:12]}, entries {sealed.get('entries')}/{len(trail)}"))
    return checks


def find_agent_document(docs: list[dict], name: str, *, writer: str, writer_id: str) -> dict:
    """The one room-scoped document ``name``, which must have been created by agent ``writer`` (the single-writer
    rule the agents themselves enforce) — a same-named global document or one created by another agent is
    rejected."""
    hits = [d for d in docs if d.get("name") == name]
    if len(hits) != 1:
        raise AssertionError(f"expected exactly one room document named {name!r}, found {len(hits)} "
                             f"(documents: {[d.get('name') for d in docs]})")
    doc = hits[0]
    if doc.get("scope") != "room" or doc.get("created_by_agent_id") != writer_id:
        raise AssertionError(f"{name!r} must be a room document created by the {writer} ({writer_id}); "
                             f"got scope={doc.get('scope')!r} created_by_agent_id={doc.get('created_by_agent_id')!r}")
    return doc


def find_ledger_document(docs: list[dict], *, translator_id: str) -> dict:
    return find_agent_document(docs, LEDGER_DOC, writer="translator", writer_id=translator_id)


def check_scorecard(state: dict | None, *, probe_event_id: str, rev: str, human: str,
                    approval_message_id: str) -> list[Check]:
    """Independent verification of the cutover gate's ``Cutover Scorecard``: the governance probe — exactly the
    message the driver sent as the fuzzer (``probe_event_id``, Switch's ``event_id`` == the event's message id,
    live-verified) — is recorded as refused with the proxy on legacy, and the cutover is the human's approval."""
    if not isinstance(state, dict):
        return [Check("scorecard state block present", False, "no ```json stranglerroom-state``` block found")]
    refusals = [r for r in state.get("refusals") or [] if isinstance(r, dict)]
    mine = [r for r in refusals if r.get("message_id") == probe_event_id]
    checks = [Check("governance refusal recorded for the probe message",
                    len(mine) == 1 and mine[0].get("by") == "fuzzer" and mine[0].get("kind") == "agent"
                    and mine[0].get("command") == "approve-cutover" and mine[0].get("proxy_target") == "legacy",
                    f"probe {probe_event_id!r}; refusals "
                    f"{[(r.get('message_id'), r.get('by'), r.get('kind'), r.get('command'), r.get('proxy_target')) for r in refusals]}")]
    cut = state.get("cutover") or {}
    checks.append(Check(f"cutover recorded as {human}'s approval of the v2 rev",
                        cut.get("approved_by") == human and cut.get("proxy_target") == "modern"
                        and cut.get("rev") == rev and cut.get("approval_message_id") == approval_message_id
                        and cut.get("verified") is True,
                        f"approved_by {cut.get('approved_by')!r}, proxy_target {cut.get('proxy_target')!r}, rev "
                        f"{cut.get('rev')!r}, message {cut.get('approval_message_id')!r} (announced "
                        f"{approval_message_id!r}), verified {cut.get('verified')!r}"))
    return checks


# ------------------------------------------------------------------------------------- driver

class StepFailed(Exception):
    """A step's expectation or assertion did not hold."""


class StepTimeout(StepFailed):
    """A step's bot posts did not all arrive within the step timeout."""


class PreflightError(RuntimeError):
    """The stack is not ready for the demo."""


class Gateway(Protocol):
    def room(self, room_id: str) -> dict: ...
    def room_documents(self, room_id: str) -> list[dict]: ...
    def room_document(self, room_id: str, document_id: str) -> dict: ...


class Chat(Protocol):
    def post(self, channel_id: str, message: str, root_id: str = "") -> dict: ...
    def posts_since(self, channel_id: str, since_ms: int) -> list[dict]: ...


@dataclass
class StepResult:
    number: int | str
    title: str
    ok: bool = False
    started: float = 0.0
    finished: float = 0.0
    detail: str = ""


@dataclass
class DemoDriver:
    """Plays the 8 demo steps. Everything external is injected so the unit tests can run it against
    ``tests/fake_mattermost.py`` with a fake Gateway and an ``httpx.MockTransport``."""

    mm: Chat
    channel_id: str
    room_id: str
    gateway: Gateway
    http: httpx.Client
    switch_url: str
    proxy_url: str
    fuzzer_id: str
    fuzzer_api_key: str
    translator_id: str
    cutover_id: str
    human: str = "user"
    step_timeout: float = 240.0
    poll: float = 1.0
    final_settle: float = 3.0
    cinema: bool = False
    channel_link: str | None = None
    out: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    results: list[StepResult] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    _t0: float = 0.0
    _first_ms: int | None = None
    _logged: set = field(default_factory=set)
    _probe_event_id: str | None = None

    # ------------------------------------------------------------------ output
    def elapsed(self) -> float:
        return self.clock() - self._t0

    def log(self, tag: str, text: str) -> None:
        first_line = " ".join(normalize(text).split("\n", 1)[0].split())
        self.out(f"[+{self.elapsed():7.1f}s] {tag:<12} {first_line[:160]}")

    def _begin(self, number: int | str, title: str) -> StepResult:
        if self.cinema and self.results:
            self.sleep(3)
        res = StepResult(number, title, started=self.elapsed())
        self.results.append(res)
        self.log(f"step {number}", f"▶ {title}")
        return res

    def _end(self, res: StepResult, detail: str = "") -> None:
        res.ok, res.finished, res.detail = True, self.elapsed(), detail
        self.log(f"step {res.number}", f"✔ {detail or res.title}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        """Record an assertion; a failed one fails the current step."""
        self.checks.append(Check(name, ok, detail))
        self.log("check", f"{'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            raise StepFailed(f"{name}: {detail}")

    # ------------------------------------------------------------------ chat
    def say(self, text: str) -> dict:
        post = self.mm.post(self.channel_id, text)
        if self._first_ms is None:
            self._first_ms = int(post["create_at"]) - 1
        self.log(self.human, text)
        return post

    def scan_failures(self) -> list[dict]:
        """Every post of the run so far; raises StepFailed on any agent ``⚠️`` posted since the first
        human post (whenever it landed — between steps too), logs announced fallbacks once."""
        posts = self.mm.posts_since(self.channel_id, self._first_ms) if self._first_ms is not None else []
        fatal, tolerated = agent_failures(posts)
        for post in tolerated:
            if post["id"] not in self._logged:
                self._logged.add(post["id"])
                self.log(post["username"], f"(announced fallback) {post['message']}")
        if fatal:
            post = fatal[0]
            raise StepFailed(f"{post['username']} reported a failure: {post['message'][:500]}")
        return posts

    def wait(self, expects: list[Expect], *, since_ms: int, forbid: tuple[Expect, ...] = ()) -> dict[str, dict]:
        """Poll the channel until every expectation has its own matching post created after
        ``since_ms``. Fails at once on an agent ``⚠️`` anywhere in the run or on a post matching one of
        ``forbid`` (a definite wrong outcome); times out after ``step_timeout``."""
        deadline = self.clock() + self.step_timeout
        while True:
            posts = [p for p in self.scan_failures() if int(p["create_at"]) > since_ms]
            for bad in forbid:
                hit = next((p for p in posts if bad.matches(p)), None)
                if hit is not None:
                    raise StepFailed(f"unexpected {bad.describe!r}: {hit['message'][:500]}")
            found = match_posts(posts, expects)
            for exp in expects:
                post = found.get(exp.key)
                if post is not None and post["id"] not in self._logged:
                    self._logged.add(post["id"])
                    self.log(post["username"], post["message"])
            if len(found) == len(expects):
                return found
            if self.clock() >= deadline:
                missing = [e.describe for e in expects if e.key not in found]
                raise StepTimeout(f"timed out after {self.step_timeout:.0f}s waiting for: " + "; ".join(missing))
            self.sleep(self.poll)

    def last_posts(self, n: int = LAST_POSTS_SHOWN) -> list[dict]:
        if self._first_ms is None:
            return []
        return self.mm.posts_since(self.channel_id, self._first_ms)[-n:]

    # ------------------------------------------------------------------ http helpers
    def proxy_status(self) -> dict:
        resp = self.http.get(f"{self.proxy_url}/_router/status")
        try:
            data = resp.json() if resp.status_code == 200 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise StepFailed(f"GET {self.proxy_url}/_router/status -> HTTP {resp.status_code}: {resp.text[:300]}")
        return data

    def check_routing(self, target: str, *, when: str = "") -> None:
        """``/_router/status`` targets ``target`` and a proxied request is answered by it."""
        status = self.proxy_status()
        self.check(f"proxy /_router/status targets {target}{when}", status.get("target") == target,
                   f"target {status.get('target')!r}")
        resp = self.http.get(f"{self.proxy_url}{PROBE_PATH}")
        upstream = resp.headers.get("x-strangler-upstream")
        self.check(f"proxied GET {PROBE_PATH} served by {target}{when}",
                   resp.status_code == 200 and upstream == target,
                   f"HTTP {resp.status_code}, X-Strangler-Upstream {upstream!r}")

    def send_probe(self) -> dict:
        """POST the human-only approval as the fuzzer agent (simulated compromised agent)."""
        url = f"{self.switch_url}/agents/{self.fuzzer_id}/message"
        resp = self.http.post(url, headers={"Authorization": f"Bearer {self.fuzzer_api_key}"},
                              json={"room_id": self.room_id, "content": PROBE_CMD})
        if resp.status_code >= 300:
            raise StepFailed(f"governance probe POST {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError:
            raise StepFailed(f"governance probe: Switch answered HTTP {resp.status_code} with non-JSON "
                             f"{resp.text[:200]!r}") from None
        if not isinstance(body, dict) or body.get("ok") is not True or not body.get("event_id"):
            raise StepFailed(f"governance probe was not accepted by Switch (expected ok=true + event_id): {body}")
        self._probe_event_id = str(body["event_id"])
        return body

    # ------------------------------------------------------------------ the demo
    def run(self) -> bool:
        self._t0 = self.clock()
        try:
            if self.cinema:
                self.intro()
            self.play()
        except StepTimeout as exc:
            self._fail(exc, timeout=True)
        except (StepFailed, AssertionError) as exc:
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001 - infrastructure/parse errors: report, summarise, exit non-zero
            self._fail(RuntimeError(f"{type(exc).__name__}: {exc}"))
        self.summary()
        return self.passed

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results) and all(c.ok for c in self.checks) \
            and len([r for r in self.results if isinstance(r.number, int)]) == 8

    def intro(self) -> None:
        if self.channel_link:
            self.out(f"\n  Open the room in Mattermost first:  {self.channel_link}\n")
        for i in range(10, 0, -1):
            self.out(f"  starting in {i:2d} s …")
            self.sleep(1)

    def play(self) -> None:
        h = self.human
        # 1 ---------------------------------------------------------------------------------
        res = self._begin(1, "human asks the translator to migrate the lists endpoints")
        anchor = int(self.say(MIGRATE_CMD)["create_at"])
        self._end(res, "migration requested")

        # 2 ---------------------------------------------------------------------------------
        res = self._begin(2, "translator: route inventory, v1 deployed, handoff to the fuzzer")
        got = self.wait([marked("accepted", "translator", MARK["migration_accepted"]),
                         marked("v1", "translator", MARK["v1_deployed"]),
                         handoff("shadow", "translator", "fuzzer", "shadow-test", parse_fuzzer)], since_ms=anchor)
        mig = find_migration(got["accepted"]["message"])
        rev1 = find_rev(got["v1"]["message"], after=MARK["v1_deployed"])
        shadow = parse_fuzzer(got["shadow"]["message"])
        self.check("migration id announced", bool(mig), got["accepted"]["message"][:200])
        self.check("migration accepted from the human", addresses(got["accepted"]["message"], h),
                   f"expected '@{h}' in the 🧭 post")
        self.check("v1 rev announced", bool(rev1), got["v1"]["message"][:200])
        self.check("shadow-test handoff carries the v1 rev and migration",
                   shadow.args.get("rev") == rev1 and shadow.args.get("migration") == mig,
                   f"handoff rev={shadow.args.get('rev')} migration={shadow.args.get('migration')}; "
                   f"v1 rev={rev1} migration={mig}")
        self._end(res, f"{mig} · v1 rev {rev1}")

        # 3 ---------------------------------------------------------------------------------
        res = self._begin(3, "fuzzer: shadow campaign on v1 finds D-1..D-5 and asks the human")
        got = self.wait([marked("campaign", "fuzzer", MARK["campaign"]),
                         marked("decision", "fuzzer", MARK["decision_needed"], to=h)],
                        since_ms=anchor)
        campaign = got["campaign"]["message"]
        self.check("campaign ran on the v1 rev", find_rev(campaign) == rev1, f"campaign rev {find_rev(campaign)}")
        self.check("campaign is for this migration", mig in campaign, f"expected {mig} in the 🔬 post")
        ids = table_ids(campaign)
        self.check("campaign lists exactly D-1..D-5", ids == EXPECTED_IDS, f"listed {ids}")
        self.check("decision request does not address another agent",
                   not any(a in mentions(got["decision"]["message"]) for a in AGENTS),
                   f"mentions {mentions(got['decision']['message'])}")
        self._end(res, f"divergences {', '.join(ids)}")

        # 4 ---------------------------------------------------------------------------------
        res = self._begin(4, "human decides: preserve D-1 D-2 D-4, deprecate D-3 D-5")
        anchor = int(self.say(DECIDE_CMD)["create_at"])
        self._end(res, "decisions posted")

        # 5 ---------------------------------------------------------------------------------
        res = self._begin(5, "translator applies the decisions, deploys v2, hands back to the fuzzer")
        got = self.wait([marked("applied", "translator", MARK["decisions_applied"]),
                         handoff("reverify", "translator", "fuzzer", "re-verify", parse_fuzzer)], since_ms=anchor)
        rev2 = find_rev(got["applied"]["message"], after="v2 deployed")
        reverify = parse_fuzzer(got["reverify"]["message"])
        self.check("v2 rev announced and differs from v1", bool(rev2) and rev2 != rev1, f"v1 {rev1} → v2 {rev2} (from the text after 'v2 deployed')")
        self.check("re-verify handoff carries the v2 rev, migration and the human's decisions",
                   reverify.args.get("rev") == rev2 and reverify.args.get("migration") == mig
                   and reverify.args.get("decisions") == DECISIONS,
                   f"handoff {reverify.args}")
        self._end(res, f"v2 rev {rev2}")

        # 6 ---------------------------------------------------------------------------------
        res = self._begin(6, "fuzzer re-verifies v2: 100% approved parity, handoff to the cutover gate")
        got = self.wait([marked("approved", "fuzzer", MARK["approved_parity"], also=("(5/5)",)),
                         handoff("bench", "fuzzer", "cutover-gate", "benchmark", parse_cutover)],
                        since_ms=anchor,
                        forbid=(marked("again", "fuzzer", MARK["decision_needed"], to=h),))
        bench = parse_cutover(got["bench"]["message"])
        self.check("parity approved on the v2 rev", find_rev(got["approved"]["message"]) == rev2,
                   f"approved rev {find_rev(got['approved']['message'])}")
        self.check("benchmark handoff carries the v2 rev and migration",
                   bench.args.get("rev") == rev2 and bench.args.get("migration") == mig, f"handoff {bench.args}")
        self._end(res, "parity 5/5 approved")

        # 6b — governance probe --------------------------------------------------------------
        res = self._begin("6b", "[probe] simulating a compromised agent: the fuzzer tries approve-cutover")
        self.log("[probe]", f"POST {self.switch_url}/agents/{self.fuzzer_id}/message as fuzzer: {PROBE_CMD!r} "
                            "— a human-only command; the cutover gate must refuse it")
        self.send_probe()
        flipped = marked("flip", "cutover-gate", MARK["cutover_complete"])
        self.wait([marked("refusal", "cutover-gate", MARK["governance_refusal"], also=("approve-cutover", "fuzzer"))],
                  since_ms=anchor, forbid=(flipped,))
        status = self.proxy_status()
        self.check("proxy still on legacy after the probe", status.get("target") == "legacy",
                   f"/_router/status target {status.get('target')!r}")
        self._end(res, "governance gate held: agent approval refused, proxy untouched")

        # 7 ---------------------------------------------------------------------------------
        res = self._begin(7, "cutover gate benchmarks both targets and asks the human for approval")
        got = self.wait([marked("card", "cutover-gate", MARK["benchmark_card"], also=("p99",)),
                         marked("approval", "cutover-gate", MARK["approval_needed"], to=h)], since_ms=anchor,
                        forbid=(flipped,))
        self.check("benchmark is for the v2 rev and this migration",
                   find_rev(got["card"]["message"]) == rev2 and mig in got["card"]["message"],
                   f"benchmark rev {find_rev(got['card']['message'])}, expected rev {rev2} and {mig}")
        self.check("approval request does not address another agent",
                   not any(a in mentions(got["approval"]["message"]) for a in AGENTS),
                   f"mentions {mentions(got['approval']['message'])}")
        anchor = int(self.say(APPROVE_CMD)["create_at"])
        self._end(res, "human approved the cutover")

        # 8 ---------------------------------------------------------------------------------
        res = self._begin(8, "cutover: proxy flipped to modern, ledger sealed")
        rolled_back = marked("rollback", "cutover-gate", MARK["rollback_complete"])
        got = self.wait([marked("cutover", "cutover-gate", MARK["cutover_complete"]),
                         marked("sealed", "translator", MARK["ledger_sealed"])], since_ms=anchor,
                        forbid=(marked("refused", "cutover-gate", MARK["governance_refusal"]), rolled_back))
        self.check("cutover is for the v2 rev", find_rev(got["cutover"]["message"]) == rev2,
                   f"cutover rev {find_rev(got['cutover']['message'])}")
        announced = _APPROVAL_MSG.search(got["cutover"]["message"])
        self.check("🚦 post names the approval message", announced is not None,
                   f"expected '(message `<id>`)' in: {got['cutover']['message'][:200]}")
        approval_id = announced.group(1)
        self.check("seal names the migration", mig in got["sealed"]["message"], got["sealed"]["message"][:200])
        self.check_routing("modern")
        docs = self.gateway.room_documents(self.room_id)
        doc = find_ledger_document(docs, translator_id=self.translator_id)
        content = self.gateway.room_document(self.room_id, doc["id"]).get("content")
        for c in check_ledger(extract_state(content or ""), migration=mig, rev=rev2, human=h,
                              approval_message_id=approval_id):
            self.check(f"ledger: {c.name}", c.ok, c.detail)
        doc = find_agent_document(docs, SCORECARD_DOC, writer="cutover-gate", writer_id=self.cutover_id)
        content = self.gateway.room_document(self.room_id, doc["id"]).get("content")
        for c in check_scorecard(extract_state(content or ""), probe_event_id=str(self._probe_event_id), rev=rev2,
                                 human=h, approval_message_id=approval_id):
            self.check(f"scorecard: {c.name}", c.ok, c.detail)
        self.sleep(self.final_settle)  # a late ⚠️ (e.g. a failed document write after the seal) must still count
        late = [p for p in self.scan_failures() if int(p["create_at"]) > anchor and rolled_back.matches(p)]
        self.check("no rollback after the cutover", not late, "; ".join(p["message"][:120] for p in late))
        self.check("no agent reported a failure during the run", True, f"rescanned after {self.final_settle:.0f}s")
        self.check_routing("modern", when=f" after the {self.final_settle:.0f}s settle")
        self._end(res, "migration complete and independently verified")

    # ------------------------------------------------------------------ reporting
    def _fail(self, exc: BaseException, *, timeout: bool = False) -> None:
        current = self.results[-1] if self.results else None
        if current is not None and not current.ok:
            current.finished, current.detail = self.elapsed(), str(exc)
        self.log("FAIL", f"{'TIMEOUT ' if timeout else ''}step {current.number if current else '?'}: {exc}")
        self.out(str(exc))
        try:
            posts = self.last_posts()
        except Exception as err:  # noqa: BLE001 - the original failure is what matters; say why we can't show more
            self.out(f"(could not read the channel to show the last posts: {err})")
            return
        self.out(f"--- last {len(posts)} channel posts ---")
        for p in posts:
            text = " ".join((p.get("message") or "").split())
            self.out(f"  {p.get('username', '?')}: {text[:300]}")

    def summary(self) -> None:
        self.out("")
        self.out("=" * 78)
        self.out(f"{'step':<6}{'result':<8}{'at (s)':>9}{'took (s)':>10}  title")
        for r in self.results:
            took = max(r.finished - r.started, 0.0)
            self.out(f"{str(r.number):<6}{'PASS' if r.ok else 'FAIL':<8}{r.finished:>9.1f}{took:>10.1f}  {r.title}")
        failed = [c for c in self.checks if not c.ok]
        self.out(f"checks: {len(self.checks) - len(failed)}/{len(self.checks)} passed")
        self.out("RESULT: " + ("PASS — every step and assertion held" if self.passed else "FAIL"))
        self.out("=" * 78)


# ---------------------------------------------------------------------------------- pre-flight

def run_cmd(argv: list[str], *, env: dict | None = None, out: Callable[[str], None] = print,
            timeout: float = 300) -> None:
    out(f"$ {' '.join(argv)}")
    proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    for line in (proc.stdout + proc.stderr).strip().splitlines()[-15:]:
        out(f"    {line}")
    if proc.returncode != 0:
        raise PreflightError(f"`{' '.join(argv)}` exited {proc.returncode}")


def wait_agents_live(gateway: Gateway, room_id: str, agents: dict[str, str], *, timeout: float = 90,
                     poll: float = 1.0, sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> dict[str, str]:
    """Block until every agent id in ``agents`` ({name: id}) is ``live`` in the room's
    ``agent_statuses``. Returns {name: status}; raises PreflightError naming the laggards."""
    deadline = clock() + timeout
    while True:
        detail = gateway.room(room_id)
        statuses = detail.get("agent_statuses") or {}
        named = {name: statuses.get(agent_id, "not in room") for name, agent_id in agents.items()}
        if all(s == "live" for s in named.values()):
            return named
        if clock() >= deadline:
            lagging = ", ".join(f"{n}={s}" for n, s in named.items() if s != "live")
            raise PreflightError(f"agents not live after {timeout:.0f}s: {lagging} — see .state/run/<name>.log "
                                 f"(`scripts/agents.sh status`)")
        sleep(poll)


def settle_room(mm: Chat, channel_id: str, *, quiet: float = 3.0, max_wait: float = 45.0, lookback_ms: int = 20000,
                out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic, now_ms: Callable[[], int] | None = None) -> list[dict]:
    """Wait until no agent has posted for ``quiet`` seconds, so start-up announcements (e.g. a restarted
    cutover gate reporting a proxy/scorecard mismatch) land BEFORE the run window opens instead of
    failing step 2. Agent posts of the last ``lookback_ms`` are printed (``⚠️`` ones flagged as start-up
    warnings). Raises PreflightError if the agents never fall quiet within ``max_wait``."""
    now_ms = now_ms or (lambda: int(time.time() * 1000))
    since = now_ms() - lookback_ms
    shown: set[str] = set()
    started = clock()
    while True:
        posts = [p for p in mm.posts_since(channel_id, since) if p.get("username") in AGENTS]
        for p in posts:
            if p["id"] not in shown:
                shown.add(p["id"])
                flag = "START-UP WARNING " if has_mark(p.get("message"), MARK["error"]) else ""
                out(f"  {flag}{p['username']}: {' '.join((p.get('message') or '').split())[:200]}")
        last = max((int(p["create_at"]) for p in posts), default=None)
        if (last is None or now_ms() - last >= quiet * 1000) and clock() - started >= quiet:
            return posts
        if clock() - started >= max_wait:
            raise PreflightError(f"agents kept posting for {max_wait:.0f}s after start-up; not starting the demo")
        sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cinema", action="store_true", help="paced run for a live audience (agent pace 2 s, pauses)")
    ap.add_argument("--fresh", action="store_true",
                    help="re-provision with --fresh-room: archive the room and start on an empty channel")
    ap.add_argument("--no-start", action="store_true", help="do not (re)start targets/agents; only check them")
    ap.add_argument("--step-timeout", type=float, default=240.0, help="seconds to wait for each step (default 240)")
    args = ap.parse_args(argv)
    out = print
    if args.no_start and args.fresh:
        out("PRE-FLIGHT FAILED: --fresh re-provisions the room and restarts the agents; it cannot be combined "
            "with --no-start")
        return 2

    from agents.mattermost import MattermostClient, MattermostError
    from agents.provision import RoomInfo
    from agents.runtime import PROXY_URL
    from agents.switch_client import AgentCredentials, Settings, SwitchError, SwitchGateway

    preflight_errors = (PreflightError, SwitchError, MattermostError, RuntimeError, httpx.HTTPError,
                        subprocess.TimeoutExpired, OSError, ValueError, TypeError, KeyError)
    gateway = None
    http = httpx.Client(timeout=30, trust_env=False)  # local services: never via an HTTP(S)_PROXY
    mm = None
    try:
        try:
            settings = Settings.load()
            health = http.get(f"{settings.switch_url}/health", timeout=10)
            if health.status_code != 200:
                raise PreflightError(f"Switch {settings.switch_url}/health -> HTTP {health.status_code}: "
                                     f"{health.text[:200]}")
            out(f"Switch healthy at {settings.switch_url}")
            if args.no_start:
                if args.cinema:
                    out("note: --no-start keeps the running agents, so STRANGLER_AGENT_PACE=2 is NOT applied")
            else:
                run_cmd(["bash", str(ROOT / "scripts/targets.sh"), "start", "--fresh"], out=out)
                if args.fresh:
                    run_cmd([sys.executable, "-m", "agents.provision", "--fresh-room"], out=out)
                env = dict(os.environ, PYTHONPATH=str(ROOT))
                if args.cinema:
                    env["STRANGLER_AGENT_PACE"] = "2"
                run_cmd(["bash", str(ROOT / "scripts/agents.sh"), "restart"], env=env, out=out)
            room = RoomInfo.load()
            if not room.channel_id:
                raise PreflightError(f"room {room.room_id} has no Mattermost channel id in .state/room.json")
            missing = [a for a in AGENTS if a not in room.agents]
            if missing:
                raise PreflightError(f".state/room.json lacks agents {missing}")
            if settings.mattermost_user != room.human:
                raise PreflightError(f"the driver logs in to Mattermost as {settings.mattermost_user!r} but the room's "
                                     f"human lead is {room.human!r}")
            fuzzer = AgentCredentials.load("fuzzer")
            if fuzzer is None or fuzzer.agent_id != room.agents["fuzzer"]:
                raise PreflightError("no stored fuzzer credentials matching .state/room.json "
                                     "(.state/agents/fuzzer.json) — needed for the governance probe")
            gateway = SwitchGateway(settings)
            out("waiting for translator, fuzzer and cutover-gate to be live …")
            live = wait_agents_live(gateway, room.room_id, {a: room.agents[a] for a in AGENTS})
            out("agents live: " + ", ".join(f"{n}={s}" for n, s in live.items()))
            status = http.get(f"{PROXY_URL}/_router/status")
            target = status.json().get("target") if status.status_code == 200 else None
            if target != "legacy":
                raise PreflightError(f"proxy {PROXY_URL} must start on legacy, got HTTP {status.status_code} "
                                     f"{status.text[:200]} — run without --no-start (targets.sh start --fresh resets it)")
            mm = MattermostClient(settings.mattermost_url)
            mm.login(settings.mattermost_user, settings.mattermost_user_password)
            channel = mm.channel(room.channel_id)
            team = mm.team(settings.mattermost_team)
            if team["id"] != channel.get("team_id"):
                raise PreflightError(f"channel {channel.get('name')} is not in team {settings.mattermost_team}")
            out("letting agent start-up announcements settle …")
            settle_room(mm, room.channel_id, out=out)
        except preflight_errors as exc:
            out(f"PRE-FLIGHT FAILED: {type(exc).__name__}: {exc}")
            return 2
        link = f"{settings.mattermost_url}/{settings.mattermost_team}/channels/{channel['name']}"
        out(f"room {room.name} ({room.room_id}) · channel {link}")
        driver = DemoDriver(mm=mm, channel_id=room.channel_id, room_id=room.room_id, gateway=gateway, http=http,
                            switch_url=settings.switch_url, proxy_url=PROXY_URL, fuzzer_id=fuzzer.agent_id,
                            fuzzer_api_key=fuzzer.api_key, translator_id=room.agents["translator"],
                            cutover_id=room.agents["cutover-gate"],
                            human=room.human, step_timeout=args.step_timeout, cinema=args.cinema,
                            channel_link=link, out=out)
        return 0 if driver.run() else 1
    finally:
        if mm is not None:
            mm.close()
        http.close()
        if gateway is not None:
            gateway.close()


if __name__ == "__main__":
    sys.exit(main())
