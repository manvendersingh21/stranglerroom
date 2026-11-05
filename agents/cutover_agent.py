"""``@cutover-gate`` — benchmarks legacy vs modern and flips the strangler proxy, only on human approval.

Run with ``python -m agents.cutover_agent`` (docs/AGENTS_SPEC.md §2).

Pipeline position::

    fuzzer ──benchmark rev=… migration=…──▶ cutover-gate ──(card + approval request)──▶ human
    human  ──approve-cutover──▶ cutover-gate ──flip :9000 → modern, verify──▶ translator (record-cutover)

Commands (parsed by :func:`agents.commands.parse_cutover`):

* ``benchmark rev=<rev> migration=<id>`` — from the fuzzer agent or a human. Preconditions: the fuzzer's
  ``Parity Audit Matrix`` (created by the fuzzer agent) has a latest run on ``rev`` with approved parity, and the
  modern service's ``/_meta`` reports ``rev``. Legacy, then modern, are load-tested **sequentially** with
  :func:`agents.bench.run_benchmark` over contract GET endpoints that answer identically on both targets.
  The result is recorded in the ``Cutover Scorecard`` room document (this agent is its only writer), posted as a
  ``📊 Benchmark`` card, and the human is asked for approval.
* ``approve-cutover`` — *human-only*. Re-checks every precondition against live state, flips the proxy to modern
  with the evidence (parity label, rev, p99s, approval message id), verifies through the proxy, records the cutover
  and hands off to the translator (``record-cutover``). If verification fails the proxy is put back on legacy and
  the failure is announced.
* ``rollback`` — *human-only*: flips the proxy back to legacy, verifies, records.
* ``status`` / ``help`` — anyone.

Governance: a human-only command from any non-human sender is answered with ``🛑 Governance gate``, recorded in
the scorecard, and changes nothing. Mention hygiene is enforced in code: this agent refuses to post any body that
``@``-mentions an agent outside a deliberate :meth:`handoff`.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from agents import bench, proxy_control
from agents.commands import (
    MARK, Command, CommandError, mentions, parse_cutover, record_cutover, strip_mentions,
)
from agents.ledger import (
    DOC_META, MATRIX_DOC, SCORECARD_DOC, digest, embed_state, extract_state, latest_benchmark, latest_run,
    new_scorecard, now_iso, render_scorecard,
)
from agents.runtime import LEGACY_URL, MODERN_URL, PROXY_URL, RoomAgent, Sender, main_for
from agents.locks import production_lock
from agents.switch_client import Event
from targets.common.router_token import resolve_router_token

#: GET contract endpoints (docs/SPEC.md §2) whose behaviour is identical on legacy and modern once the demo
#: decisions are applied (``.json`` alias and loose query preserved, positive quantity, existing resources only —
#: so neither the deprecated D-3 nor D-5 behaviour is exercised). Each is re-checked live before benchmarking.
BENCH_PATHS: tuple[str, ...] = (
    "/people/george08/lists",
    "/people/george08/lists.json?details=1",
    "/people/george08/lists/OL1L/seeds",
    "/people/openlibrary/lists/OL10L/editions",
    "/books/OL1M/quote?quantity=3",
)
#: Request used to verify routing through the proxy after a flip.
VERIFY_PATH = "/people/george08/lists.json"
HUMAN_ONLY = frozenset({"approve-cutover", "rollback"})
MAX_BENCHMARKS_KEPT = 12
MAX_REFUSALS_KEPT = 50
MAX_HISTORY_KEPT = 50
_STATE_MARKER = "\n\n---\n_Machine-readable state"

HELP = """\
**cutover-gate** — load benchmark + strangler-proxy cutover, gated on the human lead.

| Command | Who | What |
|---|---|---|
| `benchmark rev=<rev> migration=<id>` | fuzzer agent or human | legacy vs modern p50/p95/p99 card (needs approved parity on `rev`) |
| `approve-cutover` | **human only** | flip the proxy to modern, verify, hand the record to the translator |
| `rollback` | **human only** | flip the proxy back to legacy |
| `status` / `help` | anyone | current proxy target, latest benchmark, cutover record |

State lives in the room document “Cutover Scorecard” (written only by this agent)."""


class PreconditionFailed(Exception):
    """A command cannot run yet; the message explains why (posted to the room, nothing changed)."""


def _port(url: str) -> str:
    """``http://127.0.0.1:9002`` -> ``:9002`` (falls back to the URL itself)."""
    try:
        port = httpx.URL(url).port
    except Exception:  # noqa: BLE001 - only used for display
        return url
    return f":{port}" if port else url


class CutoverAgent(RoomAgent):
    name = "cutover-gate"

    def __init__(self, *, legacy_url: str | None = None, modern_url: str | None = None,
                 proxy_url: str | None = None, router_token: str | None = None,
                 bench_total: int = 500, bench_concurrency: int = 50,
                 bench_paths: tuple[str, ...] = BENCH_PATHS, http_timeout: float = 10.0,
                 production_lock_timeout: float = 120.0, **kwargs: Any):
        super().__init__(**kwargs)
        self.production_lock_timeout = production_lock_timeout
        self.legacy_url = (legacy_url or LEGACY_URL).rstrip("/")
        self.modern_url = (modern_url or MODERN_URL).rstrip("/")
        self.proxy_url = (proxy_url or PROXY_URL).rstrip("/")
        # The shared resolver applies the proxy's exact rule (trimmed env token, else the per-deployment file).
        self._router_token = router_token or resolve_router_token()
        if bench_total <= 0 or bench_concurrency <= 0:
            raise ValueError("bench_total and bench_concurrency must be positive")
        self.bench_total = bench_total
        self.bench_concurrency = bench_concurrency
        self.bench_paths = tuple(bench_paths)
        self.http_timeout = http_timeout
        self.scorecard: dict = _fresh_scorecard()

    # ================================================================== lifecycle
    def on_connected(self) -> None:
        """Rehydrate the scorecard from the room and cross-check it against the live proxy."""
        try:
            self.rehydrate()
        except Exception as exc:  # noqa: BLE001 - a broken document must not keep the gate offline
            self.log.exception("rehydration failed")
            self.scorecard = _fresh_scorecard()
            self.say(f"{MARK['error']} cutover-gate could not rehydrate “{SCORECARD_DOC}”: "
                     f"`{_defuse(f'{type(exc).__name__}: {exc}')[:300]}` — starting a fresh scorecard.")
        cut = self.scorecard.get("cutover")
        recorded = cut.get("proxy_target") if cut else "legacy"
        try:
            live = proxy_control.status(self.proxy_url, timeout=self.http_timeout)["target"]
        except RuntimeError as exc:
            self.say(f"{MARK['error']} cutover-gate restarted but cannot reach the proxy {_port(self.proxy_url)}: "
                     f"{_defuse(str(exc))}")
            return
        if live != recorded:
            what = (f"records the proxy on `{recorded}`" if cut else "records no active cutover")
            self.say(f"{MARK['error']} cutover-gate restarted: the Cutover Scorecard {what} but "
                     f"{_port(self.proxy_url)} reports `{live}` — the router was changed outside the gate. "
                     f"Ask for `status`; a human may `approve-cutover` or `rollback`.")

    def rehydrate(self) -> None:
        """Load the scorecard state from the ``Cutover Scorecard`` room document, if this agent created one."""
        doc = self._find_doc(SCORECARD_DOC)
        if doc is None:
            self.log.info("no %s yet — starting with an empty scorecard", SCORECARD_DOC)
            self.scorecard = _fresh_scorecard()
            return
        if doc.get("created_by_agent_id") != self.switch.agent_id:
            self.say(f"{MARK['error']} a room document named “{SCORECARD_DOC}” exists but was not created by "
                     f"cutover-gate (creator `{_defuse(str(doc.get('created_by_agent_id')))}`) — refusing to trust "
                     f"it; scorecard writes will fail until it is removed.")
            self.scorecard = _fresh_scorecard()
            return
        content = self.switch.load_documents([doc["id"]])[0]["content"]
        try:
            state = extract_state(content)
        except ValueError:
            state = None
        if not isinstance(state, dict) or state.get("kind") != "scorecard":
            self.say(f"{MARK['error']} could not rehydrate “{SCORECARD_DOC}” (no valid state block) — starting a "
                     f"fresh scorecard; the next write replaces the unreadable content.")
            self.scorecard = _fresh_scorecard()
            return
        for key, default in (("benchmarks", []), ("rollbacks", []), ("refusals", []), ("cutover_history", []),
                             ("cutover", None)):
            state.setdefault(key, default)
        if not all(isinstance(state[k], list) for k in ("benchmarks", "rollbacks", "refusals", "cutover_history")) \
                or not isinstance(state["cutover"], (dict, type(None))):
            self.say(f"{MARK['error']} “{SCORECARD_DOC}” has a malformed state block — starting a fresh scorecard.")
            self.scorecard = _fresh_scorecard()
            return
        self.scorecard = state
        self.log.info("rehydrated %s: %d benchmark(s), cutover=%s", SCORECARD_DOC, len(state["benchmarks"]),
                      (state.get("cutover") or {}).get("proxy_target"))

    # ================================================================== mention hygiene
    def _agent_names(self) -> set[str]:
        names = {n.lower() for n in self.room.agents} | {self.name}
        try:
            names |= {p["name"].lower() for p in self.switch.participants() if p.get("type") == "agent"}
        except Exception as exc:  # noqa: BLE001 - the provisioned list still protects the known agents
            self.log.warning("participant list unavailable for the hygiene check: %s", exc)
        return names

    def _check_hygiene(self, body: str) -> None:
        bad = sorted(set(mentions(body)) & self._agent_names())
        if bad:
            raise RuntimeError(f"mention hygiene: refusing to post a body that addresses agent(s) {bad}")

    def say(self, body: str) -> str:
        self._check_hygiene(body)
        return super().say(body)

    def handoff(self, target: str, body: str) -> dict:
        self._check_hygiene(body)  # Switch adds the single deliberate "@target" itself
        return super().handoff(target, body)

    # ================================================================== dispatch
    def handle(self, event: Event, sender: Sender) -> None:
        try:
            cmd = parse_cutover(event.body)
        except CommandError as exc:
            self.say(f"{MARK['error']} {_defuse(str(exc))}")
            return
        if cmd is None:
            if sender.is_human:
                # agents.commands only honours a verb in first position; say why if a governance verb
                # appeared later ("what would rollback do?") instead of silently ignoring it.
                if re.search(r"\b(approve-cutover|approve\s+cutover|rollback)\b", strip_mentions(event.body).lower()):
                    self.say(f"{MARK['error']} not acted on: `approve-cutover` and `rollback` change production "
                             f"routing, so they must be the first word of the message (e.g. reply to the cutover "
                             f"gate with exactly `approve-cutover`). Nothing changed.")
                else:
                    self.say("I did not recognise a command. Reply to the cutover gate with `help` for the list.")
            return  # never chat back at other agents' unrelated messages
        if cmd.verb in HUMAN_ONLY and not sender.is_human:
            self._guarded(self._refuse, cmd, sender, event)
            return
        handler = {"help": lambda *_: self.say(HELP), "status": lambda *_: self.say(self._status_text()),
                   "benchmark": self._benchmark, "approve-cutover": self._approve, "rollback": self._rollback}
        if cmd.verb in ("approve-cutover", "rollback"):
            # Mutually exclusive with the translator's deploys (agents/locks.py): a flip can never land on a build
            # that is being replaced, nor a build be written under a flip in progress.
            try:
                with production_lock(timeout=self.production_lock_timeout):
                    self._guarded(handler[cmd.verb], cmd, sender, event)
            except TimeoutError as exc:
                self.say(f"{MARK['error']} `{cmd.verb}` not done — {_defuse(str(exc))}; the translator is deploying a "
                         f"build. Nothing changed; post it again once the new build has been verified.")
            return
        self._guarded(handler[cmd.verb], cmd, sender, event)

    def _guarded(self, fn, cmd: Command, sender: Sender, event: Event) -> None:
        """Run a command; every failure is reported through the hygiene-checked ``say`` (never quoting the
        sender's text, which could carry an ``@agent`` mention)."""
        try:
            fn(cmd, sender, event)
        except PreconditionFailed as exc:
            self.say(f"{MARK['error']} `{cmd.verb}` not done — {_defuse(str(exc))}")
        except Exception as exc:  # noqa: BLE001 - the room must hear about every failure
            self.log.exception("%s failed", cmd.verb)
            self.say(f"{MARK['error']} `{cmd.verb}` failed: `{_defuse(f'{type(exc).__name__}: {exc}')[:400]}` "
                     f"— see `.state/run/{self.name}.log`.")

    # ================================================================== governance
    def _refuse(self, cmd: Command, sender: Sender, event: Event) -> None:
        try:
            target = proxy_control.status(self.proxy_url, timeout=self.http_timeout)["target"]
        except RuntimeError as exc:
            self.log.warning("proxy status unavailable during refusal: %s", exc)
            target = "unknown (proxy unreachable)"
        who = {"agent": "a room agent", "human": "a human", "guest": "a human who is not the room's lead"}.get(
            sender.kind, "not a known room member")
        reason = (f"`{cmd.verb}` is a human-only command; the sender `{_defuse(sender.name)}` is {who}, "
                  f"not the human lead")
        # Announce first: a failing document write must never swallow the refusal itself.
        self.say(f"{MARK['governance_refusal']}: refused — {reason}. Nothing changed: the proxy "
                 f"{_port(self.proxy_url)} stays on `{target}`. Only {self.room.human} (human) can approve or roll "
                 f"back a cutover. The refusal is recorded in the Cutover Scorecard.")
        self.scorecard["refusals"].append(
            {"at": now_iso(), "by": sender.name, "kind": sender.kind, "command": cmd.verb,
             "message_id": event.message_id, "reason": reason, "proxy_target": target})
        del self.scorecard["refusals"][:-MAX_REFUSALS_KEPT]
        try:
            self._save()
        except Exception as exc:  # noqa: BLE001
            self.log.exception("could not record the refusal")
            self.say(f"{MARK['error']} the governance refusal above could not be written to the Cutover "
                     f"Scorecard: `{_defuse(str(exc))[:300]}`")

    # ================================================================== benchmark
    def _benchmark(self, cmd: Command, sender: Sender, event: Event) -> None:
        if not (sender.is_human or (sender.is_agent and sender.name.lower() == "fuzzer")):
            raise PreconditionFailed(f"benchmarks are requested by the fuzzer agent or the human lead, not by "
                                     f"`{sender.name}` ({sender.kind}).")
        rev, mig = cmd.args.get("rev"), cmd.args.get("migration")
        if not rev or not mig:
            raise PreconditionFailed("usage: `benchmark rev=<rev> migration=<MIG-…>` (both are required).")
        done = next((b for b in self.scorecard["benchmarks"]
                     if event.message_id and b.get("request_message_id") == event.message_id), None)
        if done:  # a redelivered request: re-running would move the approval floor under the human's feet
            self.say(f"ℹ️ benchmark request `{_defuse(str(event.message_id))}` was already handled (rev "
                     f"`{done.get('rev')}`, finished {done.get('at')}) — not re-run; the card above stands.")
            return
        cut = self.scorecard.get("cutover")
        if cut and cut.get("proxy_target") != "legacy":
            # A newer benchmark would supersede the evidence the human approved and so block the translator's
            # seal (an approval must postdate the latest benchmark). Production changes need a rollback first.
            raise PreconditionFailed(f"production is cut over ({_port(self.proxy_url)} → "
                                     f"`{_defuse(str(cut.get('proxy_target')))}`, rev "
                                     f"`{_defuse(str(cut.get('rev')))}`); benchmarking now would supersede the evidence "
                                     f"the human approved. Roll back first (reply to the cutover gate with `rollback`).")
        run = self._approved_run(rev, mig)
        self._require_modern_rev(rev)
        paths = self._identical_paths()

        with self.working():
            legacy = asyncio.run(bench.run_benchmark(self.legacy_url, list(paths), total=self.bench_total,
                                                     concurrency=self.bench_concurrency,
                                                     timeout=self.http_timeout))
            modern = asyncio.run(bench.run_benchmark(self.modern_url, list(paths), total=self.bench_total,
                                                     concurrency=self.bench_concurrency,
                                                     timeout=self.http_timeout))
        cmp_ = bench.compare(legacy, modern)
        verdict = self._verdict(legacy, modern, cmp_)
        report = run["report"]
        record = {
            "rev": rev, "migration": mig, "at": now_iso(), "finished_at_ms": int(time.time() * 1000),
            "total": self.bench_total,
            "concurrency": self.bench_concurrency, "paths": list(paths),
            "legacy": legacy.to_dict(), "modern": modern.to_dict(), "compare": cmp_, "verdict": verdict,
            "requested_by": sender.name, "requested_by_kind": sender.kind, "request_message_id": event.message_id,
            "parity": {"label": report["parity_label"], "run": run["run"], "rev": run["rev"],
                       "evidence": digest(run)},
        }
        self.scorecard["benchmarks"].append(record)
        del self.scorecard["benchmarks"][:-MAX_BENCHMARKS_KEPT]
        self._save()
        self.say(f"{MARK['benchmark_card']} — rev `{rev}` (migration {mig}) · {self.bench_total} requests per "
                 f"target at concurrency {self.bench_concurrency}, sequential (legacy first), round-robin over "
                 f"{len(paths)} contract endpoints · parity: {report['parity_label']}\n\n"
                 f"{bench.render_card(legacy, modern)}\n\n**Verdict:** {verdict}")
        self.ask_human(f"{MARK['approval_needed']}: reply to the cutover gate with `approve-cutover` to flip "
                       f"{_port(self.proxy_url)} from legacy {_port(self.legacy_url)} to modern "
                       f"{_port(self.modern_url)} (or `rollback` later).")

    @staticmethod
    def _verdict(legacy: bench.BenchResult, modern: bench.BenchResult, cmp_: dict) -> str:
        delta = cmp_["p99_delta_pct"]
        if delta < 0:
            speed = f"modern is faster: p99 {modern.p99_ms:.1f} ms vs {legacy.p99_ms:.1f} ms ({delta:+.1f}%)"
        elif delta > 0:
            speed = f"modern is slower: p99 {modern.p99_ms:.1f} ms vs {legacy.p99_ms:.1f} ms ({delta:+.1f}%)"
        else:
            speed = f"p99 is unchanged at {modern.p99_ms:.1f} ms"
        text = (f"{speed}; throughput {modern.throughput_rps:.1f} vs {legacy.throughput_rps:.1f} req/s "
                f"({cmp_['throughput_delta_pct']:+.1f}%); errors legacy {legacy.errors}/{legacy.total}, "
                f"modern {modern.errors}/{modern.total}.")
        if modern.errors:
            text += f" ⚠️ modern returned {modern.errors} error(s) (5xx/transport) — investigate before approving."
        return text

    # ================================================================== approve / rollback
    def _approve(self, cmd: Command, sender: Sender, event: Event) -> None:
        bm = latest_benchmark(self.scorecard)
        if bm is None:
            raise PreconditionFailed("no benchmark has run yet — the fuzzer requests one after approved parity.")
        rev, mig = bm["rev"], bm["migration"]
        self._require_modern_rev(rev, hint="re-run the benchmark for the current build first")
        run = self._approved_run(rev, mig)
        router = proxy_control.status(self.proxy_url, timeout=self.http_timeout)
        previous = router["target"]
        cut = self.scorecard.get("cutover")
        if (previous == "modern" and cut and cut.get("proxy_target") == "modern"
                and cut.get("rev") == rev and cut.get("migration") == mig):
            self.say(f"ℹ️ Cutover for rev `{rev}` is already complete (approved by {_defuse(cut['approved_by'])} at "
                     f"{cut['approved_at']}); the proxy {_port(self.proxy_url)} already serves modern. No second "
                     f"flip — re-sending the record to the translator in case it was missed.")
            self.handoff("translator", record_cutover(rev, mig))
            return
        replay = self._replayed_approval(event)
        if replay:
            self.say(f"ℹ️ approval message `{_defuse(str(event.message_id))}` was already handled ({replay}) — a "
                     f"replayed approval never moves the proxy again; it stays on `{previous}`. Post a new "
                     f"`approve-cutover` to approve again.")
            return
        stale = self._predates_last_flip(event, router)
        if stale:
            self.say(f"ℹ️ approval message `{_defuse(str(event.message_id))}` {stale} — a stale or replayed approval "
                     f"never moves the proxy; it stays on `{previous}`. Post a new `approve-cutover` to approve again.")
            return
        self._require_approval_after_benchmark(event, bm)

        label = run["report"]["parity_label"]
        lg, md = bm["legacy"], bm["modern"]
        evidence = (f"{label} · rev={rev} · migration={mig} · p99 legacy {lg['p99_ms']:.1f} ms → modern "
                    f"{md['p99_ms']:.1f} ms · benchmark {bm['at']} · approval message {event.message_id}")
        try:
            state = proxy_control.flip(self.proxy_url, self._router_token, "modern", sender.name, evidence,
                                       timeout=self.http_timeout)
        except RuntimeError as exc:  # the router may or may not have applied it (e.g. a read timeout)
            self._abort_cutover(f"the flip request failed ({exc})", previous, sender, event, rev=rev, mig=mig)
            return
        ok, why = self._verify("modern", self.modern_url, expected_rev=rev)
        if not ok:
            self._abort_cutover(f"post-flip verification failed ({why})", previous, sender, event, rev=rev, mig=mig)
            return
        record = {
            "rev": rev, "migration": mig, "approved_by": sender.name, "approved_by_kind": sender.kind,
            "approved_at": now_iso(), "approval_message_id": event.message_id,
            # Switch's own timestamp of the approval message (its sent_at, epoch ms): lets the translator read
            # the exact history window that holds it instead of scanning the whole room.
            "approval_timestamp_ms": event.payload.get("timestamp"), "proxy_target": "modern",
            "upstream_url": self.modern_url, "flipped_at": state.get("flipped_at"), "verified": True,
            "verification": why, "evidence": evidence, "parity_evidence": digest(run),
            "benchmark_evidence": digest(bm),
        }
        prior_cutover = self.scorecard.get("cutover")
        self.scorecard["cutover"] = record
        self._append_history(dict(record))
        try:
            self._save()
        except Exception as exc:  # noqa: BLE001 - never leave traffic on modern without a record
            self.scorecard["cutover"] = prior_cutover
            self.scorecard["cutover_history"].pop()
            self._abort_cutover(f"the cutover could not be recorded in the Cutover Scorecard ({exc})", previous,
                                sender, event, save=False, rev=rev, mig=mig)
            return
        self.say(f"{MARK['cutover_complete']}: {_port(self.proxy_url)} now serves the modern FastAPI service "
                 f"({_port(self.modern_url)}) — approved by @{sender.name} (message `{event.message_id}`). "
                 f"Evidence: {_defuse(label)} on rev `{rev}`; p99 {lg['p99_ms']:.1f} ms → {md['p99_ms']:.1f} ms. "
                 f"Verified through the proxy: {_defuse(why)}. `rollback` (human only) flips back to legacy.")
        self.handoff("translator", record_cutover(rev, mig))

    def _abort_cutover(self, why: str, previous: str, sender: Sender, event: Event, *, save: bool = True,
                       rev: str | None = None, mig: str | None = None) -> None:
        """A cutover attempt failed after the flip may have happened: put the proxy back on ``previous`` (the
        target that was live before), make the scorecard match, and say so. Nothing is handed to the translator.

        If the revert itself fails the routing is UNKNOWN (possibly modern): the scorecard then records a
        cutover with ``proxy_target: "unknown"``, which keeps the translator from rewriting the module that
        may be live, until a human's ``rollback`` (or a new ``approve-cutover``) settles it."""
        note = f"automatic revert to {previous}: {why}"
        try:
            proxy_control.flip(self.proxy_url, self._router_token, previous, self.name, note[:500],
                               timeout=self.http_timeout)
            reverted = f"the proxy was put back on `{previous}`"
            if previous == "legacy":
                self.scorecard["cutover"] = None
        except RuntimeError as exc:
            reverted = (f"AND the automatic revert to `{previous}` failed ({exc}) — the proxy may be serving "
                        f"modern without a record; a human must `rollback` or `approve-cutover` again")
            self.scorecard["cutover"] = {
                "rev": rev, "migration": mig, "approved_by": None, "approved_by_kind": None, "approved_at": None,
                "approval_message_id": event.message_id, "approval_timestamp_ms": event.payload.get("timestamp"),
                "proxy_target": "unknown", "upstream_url": "",
                "flipped_at": None, "verified": False,
                "verification": f"routing unknown after a failed cutover: {why}; revert failed: {exc}"}
        self.scorecard["rollbacks"].append(
            {"at": now_iso(), "by": self.name, "note": f"{note}; approval by {sender.name} "
             f"(message {event.message_id}) not applied", "message_id": event.message_id})
        del self.scorecard["rollbacks"][:-MAX_HISTORY_KEPT]
        save_note = ""
        if save:
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                save_note = f" The scorecard could not be updated either: {exc}."
        self.say(_defuse(f"{MARK['error']} cutover NOT completed: {why}; {reverted}. No record was handed to the "
                         f"translator.{save_note}"))

    def _rollback(self, cmd: Command, sender: Sender, event: Event) -> None:
        current = proxy_control.status(self.proxy_url, timeout=self.http_timeout)
        if event.message_id and any(r.get("message_id") == event.message_id and r.get("by") == sender.name
                                    for r in self.scorecard["rollbacks"]):
            self.say(f"ℹ️ rollback message `{_defuse(str(event.message_id))}` was already handled — a replayed "
                     f"rollback never moves the proxy again; it stays on `{current.get('target')}`. Post a new "
                     f"`rollback` to roll back again.")
            return
        cut = self.scorecard.get("cutover") or {}
        if current.get("target") == "legacy":
            if cut:  # the router was moved back outside the gate: close the stale record
                self.scorecard["cutover"] = None
                self._mark_history_rolled_back(sender, note="proxy found already on legacy (changed outside the gate)")
                self.scorecard["rollbacks"].append(
                    {"at": now_iso(), "by": sender.name, "message_id": event.message_id, "rev": cut.get("rev"),
                     "migration": cut.get("migration"), "verified": True,
                     "note": "proxy was already on legacy (changed outside the gate); stale cutover record closed"})
                del self.scorecard["rollbacks"][:-MAX_HISTORY_KEPT]
                self._save()
                self.say(f"The proxy {_port(self.proxy_url)} already serves legacy ({_port(self.legacy_url)}) — it "
                         f"was changed outside the gate; the stale cutover record for rev `{cut.get('rev')}` is now "
                         f"closed in the Cutover Scorecard.")
                return
            self.say(f"The proxy {_port(self.proxy_url)} already serves legacy ({_port(self.legacy_url)}) — "
                     f"nothing to roll back.")
            return
        stale = self._predates_last_flip(event, current)
        if stale:
            self.say(f"ℹ️ rollback message `{_defuse(str(event.message_id))}` {stale} — a stale or replayed rollback "
                     f"never moves the proxy; it stays on `{current.get('target')}`. Post a new `rollback` to roll "
                     f"back again.")
            return
        note = f"rollback requested by {sender.name} (message {event.message_id})"
        state = proxy_control.flip(self.proxy_url, self._router_token, "legacy", sender.name, note,
                                   timeout=self.http_timeout)
        ok, why = self._verify("legacy", self.legacy_url)
        self.scorecard["rollbacks"].append(
            {"at": now_iso(), "by": sender.name, "note": f"proxy → legacy; verified: {'yes' if ok else 'NO'} — {why}",
             "message_id": event.message_id, "rev": cut.get("rev"), "migration": cut.get("migration"),
             "flipped_at": state.get("flipped_at"), "verified": ok})
        del self.scorecard["rollbacks"][:-MAX_HISTORY_KEPT]
        if cut:
            self._mark_history_rolled_back(sender)
        self.scorecard["cutover"] = None  # no active cutover; the approval stays in cutover_history
        self._save()
        if not ok:
            self.say(_defuse(f"{MARK['error']} rollback flipped {_port(self.proxy_url)} to legacy but verification "
                             f"failed: {why}. Check the proxy now."))
            return
        self.say(f"{MARK['rollback_complete']}: {_port(self.proxy_url)} serves the legacy web.py service "
                 f"({_port(self.legacy_url)}) again — requested by @{sender.name} (message `{event.message_id}`). "
                 f"Verified: {_defuse(why)}. Recorded in the Cutover Scorecard.")

    @staticmethod
    def _predates_last_flip(event: Event, router: dict) -> str | None:
        """Why ``event`` is older than the router's latest flip (so it was written for a routing state that no
        longer exists — e.g. a redelivered rollback whose id fell out of the capped history), else None. Uses the
        router's own ``flipped_at``, so it holds even when the Cutover Scorecard was lost."""
        flipped = router.get("flipped_at")
        if not flipped:
            return None  # never flipped: there is no newer routing state to protect
        ts = event.payload.get("timestamp")
        if not isinstance(ts, (int, float)):
            return f"carries no timestamp ({ts!r}), so it cannot be ordered after the latest flip ({flipped})"
        # compare at the message's millisecond precision: a command in the flip's own millisecond is not "older"
        if int(ts) < int(datetime.fromisoformat(str(flipped)).timestamp() * 1000):
            sent = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return f"was sent at {sent}, before the latest routing change ({flipped})"
        return None

    @staticmethod
    def _require_approval_after_benchmark(event: Event, bm: dict) -> None:
        """The human must approve the numbers they were shown: an ``approve-cutover`` posted before the latest
        benchmark finished (e.g. queued while the gate was still benchmarking) is not an approval of it.
        Uses the millisecond ``finished_at_ms`` (older records: ``at``, truncated to the second, so lenient by
        < 1 s)."""
        ts = event.payload.get("timestamp")
        if not isinstance(ts, (int, float)):
            raise PreconditionFailed(f"the approval message carries no timestamp ({ts!r}), so I cannot tell whether "
                                     f"it was sent after the benchmark of rev `{bm['rev']}` — nothing changed; "
                                     f"post a new `approve-cutover`.")
        finished = (bm["finished_at_ms"] / 1000 if isinstance(bm.get("finished_at_ms"), (int, float)) else
                    datetime.strptime(bm["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        if ts / 1000 < finished:
            sent = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise PreconditionFailed(f"that approval was sent at {sent}, before the benchmark of rev `{bm['rev']}` "
                                     f"finished ({bm['at']}) — an approval must follow the numbers it approves. "
                                     f"Nothing changed; read the benchmark card, then post a new `approve-cutover`.")

    def _replayed_approval(self, event: Event) -> str | None:
        """How approval ``event`` was handled before (a redelivered/replayed message), or None if it is new."""
        mid = event.message_id
        if not mid:
            return None
        for h in reversed(self.scorecard["cutover_history"]):
            if h.get("approval_message_id") == mid:
                return (f"it cut rev `{h.get('rev')}` over at {h.get('approved_at')}"
                        + (f", rolled back at {h['rolled_back_at']}" if h.get("rolled_back_at") else ""))
        for r in reversed(self.scorecard["rollbacks"]):
            if r.get("message_id") == mid and r.get("by") == self.name:
                return f"that cutover attempt failed and was reverted at {r.get('at')}"
        return None

    def _append_history(self, record: dict) -> None:
        self.scorecard["cutover_history"].append(record)
        del self.scorecard["cutover_history"][:-MAX_HISTORY_KEPT]

    def _mark_history_rolled_back(self, sender: Sender, note: str | None = None) -> None:
        history = self.scorecard["cutover_history"]
        if history and "rolled_back_at" not in history[-1]:
            history[-1] = {**history[-1], "rolled_back_at": now_iso(), "rolled_back_by": sender.name,
                           **({"rollback_note": note} if note else {})}

    def _verify(self, target: str, direct_url: str, *, expected_rev: str | None = None) -> tuple[bool, str]:
        ok, why = proxy_control.verify_routing(self.proxy_url, direct_url, VERIFY_PATH, target,
                                               timeout=self.http_timeout)
        if not ok:
            return ok, why
        try:
            live = proxy_control.status(self.proxy_url, timeout=self.http_timeout)
        except RuntimeError as exc:
            return False, f"/_router/status unavailable after the flip: {exc}"
        if live.get("target") != target:
            return False, f"/_router/status reports `{live.get('target')}`, expected `{target}`"
        if expected_rev is not None:
            try:
                self._require_modern_rev(expected_rev, hint="the build changed during the cutover")
            except PreconditionFailed as exc:
                return False, str(exc)
        return True, why

    # ================================================================== preconditions
    def _approved_run(self, rev: str, mig: str) -> dict:
        """The fuzzer matrix's latest run, which must be approved parity on ``rev`` for migration ``mig``."""
        doc = self._find_doc(MATRIX_DOC)
        if doc is None:
            raise PreconditionFailed(f"there is no “{MATRIX_DOC}” yet — the fuzzer must shadow-test rev `{rev}` "
                                     f"and reach approved parity first.")
        fuzzer_id = self.room.agents.get("fuzzer")
        if not fuzzer_id or doc.get("created_by_agent_id") != fuzzer_id:
            raise PreconditionFailed(f"“{MATRIX_DOC}” was not created by the fuzzer agent (creator "
                                     f"`{doc.get('created_by_agent_id')}`) — refusing to trust its parity verdict.")
        state = extract_state(self.switch.load_documents([doc["id"]])[0]["content"])
        run = latest_run(state)
        if run is None:
            raise PreconditionFailed(f"“{MATRIX_DOC}” has no campaign run yet.")
        if run.get("rev") != rev:
            raise PreconditionFailed(f"the matrix's latest run #{run.get('run')} is on rev `{run.get('rev')}`, "
                                     f"not `{rev}` — parity must be verified on this exact build.")
        run_mig = (run.get("trigger") or {}).get("migration") or (state or {}).get("migration")
        if isinstance(run_mig, dict):
            run_mig = run_mig.get("id")
        if run_mig and run_mig != mig:
            raise PreconditionFailed(f"the matrix's latest run belongs to migration {run_mig}, not {mig}.")
        report = run.get("report") or {}
        if not report.get("approved_parity"):
            raise PreconditionFailed(f"the matrix's latest run #{run['run']} on rev `{rev}` is "
                                     f"“{report.get('parity_label', 'not approved')}”, not 100% approved parity.")
        return run

    def _require_modern_rev(self, rev: str, hint: str = "the build under test is not the one being served") -> None:
        try:
            r = httpx.get(f"{self.modern_url}/_meta", timeout=self.http_timeout, trust_env=False)
            r.raise_for_status()
            meta = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PreconditionFailed(f"cannot read {self.modern_url}/_meta: {exc}") from exc
        if meta.get("import_error"):
            raise PreconditionFailed(f"the modern service reports an import error: {meta['import_error']}")
        if meta.get("rev") != rev:
            raise PreconditionFailed(f"the modern service {_port(self.modern_url)} serves rev `{meta.get('rev')}`, "
                                     f"not `{rev}` — {hint}.")

    def _identical_paths(self) -> tuple[str, ...]:
        """Keep only benchmark paths that answer with the same status and JSON on both targets right now."""
        keep, dropped = [], []
        with httpx.Client(timeout=self.http_timeout, trust_env=False) as client:
            for path in self.bench_paths:
                try:
                    a, b = client.get(self.legacy_url + path), client.get(self.modern_url + path)
                except httpx.HTTPError as exc:
                    raise PreconditionFailed(f"probe of `{path}` failed: {exc}") from exc
                same = a.status_code == b.status_code and a.status_code < 500 and _body(a) == _body(b)
                (keep if same else dropped).append(path if same else f"`{path}` ({a.status_code} vs {b.status_code})")
        if dropped:
            msg = (f"benchmark endpoints that differ between legacy and modern: {', '.join(dropped)}")
            if not keep:
                raise PreconditionFailed(msg + " — no common contract endpoint left to benchmark.")
            self.say(f"{MARK['error']} {msg} — excluded from the benchmark (compared like for like only).")
        return tuple(keep)

    # ================================================================== documents
    def _find_doc(self, name: str) -> dict | None:
        return next((d for d in self.switch.documents() if d.get("name") == name), None)

    def _render(self) -> str:
        """``render_scorecard`` plus a governance-refusal section (inserted before the state block)."""
        base = render_scorecard(self.scorecard)
        idx = base.find(_STATE_MARKER)
        if idx < 0:
            raise RuntimeError("render_scorecard output has no state block marker — ledger format changed")
        lines = [base[:idx].rstrip()]
        refusals = self.scorecard.get("refusals") or []
        if refusals:
            lines += ["", "## Governance refusals (human-only commands from non-humans)", "",
                      "| When (UTC) | Sender | Kind | Command | Message | Proxy stayed on |", "|---|---|---|---|---|---|"]
            lines += [f"| {r['at']} | {r['by']} | {r['kind']} | `{r['command']}` | `{r.get('message_id')}` | "
                      f"`{r.get('proxy_target')}` |" for r in refusals]
        return embed_state("\n".join(lines), self.scorecard)

    def _save(self) -> None:
        description, instructions = DOC_META[SCORECARD_DOC]
        self.switch.upsert_document(name=SCORECARD_DOC, description=description, instructions=instructions,
                                    content=self._render())

    # ================================================================== status
    def _status_text(self) -> str:
        try:
            st = proxy_control.status(self.proxy_url, timeout=self.http_timeout)
            proxy = (f"proxy {_port(self.proxy_url)} → **{st.get('target')}** ({st.get('upstream_url')}); last flip "
                     f"{st.get('flipped_at') or 'never'} by {st.get('approved_by') or '—'}")
        except RuntimeError as exc:
            proxy = f"proxy {_port(self.proxy_url)} unreachable: {exc}"
        lines = ["**cutover-gate status**", f"- {proxy}"]
        bm = latest_benchmark(self.scorecard)
        if bm:
            lines.append(f"- latest benchmark: rev `{bm['rev']}` ({bm['migration']}) at {bm['at']} — {bm['verdict']}")
        else:
            lines.append("- no benchmark yet")
        cut = self.scorecard.get("cutover")
        if cut and cut.get("proxy_target") != "modern":
            lines.append(f"- cutover: routing `{cut.get('proxy_target')}` after a failed cutover of rev "
                         f"`{cut.get('rev')}` — {cut.get('verification')}; a human must `rollback` or "
                         f"`approve-cutover`")
        elif cut:
            lines.append(f"- cutover: rev `{cut['rev']}` approved by {cut['approved_by']} at {cut['approved_at']}, "
                         f"proxy → {cut['proxy_target']}, verified: {'yes' if cut.get('verified') else 'NO'}")
        else:
            lines.append("- cutover: none active (awaiting human approval)")
        if self.scorecard.get("rollbacks"):
            last = self.scorecard["rollbacks"][-1]
            lines.append(f"- rollbacks: {len(self.scorecard['rollbacks'])} (last {last['at']} by {last['by']})")
        lines.append(f"- governance refusals recorded: {len(self.scorecard.get('refusals') or [])}")
        return _defuse("\n".join(lines))  # router fields are external text


def _fresh_scorecard() -> dict:
    """:func:`agents.ledger.new_scorecard` plus this agent's extra sections."""
    return {**new_scorecard(), "refusals": [], "cutover_history": []}


def _defuse(text: str) -> str:
    """Neutralise ``@name`` tokens in text that did not originate here (exceptions, router fields, ids), so
    quoting it can never address a participant. U+FF20 (fullwidth @) is not a Switch mention."""
    return str(text).replace("@", "\uff20")


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.content


if __name__ == "__main__":
    main_for(CutoverAgent)
