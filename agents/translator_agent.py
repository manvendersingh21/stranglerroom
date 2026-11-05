"""@translator — ports Open Library's legacy web.py lists plugin to FastAPI and keeps the audit ledger.

Run with ``python -m agents.translator_agent`` (docs/AGENTS_SPEC.md §2). Commands (case-insensitive,
mention-addressed; the grammar lives in :mod:`agents.commands`):

``migrate <path> from legacy_openlibrary to FastAPI`` — *human-only*
    AST-introspect the legacy module (never imported or executed, :mod:`agents.legacy_introspect`), check
    that ``<path>`` is one of its routes, render the naive v1 port (:func:`agents.codegen.render_lists_module`
    with an empty policy, or the LLM in LLM mode), write it atomically, wait until the modern target's
    ``/_meta`` serves exactly that revision with every legacy route, create the ``Parity & Cutover Audit
    Ledger`` and hand off ``shadow-test rev=… migration=…`` to the fuzzer.
``preserve D-… and deprecate D-…`` — *human-only*
    Map every id to its quirk tag through the fuzzer's ``Parity Audit Matrix`` (latest run), merge the
    decisions into the previous build's policy, record each decision (who, which message, matrix evidence
    digest) in the hash-chained trail, deploy v2 and hand off ``re-verify …`` with the full decision set.
``record-cutover rev=… migration=…`` — only from the ``cutover-gate`` agent
    Cross-check the matrix (approved parity on ``rev``) and the ``Cutover Scorecard`` (benchmark on ``rev``,
    cutover to modern approved by a human participant), mirror both with sha256 evidence digests, append
    the sign-off entries and seal the ledger.
``status`` / ``help`` — anyone.

Governance: a human-only command from a non-human sender (or ``record-cutover`` from anyone but the
cutover gate) is refused with :data:`agents.commands.MARK` ``governance_refusal``, recorded in the
ledger (``refusals`` + a trail entry while the ledger is open; persisted to
``.state/translator_pending_refusals.json`` and carried into the next ledger if none exists yet), and
changes nothing. While the cutover gate's scorecard shows an active cutover to modern, neither a new
migration nor new decisions may rewrite the live module (roll back first). A failed deploy restores the
previously served module. Re-delivered command events (same message id) are applied once.

Mention hygiene: the only ``@<agent>`` this agent ever writes is the deliberate :meth:`handoff`; every
piece of text that did not originate here (command echoes, document text, exception messages) is
defused with a word joiner so it can never address a participant by accident.

LLM mode (``STRANGLER_TRANSLATOR_MODE=llm`` or ``mode="llm"``): :func:`agents.llm_synth.synthesize`
runs ``STRANGLER_LLM_CMD``. Its module is accepted only if it passes static validation AND, after the hot
reload, ``/_meta`` reports its revision without ``import_error`` and with every expected route. Any
rejection is announced in the room (``⚠️ LLM synthesis rejected: <reason> — used deterministic codegen``)
and the deterministic module is deployed instead.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from agents import ledger as L
from agents.codegen import QUIRK_TAGS, module_rev, render_lists_module
from agents.commands import (MARK, MIGRATE_EXAMPLE, Command, CommandError, defuse_mentions, format_decisions,
                             parse_cutover, parse_translator, re_verify, shadow_test, strip_mentions)
from agents.legacy_introspect import LegacyRoute, introspect_file, render_route_table
from agents.llm_synth import SynthesisError, synthesize
from agents.locks import production_lock
from agents.runtime import MODERN_URL, RoomAgent, Sender, main_for
from agents.switch_client import Event

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEGACY_MODULE = ROOT / "targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py"
DEFAULT_MODERN_LISTS = ROOT / "targets/modern_fastapi/app/lists.py"
DEFAULT_PENDING_REFUSALS = ROOT / ".state/translator_pending_refusals.json"
CUTOVER_GATE = "cutover-gate"
FUZZER = "fuzzer"
MODES = ("deterministic", "llm")
SOURCE_SYSTEM, TARGET_SYSTEM = "legacy_openlibrary", "fastapi"
DECISION_STATUSES_OPEN = ("scaffolded", "awaiting-decision", "patching", "parity-approved")

#: one line per (tag, decision) for the "🔧 Applied human decisions" message.
EXPLAIN = {
    ("json_suffix", "preserve"): "`/….json` aliases are served again, exactly like infogami's implicit alias.",
    ("json_suffix", "deprecate"): "`.json` aliases stay removed — clients must call the bare path (404 otherwise).",
    ("loose_query", "preserve"): "lenient `legacy_int`/`legacy_bool` parsing is back (`limit=all`, `details=yes`, "
                                 "`quantity=abc` coerce instead of 422).",
    ("loose_query", "deprecate"): "query parameters stay strictly typed — malformed values now answer 422.",
    ("missing_resource_200", "preserve"): "an unknown list answers `200 {\"error\": \"not_found\", \"entries\": []}` "
                                          "again.",
    ("missing_resource_200", "deprecate"): "an unknown list now answers `404` — the legacy 200 was a bug and the 404 "
                                           "is the new contract.",
    ("non_positive_quantity", "preserve"): "quote quantities −10..0 are accepted again (warehouse credit reversals).",
    ("non_positive_quantity", "deprecate"): "quote quantity must be positive — 0 and negatives now answer 422.",
    ("empty_body_500", "preserve"): "an empty seeds edit crashes with 500 again (legacy ZeroDivisionError).",
    ("empty_body_500", "deprecate"): "an empty seeds edit now answers 422 instead of web.py's 500 page — 422 is the new "
                                     "contract.",
}

HELP = (
    "ℹ️ translator commands (address me as translator):\n"
    "- `migrate <path> from legacy_openlibrary to FastAPI` — human only; e.g. "
    f"`{MIGRATE_EXAMPLE.split(' ', 1)[1]}`\n"
    "- `preserve D-… and deprecate D-…` — human only; ids come from the fuzzer's Parity Audit Matrix\n"
    "- `record-cutover rev=<rev> migration=<id>` — accepted only from the cutover-gate agent; seals the ledger\n"
    "- `status`, `help` — anyone"
)


class TranslatorError(RuntimeError):
    """A step of a translator command failed; the message is posted to the room."""


class DeployError(TranslatorError):
    """The modern target did not come up serving the module that was written."""


def defuse(text: Any) -> str:
    """Neutralise every ``@`` in text this agent did not author, so it cannot address anyone."""
    return defuse_mentions(str(text))


def _iso_ms(iso: str | None) -> int:
    """Epoch ms of a ``%Y-%m-%dT%H:%M:%SZ`` time (truncated to the second, so floors built on it are lenient by
    < 1 s, never strict); 0 when absent."""
    if not iso:
        return 0
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _sent_before(event: Event, ref_ms: int | float | None) -> str | None:
    """``"was sent at <time>"`` if ``event`` was posted before ``ref_ms`` (the Switch timestamp, epoch ms, of the
    message that started the current migration), else None. A redelivered message from an earlier migration is
    such a message; unlike message-id dedupe this survives a replaced ledger. Both times come from Switch, so no
    clock comparison is involved. No timestamp on either side → None (the message-id checks still apply)."""
    ts = event.payload.get("timestamp")
    if not isinstance(ref_ms, (int, float)) or not isinstance(ts, (int, float)) or ts > ref_ms:
        return None
    # A tie is stale too: callers have already let the reference message itself through by its id, so a
    # *different* message with the very same Switch timestamp cannot be proven newer — refuse it (a person can
    # simply post again).
    when = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return f"was sent at {when}" + (" (the same millisecond as the reference message)" if ts == ref_ms else "")


def _request_floor(migration: dict) -> int | float | None:
    """The replay floor of a migration: its request's Switch timestamp, else — only for a ledger written before
    that was stored — its ``requested_at`` (this agent's clock when it handled the request, truncated to the
    second), never missing."""
    ts = migration.get("request_timestamp")
    if isinstance(ts, (int, float)):
        return ts
    return _iso_ms(migration.get("requested_at")) or None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _template_regex(template: str, json_alias: bool) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", template)
    body = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + body + (r"(?:\.json)?" if json_alias else "") + "$")


def match_route(path: str, routes: list[LegacyRoute]) -> list[LegacyRoute]:
    """Routes whose template equals ``path`` or matches it as a concrete URL path (``.json`` alias too)."""
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    return [r for r in routes if clean == r.template or _template_regex(r.template, r.json_alias).match(clean)]


def expected_routes(routes: list[LegacyRoute], policy: dict[str, str]) -> list[str]:
    """``"METHOD path"`` entries ``/_meta.routes`` must contain for ``routes`` under ``policy``."""
    out = []
    for r in routes:
        out.append(f"{r.method} {r.template}")
        if r.json_alias and policy.get("json_suffix") == "preserve":
            out.append(f"{r.method} {r.template}.json")
    return out


class TranslatorAgent(RoomAgent):
    """@translator: legacy web.py → FastAPI codegen, hot deploy, and the Parity & Cutover Audit Ledger."""

    name = "translator"

    def __init__(self, *, legacy_module_path: str | Path | None = None, modern_lists_path: str | Path | None = None,
                 modern_url: str | None = None, mode: str | None = None, llm_cmd: str | None = None,
                 llm_timeout: float = 300.0, reload_timeout: float = 30.0,
                 pending_refusals_path: str | Path | None = None, approval_lookup_attempts: int = 3,
                 approval_lookup_delay: float = 1.0, production_lock_timeout: float = 120.0, **kwargs: Any):
        super().__init__(**kwargs)
        if approval_lookup_attempts < 1:
            raise ValueError("approval_lookup_attempts must be >= 1")
        self.approval_lookup_attempts = approval_lookup_attempts
        self.approval_lookup_delay = approval_lookup_delay
        self.production_lock_timeout = production_lock_timeout
        self.legacy_module_path = Path(legacy_module_path or DEFAULT_LEGACY_MODULE)
        self.modern_lists_path = Path(modern_lists_path or os.environ.get("MODERN_LISTS_PATH") or DEFAULT_MODERN_LISTS)
        self.modern_url = (modern_url or MODERN_URL).rstrip("/")
        self.mode = (mode or os.environ.get("STRANGLER_TRANSLATOR_MODE") or "deterministic").strip().lower()
        if self.mode not in MODES:
            raise ValueError(f"translator mode must be one of {MODES}, got {self.mode!r}")
        self.llm_cmd = llm_cmd
        self.llm_timeout = llm_timeout
        self.reload_timeout = reload_timeout
        self.ledger: dict | None = None
        self._ledger_loaded = False
        self._restored: str | None = None
        #: refusals seen while no ledger existed; persisted to ``pending_refusals_path`` (survives restarts)
        #: and carried into the next migration's ledger.
        self.pending_refusals_path = Path(pending_refusals_path or os.environ.get("STRANGLER_TRANSLATOR_PENDING")
                                          or DEFAULT_PENDING_REFUSALS)
        self.pending_refusals: list[dict] = self._read_pending()

    @property
    def modern_port(self) -> str:
        return f":{urlparse(self.modern_url).port or 80}"

    # ------------------------------------------------------------------ lifecycle
    def on_connected(self) -> None:
        try:
            state = self._load_ledger()
        except TranslatorError as exc:  # forged/broken ledger: tell the room, keep serving (status/help work)
            self.log.error("could not rehydrate the ledger: %s", exc)
            self.say(f"{MARK['error']} translator could not rehydrate the “{L.LEDGER_DOC}”: {defuse(exc)}. "
                     f"Migrations and decisions are blocked until the document is removed.")
            return
        if state:
            self.log.info("rehydrated ledger: migration %s, status %s, %d trail entries",
                          state["migration"]["id"], state["status"], len(state["trail"]))

    # ------------------------------------------------------------------ dispatch
    def handle(self, event: Event, sender: Sender) -> None:
        verb = "command"
        self._restored = None
        try:
            try:
                cmd = parse_translator(event.body)
            except CommandError as exc:
                # parse_translator only raises for `migrate` / decision commands, which are human-only.
                if sender.is_human:
                    self.say(f"{MARK['error']} {defuse(exc)} — nothing changed. Try `help`.")
                else:
                    self._refuse(event, sender, "the decision/migration commands are reserved for the human lead")
                return
            if cmd is None:
                if sender.is_human:
                    self.say("ℹ️ I did not recognise that. Ask me for `help`.")
                else:
                    self.log.info("ignoring chatter from %s (%s)", sender.name, sender.kind)
                return
            verb = cmd.verb
            self._run(event, sender, cmd)
        except Exception as exc:  # noqa: BLE001 - report every failure, defused, and keep serving
            self.log.exception("%s failed", verb)
            restored = {None: "", "restored": " The previously deployed module was restored.",
                        "removed": " The partially deployed module was removed."}.get(self._restored, "")
            self.say(f"{MARK['error']} translator could not complete `{verb}`: "
                     f"`{type(exc).__name__}: {defuse(str(exc)[:400])}` — the ledger was not updated.{restored}")

    def _run(self, event: Event, sender: Sender, cmd: Command) -> None:
        if cmd.verb == "help":
            self.say(HELP)
        elif cmd.verb == "status":
            self.say(self._status())
        elif cmd.verb == "migrate":
            if not sender.is_human:
                self._refuse(event, sender, "only the human lead may start a migration")
            else:
                self._under_production_lock(self._migrate, event, sender, cmd)
        elif cmd.verb == "decide":
            if not sender.is_human:
                self._refuse(event, sender, "only the human lead may decide what happens to a divergence")
            else:
                self._under_production_lock(self._decide, event, sender, cmd)
        elif cmd.verb == "record-cutover":
            if not (sender.is_agent and sender.name.lower() == CUTOVER_GATE):
                self._refuse(event, sender, "record-cutover is accepted only from the cutover-gate agent, "
                                            "after the human approved the cutover there")
            else:
                self._record_cutover(event, sender, cmd)
        else:  # pragma: no cover - parse_translator has no other verbs
            self.say(f"{MARK['error']} unsupported command `{defuse(cmd.verb)}`.")

    def _under_production_lock(self, fn, event: Event, sender: Sender, cmd: Command) -> None:
        """Run a build-changing command while holding the production lock (``agents/locks.py``), so it can never
        interleave with the cutover gate's flip or rollback."""
        try:
            with production_lock(timeout=self.production_lock_timeout):
                fn(event, sender, cmd)
        except TimeoutError as exc:
            self.say(f"{MARK['error']} `{cmd.verb}` not done — {defuse(exc)}; a cutover or rollback is in progress. "
                     f"Nothing changed; post it again once that has finished.")

    # ------------------------------------------------------------------ documents
    def _find_doc(self, name: str) -> dict | None:
        return next((d for d in self.switch.documents() if d.get("name") == name), None)

    def _read_doc_state(self, name: str, writer: str) -> dict | None:
        """State of room document ``name`` if it exists AND was created by agent ``writer`` (else raise)."""
        doc = self._find_doc(name)
        if doc is None:
            return None
        writer_id = self.switch.agent_id if writer == self.name else self.room.agents.get(writer)
        if not writer_id or doc.get("created_by_agent_id") != writer_id:
            raise TranslatorError(f"room document “{name}” was not created by the {writer} agent "
                                  f"(creator {doc.get('created_by_agent_id')}) — refusing to trust it")
        content = (self.switch.load_documents([doc["id"]]) or [{}])[0].get("content", "")
        try:
            state = L.extract_state(content)
        except ValueError as exc:
            raise TranslatorError(f"room document “{name}” has an unreadable state block: {exc}") from exc
        if not isinstance(state, dict):
            raise TranslatorError(f"room document “{name}” has no machine-readable state block")
        return state

    def _load_ledger(self) -> dict | None:
        if not self._ledger_loaded:
            state = self._read_doc_state(L.LEDGER_DOC, self.name)
            if state is not None:
                if state.get("kind") != "ledger" or not isinstance(state.get("trail"), list):
                    raise TranslatorError(f"“{L.LEDGER_DOC}” does not hold a ledger state")
                ok, why = L.verify_chain(state["trail"])
                if not ok:
                    raise TranslatorError(f"“{L.LEDGER_DOC}” audit trail is broken ({why}) — refusing to continue it")
            self.ledger, self._ledger_loaded = state, True
        return self.ledger

    def _save_ledger(self, state: dict) -> None:
        description, instructions = L.DOC_META[L.LEDGER_DOC]
        self.switch.upsert_document(name=L.LEDGER_DOC, description=description, instructions=instructions,
                                    content=L.render_ledger(state))
        self.ledger, self._ledger_loaded = state, True

    # ------------------------------------------------------------------ governance
    def _read_pending(self) -> list[dict]:
        try:
            data = json.loads(self.pending_refusals_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"unreadable pending-refusals file {self.pending_refusals_path}: {exc}") from exc
        if not isinstance(data, list):
            raise RuntimeError(f"pending-refusals file {self.pending_refusals_path} does not hold a list")
        return data

    def _write_pending(self) -> None:
        path = self.pending_refusals_path
        if not self.pending_refusals:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.pending_refusals, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _refuse(self, event: Event, sender: Sender, reason: str) -> None:
        """Post the governance refusal and record it; the refusal is posted even if recording fails."""
        command = strip_mentions(event.body)[:200]
        refusal = {"at": L.now_iso(), "by": sender.name, "kind": sender.kind, "command": command,
                   "reason": reason, "message_id": event.message_id}
        try:
            state = self._load_ledger()
            if state is None:
                self.pending_refusals.append(refusal)
                self._write_pending()
                where = "no ledger exists yet — recorded and carried into the next migration's ledger"
            else:
                state = copy.deepcopy(state)
                state["refusals"].append(refusal)
                if not state.get("sealed"):
                    L.append_entry(state["trail"], actor=sender.name, actor_kind=sender.kind,
                                   event="governance-refusal",
                                   summary=f"refused `{command[:80]}` from {sender.kind} {sender.name}",
                                   evidence={"reason": reason}, source_message_id=event.message_id)
                    where = "recorded in the ledger"
                else:
                    where = "recorded in the ledger's refusal list (the sealed audit trail stays closed)"
                self._save_ledger(state)
        except Exception as exc:  # noqa: BLE001 - the refusal itself must still be announced
            self.log.exception("could not record a governance refusal")
            where = f"WARNING: it could not be recorded ({type(exc).__name__}: {defuse(str(exc)[:200])})"
        self.say(f"{MARK['governance_refusal']}: refused `{defuse(command)}` from {sender.kind} "
                 f"{defuse(sender.name)} — {reason}. Nothing was changed; {where}.")

    # ------------------------------------------------------------------ build + deploy
    def _header(self, mig: str, build_no: int, policy: dict[str, str], by: str) -> str:
        pol = ", ".join(f"{t}={d}" for t, d in sorted(policy.items())) or "none (naive v1 textbook port)"
        return f"Migration {mig}, build v{build_no}, requested by {by}. Quirk policy: {pol}."

    def _write_atomic(self, source: str | bytes) -> None:
        target = self.modern_lists_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".lists-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(source.encode("utf-8") if isinstance(source, str) else source)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @contextlib.contextmanager
    def _deployment(self) -> Iterator[None]:
        """Snapshot the served module; if anything in the block fails (deploy, verification, ledger save),
        put the previous bytes back (or remove the new file) so the target never keeps an unrecorded build."""
        try:
            previous: bytes | None = self.modern_lists_path.read_bytes()
        except FileNotFoundError:
            previous = None
        try:
            yield
        except BaseException:
            try:
                if previous is None:
                    self.modern_lists_path.unlink(missing_ok=True)
                    self._restored = "removed"
                else:
                    self._write_atomic(previous)
                    self._restored = "restored"
            except Exception:  # noqa: BLE001 - keep the original error; log the failed restore
                self.log.exception("could not restore %s", self.modern_lists_path)
            raise

    def _active_cutover(self) -> dict | None:
        """The cutover gate's active cutover (None after a rollback or before any cutover). A cutover whose
        routing is ``unknown`` (a failed cutover whose automatic revert also failed) counts as active: the
        module may be live behind :9000."""
        cut = (self._read_doc_state(L.SCORECARD_DOC, CUTOVER_GATE) or {}).get("cutover")
        return cut if isinstance(cut, dict) and cut.get("proxy_target") != "legacy" else None

    @staticmethod
    def _serving(cut: dict) -> str:
        if cut.get("proxy_target") == "modern":
            return "currently serves the modern service"
        return (f"may be serving the modern service (the cutover gate records routing "
                f"`{defuse(cut.get('proxy_target'))}` after a failed cutover)")

    def _wait_meta(self, rev: str) -> dict:
        """Poll ``/_meta`` until it reports ``rev``; raise :class:`DeployError` on import error or timeout."""
        deadline = time.monotonic() + self.reload_timeout
        last: str = "no answer"
        while True:
            try:
                r = httpx.get(f"{self.modern_url}/_meta", timeout=3)
                meta = r.json()
                if meta.get("rev") == rev:
                    if meta.get("import_error"):
                        raise DeployError(f"rev {rev} is served but failed to import: {meta['import_error']}")
                    return meta
                last = f"rev {meta.get('rev')}"
            except DeployError:
                raise
            except (httpx.HTTPError, ValueError) as exc:  # reloader restarting / not up yet
                last = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise DeployError(f"the modern target at {self.modern_url} did not serve rev {rev} within "
                                  f"{self.reload_timeout:.0f}s (last seen: {last})")
            time.sleep(0.25)

    def _deploy(self, source: str, routes: list[LegacyRoute], policy: dict[str, str]) -> tuple[str, dict]:
        rev = module_rev(source)
        self._write_atomic(source)
        meta = self._wait_meta(rev)
        missing = [r for r in expected_routes(routes, policy) if r not in meta.get("routes", [])]
        if missing:
            raise DeployError(f"rev {rev} is served but lacks legacy route(s): {', '.join(missing)}")
        return rev, meta

    def _build_and_deploy(self, routes: list[LegacyRoute], policy: dict[str, str], header: str
                          ) -> tuple[str, str, dict, str | None]:
        """Render (LLM or deterministic), deploy and verify. Returns ``(rev, mode, meta, note)``."""
        if self.mode == "llm":
            try:
                legacy_source = self.legacy_module_path.read_text(encoding="utf-8")
                source = synthesize(legacy_source, policy, cmd=self.llm_cmd, timeout=self.llm_timeout)
                rev, meta = self._deploy(source, routes, policy)
                return rev, "llm", meta, "LLM-synthesised; accepted after static checks and hot reload"
            except (SynthesisError, DeployError) as exc:
                reason = defuse(" ".join(str(exc).split())[:400])
                self.say(f"{MARK['error']} LLM synthesis rejected: {reason} — used deterministic codegen")
                note = f"LLM synthesis rejected ({reason[:160]}); deterministic fallback"
                source = render_lists_module(policy, header_comment=header)
                rev, meta = self._deploy(source, routes, policy)
                return rev, "deterministic", meta, note
        source = render_lists_module(policy, header_comment=header)
        rev, meta = self._deploy(source, routes, policy)
        return rev, "deterministic", meta, None

    # ------------------------------------------------------------------ migrate
    def _introspect(self) -> list[LegacyRoute]:
        routes = introspect_file(self.legacy_module_path)
        if not routes:
            raise TranslatorError(f"no web.py routes found in {self.legacy_module_path}")
        return routes

    def _legacy_rel(self) -> str:
        try:
            return str(self.legacy_module_path.resolve().relative_to(ROOT))
        except ValueError:
            return str(self.legacy_module_path)

    def _migrate(self, event: Event, sender: Sender, cmd: Command) -> None:
        path, source, target = cmd.args["path"], cmd.args["source"], cmd.args["target"]
        if source.lower() != SOURCE_SYSTEM or target.lower() != TARGET_SYSTEM:
            self.say(f"{MARK['error']} unsupported migration `{defuse(source)}` → `{defuse(target)}`: this translator "
                     f"ports `legacy_openlibrary` (web.py) to `FastAPI` only — nothing changed.")
            return
        routes = self._introspect()
        table = defuse(render_route_table(routes))
        if not match_route(path, routes):
            self.say(f"{MARK['error']} `{defuse(path)}` is not served by the legacy module "
                     f"`{defuse(self._legacy_rel())}` — nothing changed. Its routes (AST-extracted):\n\n{table}")
            return
        previous = self._load_ledger()
        started = {previous["migration"].get("request_message_id"): previous["migration"]["id"]} if previous else {}
        started.update({h.get("request_message_id"): h["id"] for h in (previous or {}).get("history", [])
                        if h.get("request_message_id")})
        if event.message_id and event.message_id in started:
            self.say(f"ℹ️ that request already started migration {started[event.message_id]} — nothing changed.")
            return
        stale = previous and _sent_before(event, _request_floor(previous["migration"]))
        if stale:
            self.say(f"ℹ️ that request {stale}, before migration {previous['migration']['id']} began — a stale or "
                     f"replayed request never starts a migration. Nothing changed; post a new `migrate` to start one.")
            return
        cut = self._active_cutover()
        if cut:
            self.say(f"{MARK['error']} :9000 {self._serving(cut)} (cutover of rev "
                     f"`{defuse(cut.get('rev'))}`, migration {defuse(cut.get('migration'))}); a new migration would "
                     f"replace the live module with a naive v1. Roll back first (reply to the cutover gate with "
                     f"`rollback`). Nothing changed.")
            return
        mig, n = f"MIG-{_utc_stamp()}", 1
        while previous and mig == previous["migration"]["id"] or any(
                mig == h["id"] for h in (previous or {}).get("history", [])):
            n += 1
            mig = f"MIG-{_utc_stamp()}-{n}"
        supersedes = ""
        if previous and not previous.get("sealed"):
            supersedes = f" It supersedes the unfinished migration {previous['migration']['id']}."
        self.say(f"{MARK['migration_accepted']} {mig} accepted from @{sender.name}: `{defuse(path)}` · "
                 f"legacy_openlibrary (web.py) → FastAPI.{supersedes} AST-extracted {len(routes)} legacy routes from "
                 f"`{defuse(self._legacy_rel())}` (parsed, never imported or executed):\n\n{table}")

        state = L.new_ledger(migration_id=mig, path=path, source=SOURCE_SYSTEM, target="FastAPI",
                             requested_by=sender.name, request_message_id=event.message_id)
        state["migration"]["request_timestamp"] = event.payload.get("timestamp")  # Switch epoch ms (replay floor)
        if previous:  # keep the record of earlier migrations (the document holds one ledger at a time)
            state["history"] = previous.get("history", []) + [{
                "id": previous["migration"]["id"], "path": previous["migration"]["path"],
                "request_message_id": previous["migration"].get("request_message_id"),
                "request_timestamp": _request_floor(previous["migration"]),
                "status": previous["status"], "sealed": previous.get("sealed"),
                "builds": [b["rev"] for b in previous["builds"]], "decisions": previous["decisions"],
                "trail_head": previous["trail"][-1]["hash"] if previous["trail"] else None,
                "trail_entries": len(previous["trail"]), "state_digest": L.digest(previous)}]
        legacy_digest = hashlib.sha256(self.legacy_module_path.read_bytes()).hexdigest()
        L.append_entry(state["trail"], actor=sender.name, actor_kind="human", event="migration-requested",
                       summary=f"migrate {path} from {source} to {target}",
                       evidence={"path": path, "source": source, "target": target},
                       source_message_id=event.message_id)
        L.append_entry(state["trail"], actor=self.name, actor_kind="agent", event="routes-extracted",
                       summary=f"{len(routes)} legacy routes AST-extracted from {self._legacy_rel()}",
                       evidence={"legacy_module": self._legacy_rel(), "legacy_sha256": legacy_digest,
                                 "routes": [f"{r.method} {r.template}" for r in routes]})
        with self._deployment(), self.working():
            rev, mode, meta, note = self._build_and_deploy(routes, {}, self._header(mig, 1, {}, sender.name))
            state["routes"] = self._route_inventory(routes, meta)
            state["builds"].append({"build": 1, "rev": rev, "mode": mode, "policy": {}, "at": L.now_iso(),
                                    "note": note or "naive v1 (textbook FastAPI port)"})
            L.append_entry(state["trail"], actor=self.name, actor_kind="agent", event="build-deployed",
                           summary=f"v1 rev {rev} ({mode}) serving on {self.modern_port}",
                           evidence={"rev": rev, "mode": mode, "policy": {}, "meta_routes": meta.get("routes", [])})
            self._absorb_pending_refusals(state)
            self._save_ledger(state)
        self.pending_refusals = []
        self._write_pending()
        self.say(f"{MARK['v1_deployed']} to {self.modern_port} — rev `{rev}` ({mode} codegen): a naive, textbook "
                 f"FastAPI port with no legacy quirk preserved yet. `/_meta` confirms the rev and all {len(routes)} "
                 f"legacy routes; the “{L.LEDGER_DOC}” is open for migration {mig}. Handing over to the fuzzer for "
                 f"differential shadow testing.")
        self.handoff(FUZZER, shadow_test(rev, mig))

    def _route_inventory(self, routes: list[LegacyRoute], meta: dict) -> list[dict]:
        served = meta.get("routes", [])
        out: dict[str, dict] = {}
        for r in routes:
            row = out.setdefault(r.handler, {"handler": r.handler, "methods": [],
                                             "legacy_path": r.regex + (r"(?:\.json)?" if r.json_alias else ""),
                                             "template": r.template, "fastapi_paths": []})
            row["methods"].append(r.method)
            for candidate in (f"{r.method} {r.template}", f"{r.method} {r.template}.json"):
                if candidate in served and candidate not in row["fastapi_paths"]:
                    row["fastapi_paths"].append(candidate)
        return list(out.values())

    def _absorb_pending_refusals(self, state: dict) -> None:
        for ref in self.pending_refusals:
            state["refusals"].append(ref)
            L.append_entry(state["trail"], actor=ref["by"], actor_kind=ref["kind"], event="governance-refusal",
                           summary=f"refused `{ref['command'][:80]}` from {ref['kind']} {ref['by']} (before this "
                                   f"migration)", evidence={"reason": ref["reason"], "refused_at": ref["at"]},
                           source_message_id=ref.get("message_id"))

    # ------------------------------------------------------------------ decide
    def _decide(self, event: Event, sender: Sender, cmd: Command) -> None:
        decisions: dict[str, str] = cmd.args["decisions"]
        state = self._load_ledger()
        if state is None or state.get("sealed"):
            self.say(f"{MARK['error']} there is no open migration to apply decisions to — start one with "
                     f"`{MIGRATE_EXAMPLE.split(' ', 1)[1]}`. Nothing changed.")
            return
        mig = state["migration"]["id"]
        if state["status"] not in DECISION_STATUSES_OPEN:
            self.say(f"{MARK['error']} migration {mig} is already `{state['status']}` — decisions can no longer "
                     f"change the deployed service. Nothing changed.")
            return
        if event.message_id and any(e.get("source_message_id") == event.message_id and e["event"] == "decision-recorded"
                                    for e in state["trail"]):
            self.say(f"ℹ️ those decisions (message {defuse(event.message_id)}) are already recorded for {mig} — "
                     f"nothing changed.")
            return
        stale = _sent_before(event, _request_floor(state["migration"]))
        if stale:
            self.say(f"ℹ️ those decisions (message {defuse(event.message_id)}) {stale}, before migration {mig} began — "
                     f"a stale or replayed decision is never applied to a newer migration. Nothing changed.")
            return
        latest = max((r.get("message_timestamp") or 0 for r in (state.get("decisions") or {}).values()), default=0)
        stale = _sent_before(event, latest or None)
        if stale:  # e.g. a redelivered no-op decision line, older than the human's latest (different) decision
            self.say(f"ℹ️ those decisions (message {defuse(event.message_id)}) {stale}, before the latest decision "
                     f"already applied to {mig} — a stale or replayed decision never overrides a newer one. "
                     f"Nothing changed.")
            return
        cut = self._active_cutover()
        if cut:
            self.say(f"{MARK['error']} :9000 {self._serving(cut)} (cutover of rev "
                     f"`{defuse(cut.get('rev'))}`); a new build now would change production without re-verification. "
                     f"Roll back first (reply to the cutover gate with `rollback`). Nothing changed.")
            return
        matrix = self._read_doc_state(L.MATRIX_DOC, FUZZER)
        run = L.latest_run(matrix)
        if run is None:
            self.say(f"{MARK['error']} the fuzzer has not published a shadow campaign yet (no “{L.MATRIX_DOC}”), so "
                     f"there are no divergence ids to decide on. Nothing changed.")
            return
        run_mig = matrix.get("migration") or (run.get("trigger") or {}).get("migration")
        if run_mig and run_mig != mig:
            self.say(f"{MARK['error']} the latest shadow campaign belongs to migration {defuse(run_mig)}, not {mig} — "
                     f"wait for the fuzzer's campaign on this migration. Nothing changed.")
            return
        clusters = {c["id"]: c for c in run["report"].get("clusters", [])}
        unknown = [cid for cid in decisions if cid not in clusters]
        if unknown:
            known = ", ".join(sorted(clusters, key=lambda c: int(c[2:]))) or "none"
            self.say(f"{MARK['error']} unknown divergence id(s) {', '.join(unknown)} — the latest campaign "
                     f"(run #{defuse(run['run'])} on rev `{defuse(run['rev'])}`) knows: {known}. Nothing changed.")
            return
        unclassified = [cid for cid, d in decisions.items() if d == "preserve" and clusters[cid]["tag"] not in QUIRK_TAGS]
        if unclassified:
            self.say(f"{MARK['error']} cannot preserve {', '.join(unclassified)}: "
                     + "; ".join(f"{cid} is `{defuse(clusters[cid]['tag'])}`" for cid in unclassified)
                     + " — an unclassified divergence has no codegen rule, so deterministic codegen cannot reproduce "
                       "it (deprecate it, or fix the port by hand). Nothing changed.")
            return

        recorded = state.get("decisions") or {}
        if state["builds"] and all(recorded.get(cid, {}).get("decision") == d and recorded[cid].get("tag") ==
                                   clusters[cid]["tag"] for cid, d in decisions.items()):
            # A re-posted decision line (a retry or a double post) must not deploy yet another build: that would
            # invalidate the verification/benchmark of the build that already carries these decisions.
            build = state["builds"][-1]
            self.say(f"ℹ️ {format_decisions(decisions)} is already applied to migration {mig} (build "
                     f"v{build['build']}, rev `{build['rev']}`) — nothing changed, no new build. To re-run the shadow "
                     f"campaign, ask the fuzzer for `re-verify rev={build['rev']} migration={mig}`.")
            return

        state = copy.deepcopy(state)
        previous_policy = dict(state["builds"][-1]["policy"]) if state["builds"] else {}
        policy = {**previous_policy,
                  **{clusters[cid]["tag"]: d for cid, d in decisions.items() if clusters[cid]["tag"] in QUIRK_TAGS}}
        matrix_digest = L.digest(matrix)
        at = L.now_iso()
        for cid, d in decisions.items():
            tag = clusters[cid]["tag"]
            state["decisions"][cid] = {"decision": d, "tag": tag, "by": sender.name, "message_id": event.message_id,
                                       "at": at, "message_timestamp": event.payload.get("timestamp")}
            L.append_entry(state["trail"], actor=sender.name, actor_kind="human", event="decision-recorded",
                           summary=f"{cid} ({tag}) → {d}", source_message_id=event.message_id,
                           evidence={"matrix_run": run["run"], "matrix_rev": run["rev"],
                                     "matrix_digest": matrix_digest, "cluster": cid, "tag": tag})
        state["decisions"] = dict(sorted(state["decisions"].items(), key=lambda kv: int(kv[0][2:])))
        state["divergences"] = [{"id": c["id"], "tag": c["tag"], "title": c["title"],
                                 "legacy_status": c.get("legacy_status"), "modern_status": c.get("modern_status"),
                                 "hits": c.get("count"), "status": c.get("status"),
                                 "sample_request": c.get("sample_request")}
                                for c in sorted(clusters.values(), key=lambda c: int(c["id"][2:]))]
        state["status"] = "patching"
        build_no = len(state["builds"]) + 1
        routes = self._introspect()
        all_decisions = {cid: rec["decision"] for cid, rec in state["decisions"].items()}
        with self._deployment(), self.working():
            rev, mode, meta, note = self._build_and_deploy(routes, policy,
                                                           self._header(mig, build_no, policy, sender.name))
            state["routes"] = self._route_inventory(routes, meta)
            state["builds"].append({"build": build_no, "rev": rev, "mode": mode, "policy": policy,
                                    "at": L.now_iso(),
                                    "note": note or f"human decisions: {format_decisions(all_decisions)}"})
            L.append_entry(state["trail"], actor=self.name, actor_kind="agent", event="build-deployed",
                           summary=f"v{build_no} rev {rev} ({mode}) serving on {self.modern_port}",
                           evidence={"rev": rev, "mode": mode, "policy": policy,
                                     "meta_routes": meta.get("routes", [])})
            self._save_ledger(state)

        lines = []
        for cid, d in decisions.items():
            tag = clusters[cid]["tag"]
            lines.append(f"- **{cid}** `{defuse(tag)}` → **{d}**: "
                         + EXPLAIN.get((tag, d), "approved deviation: modern's current behaviour becomes the contract."))
        self.say(f"{MARK['decisions_applied']} from @{sender.name}: {format_decisions(decisions)} "
                 f"(migration {mig}, recorded in the “{L.LEDGER_DOC}” with the matrix evidence digest):\n"
                 + "\n".join(lines)
                 + f"\n\nv{build_no} deployed to {self.modern_port} — rev `{rev}` ({mode} codegen); `/_meta` confirms the "
                   f"rev and every legacy route. Asking the fuzzer to re-verify.")
        self.handoff(FUZZER, re_verify(rev, mig, all_decisions))

    # ------------------------------------------------------------------ record-cutover
    def _record_cutover(self, event: Event, sender: Sender, cmd: Command) -> None:
        rev, mig = cmd.args.get("rev"), cmd.args.get("migration")
        if not rev or not mig:
            self.say(f"{MARK['error']} `record-cutover` needs `rev=<rev> migration=<id>` — nothing recorded.")
            return
        state = self._load_ledger()
        if state is None or state["migration"]["id"] != mig:
            self.say(f"{MARK['error']} record-cutover for migration {mig}, but the ledger records "
                     f"{state['migration']['id'] if state else 'no migration'} — nothing recorded.")
            return
        if state.get("sealed"):
            self.say(f"ℹ️ migration {mig} is already sealed (head `sha256:{state['sealed']['head_hash'][:16]}…`, "
                     f"{state['sealed']['entries']} entries) — nothing to do.")
            return
        current = state["builds"][-1]["rev"] if state["builds"] else None
        if rev != current:
            self.say(f"{MARK['error']} record-cutover names rev `{rev}` but the latest build of {mig} is "
                     f"`{current}` — nothing recorded.")
            return
        matrix = self._read_doc_state(L.MATRIX_DOC, FUZZER)
        run = L.latest_run(matrix)
        if run is None or run["rev"] != rev or not run["report"].get("approved_parity"):
            seen = ("no campaign" if run is None else
                    f"run #{defuse(run['run'])} on rev `{defuse(run['rev'])}`: "
                    f"{defuse(run['report'].get('parity_label'))}")
            self.say(f"{MARK['error']} the fuzzer's latest campaign does not approve parity on rev `{rev}` "
                     f"({seen}) — refusing to seal.")
            return
        scorecard = self._read_doc_state(L.SCORECARD_DOC, CUTOVER_GATE)
        bm = L.latest_benchmark(scorecard)
        cut = (scorecard or {}).get("cutover")
        problems = []
        if bm is None or bm.get("rev") != rev:
            problems.append(f"no benchmark for rev `{rev}` in the “{L.SCORECARD_DOC}”")
        if not cut or cut.get("proxy_target") != "modern":
            problems.append("the scorecard records no cutover to the modern target")
        elif cut.get("rev") != rev or cut.get("migration") != mig:
            problems.append(f"the recorded cutover is for rev `{defuse(cut.get('rev'))}` / migration "
                            f"{defuse(cut.get('migration'))}, not `{rev}` / {mig}")
        elif not self.classify(str(cut.get("approved_by") or "")).is_human:
            problems.append(f"the cutover approver `{defuse(cut.get('approved_by'))}` is not the human lead "
                            f"({self.room.human})")
        elif not cut.get("approval_message_id"):
            problems.append("the recorded cutover names no approval message, so the human's approval cannot be "
                            "verified")
        elif cut.get("verified") is not True:
            problems.append("the recorded cutover was not verified through the proxy")
        if bm is not None and bm.get("migration") not in (None, mig):
            problems.append(f"the benchmark belongs to migration {defuse(bm.get('migration'))}")
        approval_verified = None
        if not problems:
            # the approval must postdate both the build being cut over and the benchmark the human was shown
            floor = max(_iso_ms(state["builds"][-1].get("at")),
                        bm["finished_at_ms"] if isinstance(bm.get("finished_at_ms"), (int, float))
                        else _iso_ms(bm.get("at")))
            approval_verified, why_not = self._check_approval(cut, not_before_ms=floor)
            if why_not:
                problems.append(why_not)
            elif approval_verified is not True:
                # The seal is final and attributes the cutover to the human: never seal on an approval the room
                # history does not show (a cutover-gate scorecard alone is not the human's word).
                problems.append(
                    f"the approval message `{defuse(cut.get('approval_message_id'))}` could not be verified in the "
                    + ("room history (read_context failed on every attempt) — once Switch answers again, post "
                       "`approve-cutover` to the cutover gate once more; it re-sends the record"
                       if approval_verified is None else
                       "room history — `rollback` and approve the cutover again"))
        if problems:
            self.say(f"{MARK['error']} cannot seal migration {mig}: " + "; ".join(problems) + " — nothing recorded.")
            return

        state = copy.deepcopy(state)
        matrix_digest, scorecard_digest = L.digest(matrix), L.digest(scorecard)
        rep = run["report"]
        state["parity"] = {"label": rep["parity_label"], "rev": rev, "run": run["run"], "at": run["at"],
                           "approved": True, "total_requests": rep["total_requests"],
                           "clusters": [{"id": c["id"], "tag": c["tag"], "status": c["status"],
                                         "decision": c.get("decision")} for c in rep.get("clusters", [])],
                           "evidence": matrix_digest}
        state["benchmark"] = {**copy.deepcopy(bm), "evidence": scorecard_digest}
        approver = cut["approved_by"]
        state["cutover"] = {"approved_by": approver, "approved_at": cut.get("approved_at"),
                            "approval_message_id": cut.get("approval_message_id"),
                            "proxy_target": cut["proxy_target"], "upstream_url": cut.get("upstream_url", ""),
                            "flipped_at": cut.get("flipped_at"), "verified": bool(cut.get("verified")),
                            "evidence": f"parity {rep['parity_label']} (matrix sha256:{matrix_digest[:16]}…), "
                                        f"benchmark p99 {bm['legacy']['p99_ms']:.1f} → {bm['modern']['p99_ms']:.1f} ms "
                                        f"(scorecard sha256:{scorecard_digest[:16]}…), rev {rev}",
                            "recorded_message_id": event.message_id,
                            "approval_message_verified": approval_verified}
        state["status"] = "cut-over"
        trail = state["trail"]
        L.append_entry(trail, actor=FUZZER, actor_kind="agent", event="parity-verified",
                       summary=f"{rep['parity_label']} on rev {rev} (run #{run['run']}, {rep['total_requests']} requests)",
                       evidence={"matrix_digest": matrix_digest, "run": run["run"], "rev": rev})
        L.append_entry(trail, actor=CUTOVER_GATE, actor_kind="agent", event="benchmark-recorded",
                       summary=f"p99 legacy {bm['legacy']['p99_ms']:.1f} ms → modern {bm['modern']['p99_ms']:.1f} ms",
                       evidence={"scorecard_digest": scorecard_digest, "rev": rev, "total": bm.get("total"),
                                 "concurrency": bm.get("concurrency")})
        L.append_entry(trail, actor=approver, actor_kind="human", event="cutover-approved",
                       summary=f"{approver} approved the cutover of rev {rev}",
                       evidence={"approved_at": cut.get("approved_at")},
                       source_message_id=cut.get("approval_message_id"))
        L.append_entry(trail, actor=CUTOVER_GATE, actor_kind="agent", event="proxy-flipped",
                       summary=f":9000 → {cut['proxy_target']} ({cut.get('upstream_url', '')})",
                       evidence={"proxy_target": cut["proxy_target"], "upstream_url": cut.get("upstream_url"),
                                 "flipped_at": cut.get("flipped_at"), "verified": bool(cut.get("verified"))},
                       source_message_id=event.message_id)
        L.append_entry(trail, actor=self.name, actor_kind="agent", event="ledger-sealed",
                       summary=f"migration {mig} complete; audit trail sealed")
        ok, why = L.verify_chain(trail)
        if not ok:  # pragma: no cover - append_entry always yields a valid chain
            raise TranslatorError(f"audit trail failed verification before sealing: {why}")
        state["sealed"] = {"at": L.now_iso(), "head_hash": trail[-1]["hash"], "entries": len(trail)}
        state["status"] = "sealed"
        self._save_ledger(state)
        decisions = {cid: rec["decision"] for cid, rec in state["decisions"].items()}
        self.ask_human(f"{MARK['ledger_sealed']} — Migration {mig} complete. Decisions: "
                       f"{format_decisions(decisions) if decisions else 'none needed'}; parity: "
                       f"{defuse(rep['parity_label'])} on rev `{rev}`; p99 legacy {bm['legacy']['p99_ms']:.1f} ms → modern "
                       f"{bm['modern']['p99_ms']:.1f} ms; cutover approved by {defuse(approver)}; audit trail "
                       f"{why} — head `sha256:{trail[-1]['hash']}`. Full record in the “{L.LEDGER_DOC}”.")

    def _check_approval(self, cut: dict, *, not_before_ms: int = 0) -> tuple[bool | None, str | None]:
        """Cross-check the approval message in the room history. Returns ``(verified, problem)``:
        a message that exists but was not the approver's ``approve-cutover`` (or was sent before ``not_before_ms``,
        epoch ms of the later of the build being cut over and its benchmark — an older approval approves neither)
        is a problem;
        one outside the readable history is unverified (``False``); an unreadable history ``None``. The caller
        refuses to seal unless ``verified is True``.

        The seal is final, so a transient ``read_context`` failure, or a history that does not show the approval
        yet, is retried (``approval_lookup_attempts`` reads, ``approval_lookup_delay`` s apart) before the approval
        is recorded as unverified (live-verified: event ``message_id`` == ``read_context`` id)."""
        mid = cut.get("approval_message_id")
        if not mid:
            return False, None
        verified: bool | None = None
        for attempt in range(1, self.approval_lookup_attempts + 1):
            try:
                found, problem = self._lookup_approval(mid, cut, not_before_ms=not_before_ms)
            except Exception as exc:  # noqa: BLE001 - history is a cross-check, not the source of truth
                self.log.warning("could not read the room history to verify the approval (read %d/%d): %s",
                                 attempt, self.approval_lookup_attempts, exc)
                verified = None
            else:
                if problem or found:
                    return found, problem
                self.log.warning("approval message %s not in the room history yet (read %d/%d)", mid, attempt,
                                 self.approval_lookup_attempts)
                verified = False
            if attempt < self.approval_lookup_attempts:
                time.sleep(self.approval_lookup_delay)
        return verified, None

    #: Messages per history page (Switch's maximum) and the most reads per fallback scan.
    HISTORY_PAGE = 500
    HISTORY_MAX_READS = 200
    #: Largest cursor overlap of the fallback scan, in ms (see ``_scan_history``).
    HISTORY_OVERLAP_MS = 5000

    def _lookup_approval(self, mid: str, cut: dict, *, not_before_ms: int = 0) -> tuple[bool, str | None]:
        """Find approval ``mid`` in the room history.

        Primary path: the cutover gate records the approval's own Switch timestamp (``approval_timestamp_ms``,
        its ``sent_at``), so ONE read of the window ending just after it suffices — Switch returns the
        highest-``seq`` rows with ``sent_at`` before the cursor, and the approval is among them unless more than
        a page of messages committed after it yet started before it. The recorded timestamp only narrows the
        search: the message found must still be the approver's own ``approve-cutover``. A record without that
        timestamp falls back to ``_scan_history``."""
        ts = cut.get("approval_timestamp_ms")
        before: str | None = None
        if isinstance(ts, int) and not isinstance(ts, bool):
            try:
                before = datetime.fromtimestamp((ts + 1) / 1000, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):  # a corrupt record: it may only narrow, never block
                self.log.warning("approval %s carries an unusable timestamp %r; scanning the room", mid, ts)
        if before is not None:
            ctx = self.switch.read_context(self.room.room_id, limit=self.HISTORY_PAGE, before=before)
            found, problem = self._find_approval(ctx, mid, cut, not_before_ms=not_before_ms)
            if problem or found:
                return found, problem
            self.log.warning("approval %s not in its recorded history window; scanning the room", mid)
        return self._scan_history(mid, cut, not_before_ms=not_before_ms)

    def _scan_history(self, mid: str, cut: dict, *, not_before_ms: int = 0) -> tuple[bool, str | None]:
        """Fallback: page back through the room history (newest first) until the approval is found or the history
        is exhausted (an approval older than ``not_before_ms`` is found and then refused by ``_find_approval``).

        Each cursor overlaps the previous page by an adaptive window (deduped by message id) capped at half the
        page's time span, so the cursor strictly decreases on every read: a page that adds nothing new narrows
        the window (down to a strict cursor), a page with new messages lets it widen again. Known limits, all failing closed (the approval
        is reported as not found and the seal is refused, never forged), because Switch pages by timestamp only
        and reports a page's MINIMUM ``sent_at`` (a transaction's START time): a row whose transaction started
        more than the overlap before rows committed earlier can drag the cursor past messages not yet read, and a
        run of more than ``HISTORY_PAGE`` messages within one millisecond cannot be traversed. Raises if a page
        cannot be read (the caller retries)."""
        before_ms: int | None = None
        seen: set[str] = set()
        overlap = self.HISTORY_OVERLAP_MS
        for _ in range(self.HISTORY_MAX_READS):
            if before_ms is None:
                ctx = self.switch.read_context(self.room.room_id, limit=self.HISTORY_PAGE)
            else:
                ctx = self.switch.read_context(self.room.room_id, limit=self.HISTORY_PAGE,
                                               before=datetime.fromtimestamp(before_ms / 1000,
                                                                             timezone.utc).isoformat())
            found, problem = self._find_approval(ctx, mid, cut, not_before_ms=not_before_ms)
            if problem or found:
                return found, problem
            ids = {str(m.get("id")) for t in ctx.get("threads", [])
                   for m in [t.get("root") or {}, *(t.get("replies") or [])] if m.get("id")}
            fresh = ids - seen
            seen |= ids
            oldest = ctx.get("oldest_timestamp")
            if not ctx.get("truncated") or not isinstance(oldest, (int, float)):
                return False, None  # the whole history has been read
            if before_ms is not None and oldest >= before_ms:
                # Switch only returns rows strictly before the cursor; anything else would let the cursor move
                # forward, so stop (fail closed) rather than trust the page.
                self.log.warning("history page oldest %s is not before the cursor %s; stopping", oldest, before_ms)
                return False, None
            if fresh:
                overlap = min(self.HISTORY_OVERLAP_MS, max(overlap * 2, 1))
            elif overlap == 0:
                return False, None  # even a strict cursor added nothing new
            else:
                overlap //= 4  # the window held only messages already read: narrow it
            if before_ms is not None:
                # Cap the overlap at half this page's time span: the cursor then always moves strictly backwards
                # (by at least half the span, so a dense region still advances ~half a page per read), and
                # repeated pages narrow the window instead of jumping past a partly read millisecond.
                overlap = min(overlap, (before_ms - int(oldest)) // 2)
            before_ms = int(oldest) + overlap
        self.log.warning("approval %s not found within %d history reads", mid, self.HISTORY_MAX_READS)
        return False, None

    @staticmethod
    def _find_approval(ctx: dict, mid: str, cut: dict, *, not_before_ms: int = 0) -> tuple[bool, str | None]:
        """``(found, problem)`` for approval message ``mid`` in one ``read_context`` result."""
        for thread in ctx.get("threads", []):
            for msg in [thread.get("root") or {}, *(thread.get("replies") or [])]:
                if mid and msg.get("id") == mid:
                    # The same grammar the cutover gate obeys: only a message whose FIRST word is the
                    # approve-cutover verb approves ("status — do NOT approve-cutover" does not).
                    try:
                        verb = (parse_cutover(str(msg.get("body", ""))) or Command("")).verb
                    except CommandError:
                        verb = ""
                    who = str(msg.get("sender_name") or "")
                    if who.lower() != str(cut.get("approved_by", "")).lower() or verb != "approve-cutover":
                        return False, (f"the approval message `{defuse(mid)}` was posted by {defuse(who)} and is not "
                                       f"an `approve-cutover` from {defuse(cut.get('approved_by'))}")
                    if not_before_ms:
                        ts = msg.get("timestamp")
                        if not isinstance(ts, (int, float)) or ts < not_before_ms:
                            floor = datetime.fromtimestamp(not_before_ms / 1000, timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%S.%fZ")
                            return False, (f"the approval message `{defuse(mid)}` (timestamp {defuse(ts)}) predates "
                                           f"the build or benchmark being cut over ({floor}) — an earlier approval "
                                           f"cannot approve a newer build or benchmark")
                    return True, None
        return False, None

    # ------------------------------------------------------------------ status
    def _status(self) -> str:
        try:
            state = self._load_ledger()
        except TranslatorError as exc:
            return f"{MARK['error']} {defuse(exc)}"
        head = f"ℹ️ translator ({self.mode} codegen) → `{self._modern_rel()}`, modern target {self.modern_url}."
        if state is None:
            pending = f" {len(self.pending_refusals)} governance refusal(s) pending." if self.pending_refusals else ""
            return f"{head} No migration yet.{pending}"
        mig = state["migration"]
        build = state["builds"][-1] if state["builds"] else None
        decisions = {cid: rec["decision"] for cid, rec in state["decisions"].items()}
        ok, why = L.verify_chain(state["trail"])
        return (f"{head} Migration {mig['id']} (`{defuse(mig['path'])}`): "
                f"{defuse(L.STATUS_BANNERS.get(state['status'], state['status']))}. "
                f"Builds: {len(state['builds'])}"
                + (f", current rev `{build['rev']}` ({build['mode']})" if build else "")
                + f". Decisions: {format_decisions(decisions) if decisions else 'none yet'}. "
                f"Governance refusals: {len(state['refusals'])}. Audit trail: {'intact' if ok else 'BROKEN'} ({why})"
                + (". Sealed." if state.get("sealed") else "."))

    def _modern_rel(self) -> str:
        try:
            return str(self.modern_lists_path.resolve().relative_to(ROOT))
        except ValueError:
            return str(self.modern_lists_path)


if __name__ == "__main__":
    main_for(TranslatorAgent)
