"""Unit tests for scripts/run_e2e_demo.py — matchers, assertion helpers and the full 8-step flow,
played against tests/fake_mattermost.py with scripted bot replies, a fake Gateway and an
httpx.MockTransport standing in for the proxy and the Switch probe endpoint. No live stack, no agents."""
from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from agents.commands import MARK, benchmark, parse_cutover, parse_fuzzer, re_verify, record_cutover, shadow_test
from agents.cutover_agent import BENCH_PATHS
from agents.ledger import LEDGER_DOC, SCORECARD_DOC, append_entry, embed_state, new_ledger, new_scorecard
from agents.mattermost import MattermostClient
from tests.fake_mattermost import FakeMattermost
from tests.procs import free_port

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_e2e_demo", ROOT / "scripts" / "run_e2e_demo.py")
demo = importlib.util.module_from_spec(_spec)
sys.modules["run_e2e_demo"] = demo
_spec.loader.exec_module(demo)

MIG = "MIG-20261105-120000"
REV1, REV2 = "aaaaaaaaaaa1", "bbbbbbbbbbb2"
ROOM_ID = "room-1"
FUZZER_ID, FUZZER_KEY = "fuzzer-id", "fuzzer-key"
TRANSLATOR_ID = "translator-id"
CUTOVER_ID = "cutover-gate-id"
SWITCH, PROXY = "http://switch.test", "http://proxy.test"
DECISIONS = {"D-1": "preserve", "D-2": "preserve", "D-3": "deprecate", "D-4": "preserve", "D-5": "deprecate"}


# ----------------------------------------------------------------------------------- text helpers

def test_has_mark_emoji_prefix_tolerates_variation_selector_and_leading_mentions():
    assert demo.has_mark("🛠️ v1 deployed to :9002 — rev `abc123abc123`", MARK["v1_deployed"])
    assert demo.has_mark("🛠 v1 deployed to :9002", MARK["v1_deployed"])            # no VS16
    assert demo.has_mark("@user 🧾 Ledger sealed — Migration X", MARK["ledger_sealed"])
    assert not demo.has_mark("note: 🛠️ v1 deployed", MARK["v1_deployed"])           # emoji marks must lead
    assert not demo.has_mark("🛠️ v2 deployed", MARK["v1_deployed"])


def test_has_mark_plain_phrases_match_anywhere():
    assert demo.has_mark("@user Decision needed on D-1", MARK["decision_needed"])
    assert demo.has_mark("@user approval needed: reply to the cutover gate", MARK["approval_needed"])
    assert not demo.has_mark("@user all good", MARK["approval_needed"])


def test_doc_notices_are_recognised():
    assert demo.is_doc_notice("📄 Parity & Cutover Audit Ledger was updated")
    assert demo.is_doc_notice("@user 📄 doc created")
    assert not demo.is_doc_notice("🧭 Migration MIG-1 accepted")


def test_rev_migration_and_id_extraction():
    assert demo.find_rev("shadow-test rev=ABCDEF123456 migration=MIG-1") == "abcdef123456"
    assert demo.find_rev("🛠️ v1 deployed to :9002 — rev `0123456789ab` …") == "0123456789ab"
    assert demo.find_rev("📊 Benchmark — rev **`0123456789ab`**") == "0123456789ab"
    assert demo.find_rev("🔧 Applied … v1 was rev `111111111111`; v2 deployed — rev `222222222222`",
                         after="v2 deployed") == "222222222222"
    assert demo.find_rev("no revision here") is None
    assert demo.find_rev("🔧 Applied … rev `111111111111`", after="v2 deployed") is None  # no silent fallback
    # has_mark() tolerates a missing emoji variation selector, so the rev boundary must too
    no_vs16 = MARK["v1_deployed"].replace("\ufe0f", "")
    assert no_vs16 != MARK["v1_deployed"]
    assert demo.find_rev(f"{no_vs16} to :9002 — rev `0123456789ab` …", after=MARK["v1_deployed"]) == "0123456789ab"
    assert demo.find_rev(f"{MARK['v1_deployed']} to :9002 — rev `0123456789ab`", after=no_vs16) == "0123456789ab"
    assert demo.find_migration(f"🧭 Migration **{MIG}** accepted from @user") == MIG
    assert demo.table_ids("| D-3 | x |\n| **d-1** | y |\n| D-10 | z |\nD-3 again") == ["D-1", "D-3", "D-10"]
    assert demo.table_ids("🔬 … (new migration: divergence ids restart at D-1)\n| D-2 | x |") == ["D-2"]


def _post(username: str, message: str, pid: str = "p", create_at: int = 1) -> dict:
    return {"id": pid, "username": username, "message": message, "create_at": create_at}


def test_handoff_matcher_uses_the_receiving_agents_parser():
    exp = demo.handoff("h", "translator", "fuzzer", "shadow-test", parse_fuzzer)
    assert exp.matches(_post("translator", "@fuzzer " + shadow_test(REV1, MIG)))
    assert not exp.matches(_post("fuzzer", "@fuzzer " + shadow_test(REV1, MIG)))         # wrong author
    assert not exp.matches(_post("translator", shadow_test(REV1, MIG)))                  # not addressed
    assert not exp.matches(_post("translator", "@fuzzer " + re_verify(REV1, MIG, DECISIONS)))  # wrong verb
    bad = demo.handoff("r", "translator", "fuzzer", "re-verify", parse_fuzzer)
    assert not bad.matches(_post("translator", "@fuzzer re-verify rev=abcdef — human decisions applied: D-1 preserve"))  # malformed
    bench = demo.handoff("b", "fuzzer", "cutover-gate", "benchmark", parse_cutover)
    assert bench.matches(_post("fuzzer", "@cutover-gate " + benchmark(REV2, MIG)))


def test_marked_skips_document_notices_and_checks_addressee():
    exp = demo.marked("v1", "translator", MARK["v1_deployed"])
    assert not exp.matches(_post("translator", "📄 🛠️ v1 deployed (document notice)"))
    ask = demo.marked("d", "fuzzer", MARK["decision_needed"], to="user")
    assert ask.matches(_post("fuzzer", "@user decision needed on D-1"))
    assert not ask.matches(_post("fuzzer", "decision needed on D-1"))


def test_match_posts_gives_each_expectation_its_own_post():
    exps = [demo.marked("a", "fuzzer", MARK["campaign"]), demo.marked("b", "fuzzer", MARK["campaign"])]
    one = [_post("fuzzer", "🔬 Shadow campaign #1", "p1")]
    assert set(demo.match_posts(one, exps)) == {"a"}
    two = one + [_post("fuzzer", "🔬 Shadow campaign #2", "p2", 2)]
    got = demo.match_posts(two, exps)
    assert got["a"]["id"] == "p1" and got["b"]["id"] == "p2"


def test_agent_failures_split_fatal_and_announced_fallbacks():
    posts = [_post("translator", "⚠️ LLM synthesis rejected: no router — used deterministic codegen", "1"),
             _post("fuzzer", "⚠️ fuzzer failed while handling …", "2"),
             _post("user", "⚠️ my own note", "3")]
    fatal, tolerated = demo.agent_failures(posts)
    assert [p["id"] for p in fatal] == ["2"] and [p["id"] for p in tolerated] == ["1"]


# ----------------------------------------------------------------------------------- ledger checks

def sealed_ledger(*, human: str = "user", decided_by: str | None = None) -> dict:
    state = new_ledger(migration_id=MIG, path="/people/{user}/lists", source="legacy_openlibrary", target="FastAPI",
                       requested_by=human, request_message_id="m0")
    trail = state["trail"]
    append_entry(trail, actor=human, actor_kind="human", event="migration-requested", summary="requested")
    state["decisions"] = {cid: {"decision": d, "tag": cid.lower(), "by": decided_by or human, "message_id": "m1",
                                "at": "2026-11-05T12:00:00Z"} for cid, d in DECISIONS.items()}
    for cid, d in DECISIONS.items():
        append_entry(trail, actor=human, actor_kind="human", event="decision", summary=f"{d} {cid}")
    state["parity"] = {"label": "100% Approved Parity (5/5)", "rev": REV2, "total_requests": 150, "run": 2,
                       "at": "t", "evidence": "e" * 64}
    side = {"p50_ms": 4.0, "p95_ms": 8.0, "p99_ms": 12.5, "throughput_rps": 900.0, "errors": 0, "total": 500,
            "concurrency": 50, "ok": 500, "status_counts": {"200": 500}}
    state["benchmark"] = {"rev": REV2, "legacy": dict(side, p99_ms=40.2), "modern": side, "total": 500,
                          "concurrency": 50, "paths": list(BENCH_PATHS), "at": "t", "evidence": "f" * 64,
                          "compare": {}}
    state["cutover"] = {"approved_by": human, "approved_at": "t", "approval_message_id": "m2",
                        "proxy_target": "modern", "verified": True, "approval_message_verified": True}
    append_entry(trail, actor=human, actor_kind="human", event="cutover-approved", summary="approved",
                 source_message_id="m2")
    append_entry(trail, actor="translator", actor_kind="agent", event="ledger-sealed", summary="sealed")
    state["status"] = "sealed"
    state["sealed"] = {"at": "t", "head_hash": trail[-1]["hash"], "entries": len(trail)}
    return state


def test_check_ledger_accepts_a_sealed_ledger():
    checks = demo.check_ledger(sealed_ledger(), migration=MIG, rev=REV2, human="user")
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert len(checks) == 9
    announced = demo.check_ledger(sealed_ledger(), migration=MIG, rev=REV2, human="user", approval_message_id="m2")
    assert all(c.ok for c in announced), [c for c in announced if not c.ok]


def _reseal(state: dict) -> None:
    """Re-chain and re-seal ``state`` after a test edited its trail, so only the intended check fails."""
    trail = [dict(e) for e in state["trail"]]
    state["trail"] = []
    for e in trail:
        append_entry(state["trail"], actor=e["actor"], actor_kind=e["actor_kind"], event=e["event"],
                     summary=e["summary"], evidence=e["evidence"], source_message_id=e["source_message_id"],
                     at=e["at"])
    state["sealed"] = {"at": "t", "head_hash": state["trail"][-1]["hash"], "entries": len(state["trail"])}


def _approval_entry(state: dict) -> dict:
    return next(e for e in state["trail"] if e["event"] == "cutover-approved")


@pytest.mark.parametrize("mutate", [
    lambda s: s["cutover"].update(approval_message_verified=False),   # not found in the room history
    lambda s: s["cutover"].update(approval_message_verified=None),    # history unreadable
    lambda s: s["cutover"].pop("approval_message_verified"),
    lambda s: s["cutover"].update(approval_message_id=None),
    lambda s: (_approval_entry(s).update(source_message_id="m-other"), _reseal(s)),
    lambda s: (_approval_entry(s).update(actor="fuzzer", actor_kind="agent"), _reseal(s)),
    lambda s: (s["trail"].remove(_approval_entry(s)), _reseal(s)),
])
def test_check_ledger_requires_the_verified_human_approval_message(mutate):
    state = sealed_ledger()
    mutate(state)
    bad = {c.name for c in demo.check_ledger(state, migration=MIG, rev=REV2, human="user") if not c.ok}
    assert bad == {"approval message verified in the room history"}


def test_check_ledger_rejects_an_approval_other_than_the_announced_one():
    bad = {c.name for c in demo.check_ledger(sealed_ledger(), migration=MIG, rev=REV2, human="user",
                                             approval_message_id="m-announced") if not c.ok}
    assert bad == {"approval message verified in the room history"}


WORKLOAD = (f"benchmark workload {demo.BENCH_TOTAL} requests at concurrency {demo.BENCH_CONCURRENCY} per target "
            f"over the {len(BENCH_PATHS)} contract endpoints, all answered 2xx")


@pytest.mark.parametrize("mutate, failing", [
    (lambda s: s["trail"][1].update(summary="forged"), "audit chain verifies"),
    (lambda s: s["decisions"]["D-3"].update(decision="preserve"), "5 decisions by user"),
    (lambda s: s["decisions"].pop("D-5"), "5 decisions by user"),
    (lambda s: s.update(parity=dict(s["parity"], rev=REV1)), "parity approved"),
    (lambda s: s.update(parity=dict(s["parity"], label="80% parity (4/5)")), "parity approved"),
    (lambda s: s.update(benchmark=None), "benchmark p99s recorded"),
    (lambda s: s["benchmark"].update(total=50), WORKLOAD),
    (lambda s: s["benchmark"].update(concurrency=1), WORKLOAD),
    (lambda s: s["benchmark"]["modern"].update(total=499), WORKLOAD),
    (lambda s: s["benchmark"].update(paths=list(BENCH_PATHS)[:1]), WORKLOAD),
    # every request failed: total is the requested count and the p99 is still positive
    (lambda s: s["benchmark"]["modern"].update(ok=0, errors=500, status_counts={"504": 500}), WORKLOAD),
    (lambda s: s["benchmark"]["legacy"].update(ok=499, errors=1, status_counts={"200": 499, "500": 1}), WORKLOAD),
    (lambda s: s["benchmark"]["modern"].update(status_counts={"200": 499, "404": 1}), WORKLOAD),
    (lambda s: s["benchmark"]["modern"].update(concurrency=1), WORKLOAD),
    (lambda s: s["cutover"].update(approved_by="fuzzer"), "cutover approved by user"),
    (lambda s: s["cutover"].update(proxy_target="legacy"), "cutover approved by user"),
    (lambda s: s.update(sealed=None), "ledger sealed"),
    (lambda s: s.update(status="cut-over"), "ledger sealed"),
    (lambda s: s["migration"].update(id="MIG-OLD"), "ledger is for this migration"),
])
def test_check_ledger_rejects(mutate, failing):
    state = sealed_ledger()
    mutate(state)
    bad = {c.name for c in demo.check_ledger(state, migration=MIG, rev=REV2, human="user") if not c.ok}
    assert failing in bad


def test_check_ledger_rejects_decisions_by_an_agent_and_missing_state():
    bad = {c.name for c in demo.check_ledger(sealed_ledger(decided_by="translator"), migration=MIG, rev=REV2,
                                             human="user") if not c.ok}
    assert bad == {"5 decisions by user"}
    assert not demo.check_ledger(None, migration=MIG, rev=REV2, human="user")[0].ok


def test_find_ledger_document_requires_one_room_document_by_the_translator():
    doc = {"id": "d1", "name": LEDGER_DOC, "scope": "room", "created_by_agent_id": TRANSLATOR_ID}
    other = {"id": "x", "name": "Parity Audit Matrix", "scope": "room", "created_by_agent_id": FUZZER_ID}
    assert demo.find_ledger_document([other, doc], translator_id=TRANSLATOR_ID) is doc
    with pytest.raises(AssertionError):
        demo.find_ledger_document([other], translator_id=TRANSLATOR_ID)
    with pytest.raises(AssertionError):
        demo.find_ledger_document([other, doc, dict(doc, id="d2")], translator_id=TRANSLATOR_ID)
    with pytest.raises(AssertionError, match="created by the translator"):
        demo.find_ledger_document([dict(doc, created_by_agent_id=FUZZER_ID)], translator_id=TRANSLATOR_ID)
    with pytest.raises(AssertionError, match="scope='global'"):
        demo.find_ledger_document([dict(doc, scope="global")], translator_id=TRANSLATOR_ID)


# -------------------------------------------------------------------------------- agents live check

class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class StatusGateway:
    def __init__(self, sequence):
        self.sequence = list(sequence)

    def room(self, room_id):
        statuses = self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
        return {"id": room_id, "agent_statuses": statuses}


def test_wait_agents_live_polls_until_all_live():
    agents = {"translator": "t", "fuzzer": "f", "cutover-gate": "c"}
    gw = StatusGateway([{"t": "live", "f": "disconnected", "c": "live"}, {"t": "live", "f": "live", "c": "live"}])
    clock = FakeClock()
    assert demo.wait_agents_live(gw, "r", agents, timeout=10, sleep=clock.sleep, clock=clock) == {
        "translator": "live", "fuzzer": "live", "cutover-gate": "live"}


def test_wait_agents_live_names_the_laggards():
    gw = StatusGateway([{"t": "live", "f": "disconnected"}])
    clock = FakeClock()
    with pytest.raises(demo.PreflightError) as ei:
        demo.wait_agents_live(gw, "r", {"translator": "t", "fuzzer": "f", "cutover-gate": "c"}, timeout=5,
                              sleep=clock.sleep, clock=clock)
    assert "fuzzer=disconnected" in str(ei.value) and "cutover-gate=not in room" in str(ei.value)
    assert "translator" not in str(ei.value).split("after")[1].split("—")[0]


# ------------------------------------------------------------------------------ scripted full flow

@dataclass
class Script:
    """Scripted agent behaviour; flags inject the failure modes under test."""

    ids: tuple[str, ...] = ("D-1", "D-2", "D-3", "D-4", "D-5")
    gate_refuses_probe: bool = True
    gate_flips_on_probe: bool = False
    translator_crashes: bool = False
    decision_mentions_translator: bool = False
    silent_after_migrate: bool = False
    handoff_bridged_late: bool = False  # bridge posts the handoff after the receiver already answered
    seal_ledger: bool = True
    ledger_creator: str = TRANSLATOR_ID
    gate_posts_cutover_on_probe: bool = False
    second_decision_request: bool = False
    warn_after_seal: bool = False
    stale_card: bool = False
    announced_approval: str | None = "m2"  # id the 🚦 post names (the ledger holds "m2"); None = names none
    record_refusal: bool = True     # the gate records the probe's refusal in its Cutover Scorecard
    refusal_message_id: str | None = None  # record the refusal under another message id than the probe's
    rollback_after_seal: bool = False  # a rollback lands during the final settle
    probe_response: dict = field(default_factory=lambda: {"ok": True, "event_id": "e1"})
    proxy_target: str = "legacy"
    documents: dict = field(default_factory=dict)
    probes: list = field(default_factory=list)


class World:
    """FakeMattermost + scripted bots + fake Gateway + MockTransport (proxy and Switch)."""

    def __init__(self, fake: FakeMattermost, script: Script):
        self.fake, self.s = fake, script
        for name in ("translator", "fuzzer", "cutover-gate"):
            fake.add_user(name, is_bot=True)
        fake.add_user("user", "pw")
        team = fake.add_team("switch")
        self.channel = fake.add_channel(team["id"], "strangler-migration")
        fake.on_post = self.on_user_post

    # bots ---------------------------------------------------------------------------------
    def bot(self, name: str, message: str) -> None:
        self.fake.add_post(self.channel["id"], name, message)

    def on_user_post(self, fake, post):
        text, s = post["message"], self.s
        if text.startswith("@translator migrate"):
            if s.translator_crashes:
                self.bot("translator", "⚠️ translator failed while handling “migrate”: `RuntimeError: boom`")
                return
            if s.silent_after_migrate:
                return
            self.bot("translator", f"🧭 Migration {MIG} accepted from @user — 5 legacy routes found")
            self.bot("translator", "📄 Document “Parity & Cutover Audit Ledger” created")
            self.bot("translator", f"🛠️ v1 deployed to :9002 — rev `{REV1}` (deterministic codegen)")
            if not s.handoff_bridged_late:
                self.bot("translator", "@fuzzer " + shadow_test(REV1, MIG))
            rows = "\n".join(f"| {cid} | quirk | 200 | 404 | 3 | `/x` | — / open |" for cid in s.ids)
            self.bot("fuzzer", f"🔬 Shadow campaign #1 on rev `{REV1}`: 150 requests — migration {MIG} "
                               f"(new migration: divergence ids restart at D-1)\n\n**60% parity**\n\n{rows}")
            extra = " @translator" if s.decision_mentions_translator else ""
            self.bot("fuzzer", "@user decision needed on " + ", ".join(s.ids) + ": preserve = keep the legacy "
                     f"contract; deprecate = drop it. Reply to the translator{extra} with e.g. "
                     "`preserve D-1 D-2 D-4 and deprecate D-3 D-5`.")
            if s.handoff_bridged_late:
                self.bot("translator", "@fuzzer " + shadow_test(REV1, MIG))
        elif text.startswith("@translator preserve"):
            self.bot("translator", f"🔧 Applied human decisions from @user: … v2 deployed to :9002 — rev `{REV2}`")
            self.bot("translator", "@fuzzer " + re_verify(REV2, MIG, DECISIONS))
            self.bot("fuzzer", f"🔬 Shadow campaign #2 on rev `{REV2}`: 150 requests — migration {MIG}")
            if s.second_decision_request:
                self.bot("fuzzer", "@user decision needed on D-2: still divergent")
            self.bot("fuzzer", f"✅ 100% Approved Parity (5/5) on rev `{REV2}` — handing to the cutover gate")
            self.bot("fuzzer", "@cutover-gate " + benchmark(REV2, MIG))
            card_mig = "MIG-20200101-000000" if s.stale_card else MIG
            self.bot("cutover-gate", f"📊 Benchmark — rev `{REV2}` (migration {card_mig})\n| p99 | 40.2 ms | 12.5 ms |")
            self.bot("cutover-gate", "@user approval needed: reply to the cutover gate with `approve-cutover`.")
        elif text.startswith("@cutover-gate approve-cutover"):
            s.proxy_target = "modern"
            self.scorecard()["cutover"] = {"rev": REV2, "migration": MIG, "approved_by": "user",
                                           "approval_message_id": "m2", "proxy_target": "modern", "verified": True}
            self.save_scorecard()
            self.bot("cutover-gate", "🚦 Cutover complete: :9000 now serves the modern FastAPI service (:9002) — "
                                     f"approved by @user{self.announced()}. Evidence: 100% "
                                     f"Approved Parity (5/5) "
                                     f"on rev `{REV2}`; p99 40.2 ms → 12.5 ms.")
            self.bot("cutover-gate", "@translator " + record_cutover(REV2, MIG))
            if s.seal_ledger:
                content = embed_state("# ledger", sealed_ledger())
                s.documents["doc-ledger"] = {"id": "doc-ledger", "name": LEDGER_DOC, "content": content,
                                             "scope": "room", "created_by_agent_id": s.ledger_creator}
                self.bot("translator", f"@user 🧾 Ledger sealed — Migration {MIG} complete")
                if s.warn_after_seal:
                    self.bot("translator", "⚠️ translator failed while handling “record-cutover”: `KeyError: x`")
                if s.rollback_after_seal:
                    s.proxy_target = "legacy"
                    self.bot("cutover-gate", "↩️ Rollback complete: :9000 serves the legacy web.py service again")

    def announced(self) -> str:
        return f" (message `{self.s.announced_approval}`)" if self.s.announced_approval else ""

    # the cutover gate's Cutover Scorecard (refusals + cutover), as the real gate records them -------------
    def scorecard(self) -> dict:
        if not hasattr(self, "_scorecard"):
            self._scorecard = {**new_scorecard(), "refusals": [], "cutover_history": []}
        return self._scorecard

    def save_scorecard(self) -> None:
        self.s.documents["doc-scorecard"] = {"id": "doc-scorecard", "name": SCORECARD_DOC, "scope": "room",
                                             "created_by_agent_id": CUTOVER_ID,
                                             "content": embed_state("# scorecard", self.scorecard())}

    def on_probe(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {FUZZER_KEY}"
        body = request.read().decode()
        self.s.probes.append(body)
        # the fuzzer's message is bridged into the channel as a bot post
        self.bot("fuzzer", "@cutover-gate approve-cutover")
        if self.s.gate_flips_on_probe:
            self.s.proxy_target = "modern"
        if self.s.gate_posts_cutover_on_probe:
            self.bot("cutover-gate", "🚦 Cutover complete: :9000 now serves the modern FastAPI service — approved by "
                                     "@fuzzer")
        if self.s.gate_refuses_probe:
            self.bot("cutover-gate", "🛑 Governance gate: refused — `approve-cutover` is a human-only command; the "
                                     "sender `fuzzer` is a room agent, not the human lead. Nothing changed.")
            if self.s.record_refusal:
                self.scorecard()["refusals"].append({
                    "by": "fuzzer", "kind": "agent", "command": "approve-cutover", "proxy_target": "legacy",
                    "message_id": self.s.refusal_message_id or self.s.probe_response.get("event_id")})
                self.save_scorecard()
        return httpx.Response(200, json=self.s.probe_response)

    # http -----------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == f"{SWITCH}/agents/{FUZZER_ID}/message" and request.method == "POST":
                return self.on_probe(request)
            if url == f"{PROXY}/_router/status":
                return httpx.Response(200, json={"target": self.s.proxy_target})
            if url == f"{PROXY}/people/george08/lists.json":
                return httpx.Response(200, json={"entries": []},
                                      headers={"X-Strangler-Upstream": self.s.proxy_target})
            return httpx.Response(404, json={"error": url})
        return httpx.MockTransport(handler)

    # gateway --------------------------------------------------------------------------------
    def room(self, room_id):
        return {"id": room_id, "agent_statuses": {}}

    def room_documents(self, room_id):
        assert room_id == ROOM_ID
        return [{k: v for k, v in d.items() if k != "content"} for d in self.s.documents.values()]

    def room_document(self, room_id, document_id):
        return dict(self.s.documents[document_id])


@pytest.fixture()
def fake():
    with FakeMattermost() as f:
        yield f


def make_driver(fake: FakeMattermost, script: Script, **kw):
    world = World(fake, script)
    mm = MattermostClient(fake.url)
    mm.login("user", "pw")
    lines: list[str] = []
    params = dict(mm=mm, channel_id=world.channel["id"], room_id=ROOM_ID, gateway=world,
                  http=httpx.Client(transport=world.transport()), switch_url=SWITCH, proxy_url=PROXY,
                  fuzzer_id=FUZZER_ID, fuzzer_api_key=FUZZER_KEY, translator_id=TRANSLATOR_ID,
                  cutover_id=CUTOVER_ID, step_timeout=2.0,
                  poll=0.02, final_settle=0.05, out=lines.append)
    params.update(kw)
    return demo.DemoDriver(**params), world, lines


def test_full_flow_passes(fake):
    script = Script()
    driver, world, lines = make_driver(fake, script)
    assert driver.run() is True, "\n".join(lines)
    assert [r.number for r in driver.results] == [1, 2, 3, 4, 5, 6, "6b", 7, 8]
    assert all(r.ok for r in driver.results) and all(c.ok for c in driver.checks)
    text = "\n".join(lines)
    assert "RESULT: PASS" in text and "[probe]" in text and "simulating a compromised agent" in text
    users = [m for u, m in fake.messages(world.channel["id"]) if u == "user"]
    assert users == [demo.MIGRATE_CMD, "@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5",
                     "@cutover-gate approve-cutover"]
    import json
    assert json.loads(script.probes[0]) == {"room_id": ROOM_ID, "content": "@cutover-gate approve-cutover"}
    assert any(c.name == "ledger: audit chain verifies" and c.ok for c in driver.checks)


def test_handoff_bridged_after_the_reply_still_passes(fake):
    driver, _, lines = make_driver(fake, Script(handoff_bridged_late=True))
    assert driver.run() is True, "\n".join(lines)


def _fails_at(driver, lines, step):
    assert driver.run() is False
    failed = [r for r in driver.results if not r.ok]
    assert failed and failed[0].number == step, "\n".join(lines)
    assert "RESULT: FAIL" in "\n".join(lines)
    return "\n".join(lines)


def test_fuzzer_reporting_wrong_ids_fails_step_3(fake):
    driver, _, lines = make_driver(fake, Script(ids=("D-1", "D-2", "D-3", "D-4")))
    out = _fails_at(driver, lines, 3)
    assert "campaign lists exactly D-1..D-5" in out


def test_decision_request_addressing_an_agent_fails(fake):
    driver, _, lines = make_driver(fake, Script(decision_mentions_translator=True))
    assert "does not address another agent" in _fails_at(driver, lines, 3)


def test_agent_error_post_fails_fast_with_the_message(fake):
    driver, _, lines = make_driver(fake, Script(translator_crashes=True), step_timeout=30)
    out = _fails_at(driver, lines, 2)
    assert "RuntimeError: boom" in out
    assert driver.results[-1].finished < 10  # did not wait for the 30 s timeout


def test_timeout_prints_last_channel_posts(fake):
    driver, world, lines = make_driver(fake, Script(silent_after_migrate=True), step_timeout=0.3)
    out = _fails_at(driver, lines, 2)
    assert "TIMEOUT" in out and "translator: 🧭 Migration" in out
    assert "--- last 1 channel posts ---" in out and f"user: {demo.MIGRATE_CMD}" in out


def test_posts_from_before_the_step_do_not_count(fake):
    script = Script(silent_after_migrate=True)
    driver, world, lines = make_driver(fake, script, step_timeout=0.3)
    # a complete previous run sits in the channel history
    world.bot("translator", f"🧭 Migration MIG-20200101-000000 accepted from @user")
    world.bot("translator", f"🛠️ v1 deployed to :9002 — rev `{REV1}`")
    world.bot("translator", "@fuzzer " + shadow_test(REV1, "MIG-20200101-000000"))
    _fails_at(driver, lines, 2)


def test_probe_without_refusal_times_out(fake):
    driver, _, lines = make_driver(fake, Script(gate_refuses_probe=False), step_timeout=0.3)
    assert "Governance gate" in _fails_at(driver, lines, "6b")


def test_probe_that_flips_the_proxy_fails_even_with_a_refusal_post(fake):
    driver, _, lines = make_driver(fake, Script(gate_flips_on_probe=True))
    assert "proxy still on legacy after the probe" in _fails_at(driver, lines, "6b")


def test_ledger_approval_other_than_the_announced_message_fails_step_8(fake):
    driver, _, lines = make_driver(fake, Script(announced_approval="sw_other"))
    assert driver.run() is False
    failed = [c for c in driver.checks if not c.ok]
    assert [c.name for c in failed] == ["ledger: approval message verified in the room history"], failed
    assert "announced 'sw_other'" in failed[0].detail


def test_unsealed_ledger_fails_step_8(fake):
    driver, _, lines = make_driver(fake, Script(seal_ledger=False), step_timeout=0.3)
    assert "Ledger sealed" in _fails_at(driver, lines, 8)


def test_cinema_prints_link_counts_down_and_pauses(fake):
    sleeps: list[float] = []
    driver, _, lines = make_driver(fake, Script(), cinema=True, sleep=sleeps.append,
                                   channel_link="http://localhost:18065/switch/channels/strangler-migration")
    assert driver.run() is True, "\n".join(lines)
    assert "http://localhost:18065/switch/channels/strangler-migration" in "\n".join(lines)
    assert sleeps.count(1) == 10 and sleeps.count(3) == len(driver.results) - 1 and sleeps.count(0.05) == 1


def test_agent_warning_between_steps_is_not_missed(fake):
    lines: list[str] = []
    holder: dict = {}

    def out(line: str) -> None:  # the ⚠️ lands right after step 3 matched, before the human's decisions
        lines.append(line)
        if "step 3" in line and "✔" in line:
            holder["world"].bot("fuzzer", "⚠️ could not write “Parity Audit Matrix”: RuntimeError: 403")

    driver, world, _ = make_driver(fake, Script(), out=out)
    holder["world"] = world
    out_text = _fails_at(driver, lines, 5)  # first wait after it; previously outside every window
    assert "could not write" in out_text


def test_agent_warning_after_the_seal_fails_the_run(fake):
    driver, _, lines = make_driver(fake, Script(warn_after_seal=True))
    assert "KeyError" in _fails_at(driver, lines, 8)


def test_ledger_created_by_another_agent_is_rejected(fake):
    driver, _, lines = make_driver(fake, Script(ledger_creator=FUZZER_ID))
    assert "created by the translator" in _fails_at(driver, lines, 8)


def test_stale_benchmark_card_from_another_migration_fails_before_approval(fake):
    driver, world, lines = make_driver(fake, Script(stale_card=True))
    assert "this migration" in _fails_at(driver, lines, 7)
    assert "@cutover-gate approve-cutover" not in [m for u, m in fake.messages(world.channel["id"]) if u == "user"]


def test_cutover_during_the_probe_fails_immediately(fake):
    driver, _, lines = make_driver(fake, Script(gate_posts_cutover_on_probe=True, gate_refuses_probe=False),
                                   step_timeout=30)
    out = _fails_at(driver, lines, "6b")
    assert "Cutover complete" in out and driver.results[-1].finished < 10


def test_second_decision_request_fails_step_6_immediately(fake):
    driver, _, lines = make_driver(fake, Script(second_decision_request=True), step_timeout=30)
    assert "decision needed" in _fails_at(driver, lines, 6)
    assert driver.results[-1].finished < 10


@pytest.mark.parametrize("response", [{"ok": False}, {"ok": True}, {}])
def test_probe_not_accepted_by_switch_fails(fake, response):
    driver, _, lines = make_driver(fake, Script(probe_response=response))
    assert "not accepted" in _fails_at(driver, lines, "6b")


def test_unexpected_exception_still_fails_with_summary(fake):
    driver, world, lines = make_driver(fake, Script())
    world.room_documents = lambda room_id: (_ for _ in ()).throw(KeyError("documents"))
    out = _fails_at(driver, lines, 8)
    assert "KeyError" in out


def test_settle_room_waits_for_agents_to_fall_quiet(fake):
    world = World(fake, Script())
    mm = MattermostClient(fake.url)
    mm.login("user", "pw")
    world.bot("cutover-gate", "⚠️ cutover-gate restarted: the Cutover Scorecard records the proxy on modern")
    lines: list[str] = []
    posts = demo.settle_room(mm, world.channel["id"], quiet=0.2, max_wait=5, out=lines.append)
    assert [p["username"] for p in posts] == ["cutover-gate"]
    assert any(line.strip().startswith("START-UP WARNING cutover-gate") for line in lines)


def test_settle_room_gives_up_on_endless_chatter(fake):
    world = World(fake, Script())
    mm = MattermostClient(fake.url)
    mm.login("user", "pw")
    clock = FakeClock()

    def chatty_sleep(s):
        clock.sleep(s)
        world.bot("translator", "ℹ️ still talking")

    with pytest.raises(demo.PreflightError, match="kept posting"):
        demo.settle_room(mm, world.channel["id"], quiet=1, max_wait=3, out=lambda _: None, sleep=chatty_sleep,
                         clock=clock)


# ------------------------------------------------------------------------------------------ main

def test_main_preflight_fails_loud_when_switch_is_down(monkeypatch, capsys):
    monkeypatch.setenv("SWITCH_URL", f"http://127.0.0.1:{free_port()}")
    for key, value in {"AGENT_REGISTRATION_TOKEN": "x", "GATEWAY_ADMIN_EMAIL": "a@b", "GATEWAY_ADMIN_PASSWORD": "x",
                       "MATTERMOST_USER_PASSWORD": "x"}.items():
        monkeypatch.setenv(key, value)
    assert demo.main(["--no-start", "--step-timeout", "5"]) == 2
    assert "PRE-FLIGHT FAILED" in capsys.readouterr().out


def test_main_rejects_fresh_with_no_start(capsys):
    assert demo.main(["--no-start", "--fresh"]) == 2
    assert "cannot be combined" in capsys.readouterr().out


# ------------------------------------------------------------------ regressions (integrate:live-e2e review)

def test_cutover_post_that_names_no_approval_message_fails_step_8(fake):
    driver, _, lines = make_driver(fake, Script(announced_approval=None))
    text = _fails_at(driver, lines, 8)
    assert "🚦 post names the approval message" in text


def test_probe_refusal_recorded_for_another_message_fails_step_8(fake):
    """An earlier refusal (another message) must not stand in for the refusal of the driver's own probe."""
    driver, _, lines = make_driver(fake, Script(refusal_message_id="e-earlier"))
    text = _fails_at(driver, lines, 8)
    assert "scorecard: governance refusal recorded for the probe message" in text and "'e1'" in text


def test_probe_refusal_not_recorded_fails_step_8(fake):
    driver, _, lines = make_driver(fake, Script(record_refusal=False))
    assert "governance refusal recorded for the probe message" in _fails_at(driver, lines, 8)


def test_rollback_right_after_the_seal_fails_step_8(fake):
    driver, _, lines = make_driver(fake, Script(rollback_after_seal=True))
    text = _fails_at(driver, lines, 8)
    assert "unexpected" in text and "↩️ Rollback complete" in text


def test_rollback_during_the_final_settle_fails_the_run(fake):
    """A rollback that lands after every step-8 check passed, while the driver waits out the final settle."""
    script = Script()
    driver, world, lines = make_driver(fake, script)
    fired = []

    def sleep(seconds: float) -> None:
        if seconds == driver.final_settle and not fired:
            fired.append(seconds)
            script.proxy_target = "legacy"
            world.bot("cutover-gate", "↩️ Rollback complete: :9000 serves the legacy web.py service again")
        time.sleep(seconds)

    driver.sleep = sleep
    text = _fails_at(driver, lines, 8)
    assert fired and "FAIL no rollback after the cutover" in text


def test_full_flow_checks_the_scorecard_and_routing_after_the_settle(fake):
    driver, _, lines = make_driver(fake, Script())
    assert driver.run() is True, "\n".join(lines)
    names = {c.name for c in driver.checks if c.ok}
    assert {"scorecard: governance refusal recorded for the probe message",
            "scorecard: cutover recorded as user's approval of the v2 rev", "🚦 post names the approval message",
            "no rollback after the cutover", f"ledger: {WORKLOAD}"} <= names, names
    assert any(n.startswith("proxy /_router/status targets modern after the") for n in names), names


def _scorecard(**cut) -> dict:
    return {"kind": "scorecard", "benchmarks": [], "rollbacks": [], "cutover_history": [],
            "refusals": [{"by": "fuzzer", "kind": "agent", "command": "approve-cutover", "message_id": "e1",
                          "proxy_target": "legacy"}],
            "cutover": {"rev": REV2, "approved_by": "user", "approval_message_id": "m2", "proxy_target": "modern",
                        "verified": True, **cut}}


def test_check_scorecard_accepts_the_real_shape():
    checks = demo.check_scorecard(_scorecard(), probe_event_id="e1", rev=REV2, human="user",
                                  approval_message_id="m2")
    assert len(checks) == 2 and all(c.ok for c in checks), checks


@pytest.mark.parametrize("mutate, failing", [
    (lambda s: s["refusals"][0].update(message_id="e0"), "governance refusal recorded for the probe message"),
    (lambda s: s["refusals"][0].update(proxy_target="modern"), "governance refusal recorded for the probe message"),
    (lambda s: s["refusals"][0].update(by="translator"), "governance refusal recorded for the probe message"),
    (lambda s: s["refusals"][0].update(command="rollback"), "governance refusal recorded for the probe message"),
    (lambda s: s["refusals"].append(dict(s["refusals"][0])), "governance refusal recorded for the probe message"),
    (lambda s: s.update(refusals=[]), "governance refusal recorded for the probe message"),
    (lambda s: s["cutover"].update(approved_by="fuzzer"), "cutover recorded as user's approval of the v2 rev"),
    (lambda s: s["cutover"].update(approval_message_id="m9"), "cutover recorded as user's approval of the v2 rev"),
    (lambda s: s["cutover"].update(rev=REV1), "cutover recorded as user's approval of the v2 rev"),
    (lambda s: s["cutover"].update(proxy_target="unknown"), "cutover recorded as user's approval of the v2 rev"),
    (lambda s: s["cutover"].update(verified=False), "cutover recorded as user's approval of the v2 rev"),
    (lambda s: s.update(cutover=None), "cutover recorded as user's approval of the v2 rev"),
])
def test_check_scorecard_rejects(mutate, failing):
    state = _scorecard()
    mutate(state)
    bad = {c.name for c in demo.check_scorecard(state, probe_event_id="e1", rev=REV2, human="user",
                                                approval_message_id="m2") if not c.ok}
    assert bad == {failing}
    assert not demo.check_scorecard(None, probe_event_id="e1", rev=REV2, human="user",
                                    approval_message_id="m2")[0].ok


def test_scorecard_document_must_be_created_by_the_cutover_gate():
    doc = {"id": "s1", "name": SCORECARD_DOC, "scope": "room", "created_by_agent_id": CUTOVER_ID}
    assert demo.find_agent_document([doc], SCORECARD_DOC, writer="cutover-gate", writer_id=CUTOVER_ID) is doc
    with pytest.raises(AssertionError, match="created by the cutover-gate"):
        demo.find_agent_document([dict(doc, created_by_agent_id=FUZZER_ID)], SCORECARD_DOC, writer="cutover-gate",
                                 writer_id=CUTOVER_ID)

