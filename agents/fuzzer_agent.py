"""@fuzzer — differential shadow tester of the legacy→FastAPI migration (docs/AGENTS_SPEC.md §2).

Run with ``python -m agents.fuzzer_agent``.

Commands it accepts (addressed with ``@fuzzer``):

* ``shadow-test rev=<rev> migration=<id>`` / ``re-verify rev=<rev> migration=<id> [— human decisions applied:
  preserve … and deprecate …]`` — from the ``translator`` agent or a human. The fuzzer

  1. waits (≤ 30 s) until ``MODERN_URL/_meta`` reports exactly ``rev`` (and no ``import_error``);
  2. on a migration id it has not seen, starts a fresh Parity Audit Matrix and resets the divergence
     :class:`~agents.divergence.Registry` (so the new migration's clusters are numbered ``D-1`` … again);
  3. reads the human decisions from the translator's ``Parity & Cutover Audit Ledger`` (the system of
     record) and, if the handoff itself carries decisions, insists they are identical;
  4. runs :func:`agents.divergence.run_campaign` (deterministic quirk corpus + Hypothesis GETs + POSTs),
     records the run in its own room document ``Parity Audit Matrix`` and posts the divergence table;
  5. not approved → asks the human which clusters to ``preserve`` / ``deprecate`` (naming the translator
     WITHOUT an @-mention); approved → posts ``✅ 100% Approved Parity (k/k)`` and hands off to
     ``cutover-gate`` with ``benchmark rev=<rev> migration=<id>``.

* ``status`` / ``help`` — anyone.

Anything else an agent says to the fuzzer is ignored (logged), so agents can never ping-pong. Every
problem that stops a campaign is reported in the room with a ``⚠️`` prefix — nothing fails silently.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
from pathlib import Path

import httpx

from agents import ledger
from agents.commands import (
    MARK,
    Command,
    CommandError,
    benchmark,
    format_decisions,
    normalize_id,
    parse_fuzzer,
)
from agents.divergence import (
    RESOLVED_STATUSES,
    CampaignError,
    CampaignReport,
    Registry,
    run_campaign,
)
from agents.runtime import LEGACY_URL, MODERN_URL, RoomAgent, Sender, main_for
from agents.switch_client import Event

#: The agent whose handoffs (and ledger) the fuzzer trusts besides humans.
TRANSLATOR = "translator"
#: The agent the fuzzer hands an approved build to.
CUTOVER_GATE = "cutover-gate"

#: Example verdict per known quirk tag, used ONLY to build the "e.g." line of a decision request.
#: It mirrors the "Demo decision" column of docs/SPEC.md §3: quirks that are part of the public
#: contract are preserved, legacy bugs (200 for a missing list, 500 on an empty POST) deprecated.
#: The human decides; unknown tags default to ``preserve`` in the example.
EXAMPLE_VERDICT = {
    "json_suffix": "preserve",
    "loose_query": "preserve",
    "missing_resource_200": "deprecate",
    "non_positive_quantity": "preserve",
    "empty_body_500": "deprecate",
}

#: Why an unresolved cluster with a decision still blocks parity.
_VIOLATION_HINT = {
    "preserved-regressed": "decided preserve, but the modern build still diverges from legacy",
    "deprecated-violated": "decided deprecate, but the modern build no longer returns the status "
                           "recorded at discovery",
}

HELP = (
    "ℹ️ fuzzer — differential shadow testing of legacy :9001 vs modern :9002.\n"
    "- `shadow-test rev=<rev> migration=<MIG-id>` — from the translator agent or a human: wait for the modern "
    "build `<rev>`, run the shadow campaign and publish it in the room document “Parity Audit Matrix”.\n"
    "- `re-verify rev=<rev> migration=<MIG-id>` — same, after the human decisions were applied (decisions are "
    "read from the translator's “Parity & Cutover Audit Ledger”).\n"
    "- `status` — the latest campaign; `help` — this text.\n"
    "Approved parity is handed to the cutover gate for benchmarking; otherwise the human lead is asked to "
    "decide which divergences to preserve or deprecate."
)


def _defuse_mentions(text: str) -> str:
    """Neutralise every ``@`` in text the fuzzer did not write itself (probe samples, titles from the
    registry), so a campaign table can never accidentally address a room participant."""
    return text.replace("@", "@⁠")  # U+2060 WORD JOINER breaks Switch's ``@name`` token


def _id_key(cluster_id: str) -> int:
    return int(cluster_id.split("-", 1)[1])


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write ``data`` as the divergence registry file (same format as :class:`Registry`), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".divergence_registry.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _matrix_problem(state: object) -> str | None:
    """Why ``state`` is not a usable matrix state (None if it is)."""
    if not isinstance(state, dict):
        return "it has no machine-readable state block"
    if state.get("kind") != "matrix" or not isinstance(state.get("runs"), list):
        return "its state is not a matrix (`kind`/`runs` missing)"
    for i, run in enumerate(state["runs"], start=1):
        report = run.get("report") if isinstance(run, dict) else None
        if not (isinstance(run, dict) and isinstance(run.get("run"), int) and isinstance(run.get("rev"), str)
                and isinstance(run.get("trigger"), dict) and isinstance(report, dict)
                and isinstance(report.get("clusters"), list)
                and all(isinstance(c, dict) and "id" in c and "status" in c for c in report["clusters"])):
            return f"run entry {i} is malformed"
    if "registry" in state and not isinstance(state["registry"], dict):
        return "its `registry` is not a mapping"
    return None


class FuzzerAgent(RoomAgent):
    """The ``@fuzzer`` room agent. Owns (and is the only writer of) the ``Parity Audit Matrix``."""

    name = "fuzzer"

    def __init__(self, *, legacy_url: str | None = None, modern_url: str | None = None,
                 registry_path: Path | str | None = None, n_random: int = 120, rev_timeout: float = 30.0,
                 rev_poll: float = 0.5, **kwargs):
        super().__init__(**kwargs)
        self.legacy_url = (legacy_url or LEGACY_URL).rstrip("/")
        self.modern_url = (modern_url or MODERN_URL).rstrip("/")
        self.registry = Registry(registry_path)  # None: <STRANGLER_STATE_DIR>/fuzzer/divergence_registry.json
        self.n_random = n_random
        self.rev_timeout = rev_timeout
        self.rev_poll = rev_poll
        self.matrix: dict = ledger.new_matrix()
        #: Migration id the current matrix (and registry numbering) belongs to; None = none yet.
        self.migration: str | None = None
        #: False until the matrix document was read successfully (or found absent/unusable).
        self._rehydrated = False

    def say(self, body: str) -> str:
        """Post ``body`` with every ``@`` defused: the fuzzer's plain posts never address anyone (handoffs
        and the decision request go through :meth:`handoff`)."""
        return super().say(_defuse_mentions(body))

    # ------------------------------------------------------------------ lifecycle
    def on_connected(self) -> None:
        """Rehydrate the matrix (and its migration id) from the ``Parity Audit Matrix`` room document.

        A failure to *read* the document (Switch error) is announced and retried before the next
        campaign, which is refused until rehydration succeeded — so a transient error can never make
        the fuzzer forget a migration and renumber its divergences."""
        try:
            self._rehydrate()
        except Exception as exc:  # noqa: BLE001 - announced; the next campaign retries and fails loud
            self.log.exception("rehydration failed")
            self.say(f"{MARK['error']} could not read “{ledger.MATRIX_DOC}” at startup "
                     f"(`{type(exc).__name__}: {str(exc)[:200]}`) — will retry before the next campaign.")

    def _rehydrate(self) -> None:
        doc = self._matrix_doc()
        if doc is None:
            self.log.info("no %r document yet — starting with an empty matrix", ledger.MATRIX_DOC)
            self._rehydrated = True
            return
        if doc.get("created_by_agent_id") != self.switch.agent_id:
            self.say(f"{MARK['error']} room document “{ledger.MATRIX_DOC}” was not created by the fuzzer "
                     f"(creator `{doc.get('created_by_agent_id')}`) — not trusting it; campaigns are refused "
                     f"until it is removed.")
            self._rehydrated = True
            return
        content = (self.switch.load_documents([doc["id"]]) or [{}])[0].get("content", "")
        try:
            state = ledger.extract_state(content)
        except ValueError as exc:
            state, why = None, f"its state block is not valid JSON ({exc})"
        else:
            why = _matrix_problem(state)
        if why:
            self.say(f"{MARK['error']} could not rehydrate “{ledger.MATRIX_DOC}”: {why} — starting a fresh "
                     f"matrix (the document is replaced by the next campaign).")
            self._rehydrated = True
            return
        self.matrix = state
        run = ledger.latest_run(state)
        self.migration = state.get("migration") or (run or {}).get("trigger", {}).get("migration")
        self._rehydrated = True
        self.log.info("rehydrated %s: %d run(s), migration %s", ledger.MATRIX_DOC, len(state["runs"]),
                      self.migration)
        self._reconcile_registry()

    def _matrix_doc(self) -> dict | None:
        return next((d for d in self.switch.documents() if d.get("name") == ledger.MATRIX_DOC), None)

    def _reconcile_registry(self) -> bool:
        """Make the on-disk divergence registry agree with the mapping committed in the matrix.

        The matrix document is the source of truth (it is what the translator and the human read): if
        the registry file lost or renumbered any of its clusters (deleted, corrupt, another process),
        restore the matrix's mapping and say so. Returns False (after explaining) if it cannot."""
        committed = self.matrix.get("registry")
        if not self.matrix.get("runs"):
            return True
        if not isinstance(committed, dict):
            self.say(f"{MARK['error']} “{ledger.MATRIX_DOC}” for migration {self.migration} carries no divergence "
                     f"registry, so the D-n numbering cannot be verified — no campaign run. Start a new migration "
                     f"(the translator's `migrate`) to renumber from scratch.")
            return False
        on_disk = self.registry.all()
        if ({tag: e.get("id") for tag, e in on_disk.items()}
                == {tag: e.get("id") for tag, e in committed.items() if isinstance(e, dict)}):
            return True
        _write_json_atomic(self.registry.path, committed)
        self.say(f"ℹ️ the divergence registry `{self.registry.path}` disagreed with “{ledger.MATRIX_DOC}” "
                 f"(migration {self.migration}) — restored its {len(committed)} cluster id(s) from the document.")
        return True

    # ------------------------------------------------------------------ dispatch
    def handle(self, event: Event, sender: Sender) -> None:
        trusted = sender.is_human or (sender.is_agent and sender.name.lower() == TRANSLATOR)
        try:
            cmd = parse_fuzzer(event.body)
        except CommandError as exc:
            if trusted:
                self.say(f"{MARK['error']} malformed command: {exc}. Try `help`.")
            else:
                self.log.info("ignoring malformed command from %s (%s): %s", sender.name, sender.kind, exc)
            return
        if cmd is None:
            if sender.is_human:
                self.say("ℹ️ I did not recognise that. Ask me for `help`.")
            else:
                self.log.info("ignoring chatter from %s (%s)", sender.name, sender.kind)
            return
        if cmd.verb == "help":
            self.say(HELP)
        elif cmd.verb == "status":
            self.say(self._status())
        elif not trusted:
            self.log.warning("ignoring %s from %s (%s): only the translator or a human may trigger a campaign",
                             cmd.verb, sender.name, sender.kind)
        else:
            self._campaign(cmd, event, sender)

    # ------------------------------------------------------------------ campaign
    def _campaign(self, cmd: Command, event: Event, sender: Sender) -> None:
        rev, mig = cmd.args.get("rev"), cmd.args.get("migration")
        if not rev or not mig:
            self.say(f"{MARK['error']} `{cmd.verb}` needs both the build and the migration, e.g. "
                     f"`{cmd.verb} rev=<12-hex rev> migration=MIG-<id>` — no campaign run.")
            return
        if not self._rehydrated:
            self._rehydrate()  # raises (→ ⚠️ via dispatch) if the matrix still cannot be read
        latest = ledger.latest_run(self.matrix)
        if event.message_id and latest and latest["trigger"].get("message_id") == event.message_id:
            self.say(f"ℹ️ request `{event.message_id}` was already handled by campaign #{latest['run']} on rev "
                     f"`{latest['rev']}` (redelivered event) — not running it twice.")
            return
        doc = self._matrix_doc()
        if doc is not None and doc.get("created_by_agent_id") != self.switch.agent_id:
            self.say(f"{MARK['error']} room document “{ledger.MATRIX_DOC}” was created by "
                     f"`{doc.get('created_by_agent_id')}`, not the fuzzer — refusing to run a campaign whose "
                     f"results could not be recorded.")
            return
        if not self._wait_for_rev(rev):
            return
        from_ledger = self._ledger_decisions(mig, rev, cmd.args.get("decisions"))
        if from_ledger is None:
            return
        decisions, decision_tags = from_ledger

        fresh = mig != self.migration
        if fresh:
            # Number the new migration's clusters in a staging registry; it replaces the committed one only
            # once the matrix recording this run is written, so a failure leaves the old migration intact.
            registry = Registry(self.registry.path.with_name(self.registry.path.name + ".staging"))
            registry.reset()
            state = ledger.new_matrix()
            state["migration"] = mig
            self.log.info("new migration %s (was %s): fresh matrix and registry", mig, self.migration)
        else:
            if not self._reconcile_registry():
                return
            registry = self.registry
            state = copy.deepcopy(self.matrix)
        try:
            with self.working():
                try:
                    report = asyncio.run(run_campaign(
                        self.legacy_url, self.modern_url, {cid: {"decision": d} for cid, d in decisions.items()},
                        n_random=self.n_random, registry=registry))
                except CampaignError as exc:
                    self.say(f"{MARK['error']} shadow campaign on rev `{rev}` could not run: {exc} — are both "
                             f"targets up (`scripts/targets.sh status`)?")
                    return
            problems = self._verify_campaign(report, rev, decisions, decision_tags)
            if problems:
                self.say(f"{MARK['error']} shadow campaign on rev `{rev}` (migration {mig}) is not recorded and "
                         f"not approved: " + "; ".join(problems) + ".")
                return
            trigger = {"by": sender.name, "kind": sender.kind, "command": cmd.verb, "migration": mig,
                       "message_id": event.message_id}
            run = ledger.add_run(state, rev=rev, trigger=trigger, decisions=decisions, report=report.to_dict())
            state["registry"] = registry.all()
            description, instructions = ledger.DOC_META[ledger.MATRIX_DOC]
            self.switch.upsert_document(name=ledger.MATRIX_DOC, description=description,
                                        instructions=instructions, content=ledger.render_matrix(state))
            if fresh:
                _write_json_atomic(self.registry.path, state["registry"])
            self.matrix, self.migration = state, mig
        finally:
            if fresh:
                registry.reset()  # drop the staging file whatever happened

        self.say(
            f"{MARK['campaign']} #{run['run']} on rev `{rev}`: {report.total_requests} requests "
            f"(deterministic quirk corpus + {self.n_random} generated GETs fired concurrently + sequential POSTs) "
            f"against legacy and modern — migration {mig}"
            f"{' (new migration: divergence ids restart at D-1)' if fresh else ''}, decisions applied: "
            f"{format_decisions(decisions) if decisions else 'none yet'}. Full matrix in the room document "
            f"“{ledger.MATRIX_DOC}”.\n\n{report.to_markdown()}")
        if report.approved_parity:
            self._approve(report, rev, mig, decisions)
        else:
            self._request_decisions(report, rev)

    def _approve(self, report: CampaignReport, rev: str, mig: str, decisions: dict[str, str]) -> None:
        line = f"✅ {report.parity_label}"
        if not line.startswith(MARK["approved_parity"]):
            raise RuntimeError(f"parity label {report.parity_label!r} does not carry the approved-parity marker")
        self.say(f"{line} on rev `{rev}` — {report.total_requests} shadow requests; every divergence cluster is "
                 f"resolved by the human decisions ({format_decisions(decisions) if decisions else 'none needed'}). "
                 f"Handing over to the cutover gate for the load benchmark.")
        self.handoff(CUTOVER_GATE, benchmark(rev, mig))

    def _request_decisions(self, report: CampaignReport, rev: str) -> None:
        unresolved = sorted((c for c in report.clusters if not c.resolved), key=lambda c: _id_key(c.id))
        ids = " ".join(c.id for c in unresolved)
        example = format_decisions({c.id: EXAMPLE_VERDICT.get(
            c.tag, "deprecate" if c.tag.startswith("unclassified:") else "preserve") for c in unresolved})
        notes = [f"{c.id} ({c.status}): {_VIOLATION_HINT[c.status]}" for c in unresolved
                 if c.status in _VIOLATION_HINT]
        body = (f"{MARK['decision_needed']} on {ids} (rev `{rev}`, {report.parity_label}): "
                f"preserve = keep the legacy behaviour in FastAPI (it is part of the contract); "
                f"deprecate = adopt the modern behaviour (the legacy quirk was a bug). "
                + (" ".join(f"Note — {n}." for n in notes) + " " if notes else "")
                + f"Reply to the translator with e.g. `{example}`.")
        self.ask_human(_defuse_mentions(body))

    # ------------------------------------------------------------------ preconditions
    def _wait_for_rev(self, rev: str) -> bool:
        """Poll ``/_meta`` until it serves ``rev`` without an import error, within ``rev_timeout`` seconds
        of wall-clock time (each request and sleep is clamped to the remaining budget); announce failure."""
        deadline = time.monotonic() + self.rev_timeout
        seen: str = "nothing yet"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.say(f"{MARK['error']} modern {self.modern_url} did not serve rev `{rev}` within "
                         f"{self.rev_timeout:g} s (last seen: {seen}) — no campaign run.")
                return False
            try:
                meta = httpx.get(f"{self.modern_url}/_meta", timeout=max(0.05, min(5.0, remaining))).json()
            except (httpx.HTTPError, ValueError) as exc:
                seen = f"no answer ({type(exc).__name__}: {exc})"
            else:
                served = str(meta.get("rev") or "").lower() if isinstance(meta, dict) else ""
                if served == rev.lower():
                    if meta.get("import_error"):
                        self.say(f"{MARK['error']} modern {self.modern_url} serves rev `{rev}` but it failed to "
                                 f"import: `{str(meta['import_error'])[:300]}` — no campaign run.")
                        return False
                    return True
                seen = f"rev `{served or None}`"
            time.sleep(max(0.0, min(self.rev_poll, deadline - time.monotonic())))

    def _ledger_decisions(self, mig: str, rev: str,
                          carried: dict[str, str] | None) -> tuple[dict[str, str], dict[str, str]] | None:
        """The human decisions recorded in the translator's ledger for migration ``mig``, as
        ``({id: verdict}, {id: tag the human decided on})``.

        Returns None (after explaining why in the room) when the ledger is untrustworthy, belongs to
        another migration, does not list build ``rev``, holds decisions not made by a human, or
        disagrees with the decisions carried by the handoff."""
        doc = next((d for d in self.switch.documents() if d.get("name") == ledger.LEDGER_DOC), None)
        if doc is None:
            if carried:
                self.say(f"{MARK['error']} the handoff carries decisions ({format_decisions(carried)}) but there is "
                         f"no “{ledger.LEDGER_DOC}” to verify them against — no campaign run.")
                return None
            self.say(f"ℹ️ no “{ledger.LEDGER_DOC}” in the room yet — running migration {mig} without decisions.")
            return {}, {}
        translator_id = self.room.agents.get(TRANSLATOR)
        if not translator_id or doc.get("created_by_agent_id") != translator_id:
            self.say(f"{MARK['error']} “{ledger.LEDGER_DOC}” was not created by the translator agent (creator "
                     f"`{doc.get('created_by_agent_id')}`) — refusing to take decisions from it; no campaign run.")
            return None
        content = (self.switch.load_documents([doc["id"]]) or [{}])[0].get("content", "")
        try:
            state = ledger.extract_state(content)
        except ValueError as exc:
            state = None
            self.log.error("ledger state is not valid JSON: %s", exc)
        if not isinstance(state, dict):
            self.say(f"{MARK['error']} “{ledger.LEDGER_DOC}” has no readable state block — no campaign run.")
            return None
        ledger_mig = (state.get("migration") or {}).get("id")
        if ledger_mig != mig:
            self.say(f"{MARK['error']} the handoff is for migration {mig} but the ledger records migration "
                     f"{ledger_mig} — no campaign run.")
            return None
        builds = [b.get("rev") for b in state.get("builds") or [] if isinstance(b, dict)]
        if builds and rev.lower() not in {str(b).lower() for b in builds}:
            self.say(f"{MARK['error']} rev `{rev}` is not a build recorded in the ledger for migration {mig} "
                     f"(builds: {', '.join(f'`{b}`' for b in builds)}) — no campaign run.")
            return None
        if builds and rev.lower() != str(builds[-1]).lower():
            # Only the latest build can be promoted: the cutover gate benchmarks/flips what the fuzzer
            # approves, and the translator seals only its latest build (record-cutover).
            self.say(f"{MARK['error']} rev `{rev}` is an older build of migration {mig}; the ledger's latest build "
                     f"is `{builds[-1]}` — parity is only verified on the latest build (is the modern target "
                     f"serving a stale module?). No campaign run.")
            return None
        raw = state.get("decisions")
        if not isinstance(raw, dict):
            self.say(f"{MARK['error']} the ledger's `decisions` field is missing or not a mapping "
                     f"({type(raw).__name__}) — no campaign run.")
            return None
        decisions: dict[str, str] = {}
        tags: dict[str, str] = {}
        for key, rec in raw.items():
            try:
                cid = normalize_id(str(key))
            except CommandError as exc:
                self.say(f"{MARK['error']} the ledger's decisions are malformed ({exc}) — no campaign run.")
                return None
            verdict = str(rec.get("decision", "") if isinstance(rec, dict) else "").lower()
            if verdict not in ("preserve", "deprecate"):
                self.say(f"{MARK['error']} the ledger holds invalid decisions {{{cid!r}: {verdict!r}}} — "
                         f"no campaign run.")
                return None
            if decisions.get(cid, verdict) != verdict:
                self.say(f"{MARK['error']} the ledger records {cid} as both preserve and deprecate — "
                         f"no campaign run.")
                return None
            by = str(rec.get("by") or "")
            if not by or not self.classify(by).is_human:
                self.say(f"{MARK['error']} the ledger's decision on {cid} is attributed to "
                         f"`{by or 'nobody'}`, not a human room member — only the human lead decides; "
                         f"no campaign run.")
                return None
            decisions[cid] = verdict
            if rec.get("tag"):
                tags[cid] = str(rec["tag"])
        decisions = dict(sorted(decisions.items(), key=lambda kv: _id_key(kv[0])))
        if carried is not None and carried != decisions:
            self.say(f"{MARK['error']} decisions mismatch: the handoff says "
                     f"`{format_decisions(carried)}` but the ledger records "
                     f"`{format_decisions(decisions) if decisions else 'no decisions'}` — no campaign run.")
            return None
        return decisions, tags

    def _verify_campaign(self, report: CampaignReport, rev: str, decisions: dict[str, str],
                         tags: dict[str, str]) -> list[str]:
        """Reasons the finished campaign must not be recorded/approved (empty list = fine)."""
        problems = []
        try:
            meta = httpx.get(f"{self.modern_url}/_meta", timeout=5).json()
            served = str(meta.get("rev") or "").lower()
            if served != rev.lower():
                problems.append(f"modern switched to rev `{served or None}` during the campaign")
            elif meta.get("import_error"):
                problems.append("modern reports an import error after the campaign")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            problems.append(f"could not re-check modern /_meta after the campaign ({type(exc).__name__})")
        by_id = {c.id: c for c in report.clusters}
        stray = sorted(set(decisions) - set(by_id), key=_id_key)
        if stray:
            problems.append(f"the ledger decides {' '.join(stray)}, which this campaign has never seen")
        wrong = [f"{cid} is `{by_id[cid].tag}` here but the human decided on `{tag}`"
                 for cid, tag in sorted(tags.items(), key=lambda kv: _id_key(kv[0]))
                 if cid in by_id and by_id[cid].tag != tag]
        if wrong:
            problems.append("divergence ids no longer match the decisions (" + "; ".join(wrong) + ")")
        return problems

    # ------------------------------------------------------------------ status
    def _status(self) -> str:
        if not self._rehydrated:
            try:
                self._rehydrate()
            except Exception as exc:  # noqa: BLE001 - reported in the reply
                return (f"{MARK['error']} fuzzer status unknown — “{ledger.MATRIX_DOC}” could not be read "
                        f"(`{type(exc).__name__}: {str(exc)[:200]}`); campaigns are refused until it can.")
        run = ledger.latest_run(self.matrix)
        if run is None:
            return (f"ℹ️ fuzzer status — no campaign yet (migration {self.migration or 'none'}); "
                    f"legacy {self.legacy_url}, modern {self.modern_url}.")
        rep = run["report"]
        unresolved = [f"{c['id']} ({c['status']})" for c in rep["clusters"]
                      if c["status"] not in RESOLVED_STATUSES]
        return (
            f"ℹ️ fuzzer status — migration {self.migration}; latest campaign #{run['run']} on rev `{run['rev']}` at "
            f"{run['at']}: {rep['parity_label']}, {rep['total_requests']} requests, {rep['divergent']} divergent; "
            f"unresolved: {', '.join(unresolved) or 'none'}. Matrix: room document “{ledger.MATRIX_DOC}”.")


if __name__ == "__main__":
    main_for(FuzzerAgent)
