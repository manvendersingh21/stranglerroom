"""Tests for @translator (agents/translator_agent.py, docs/AGENTS_SPEC.md §2).

The room is a :class:`tests.fakes.FakeRoom`; the modern target is a REAL subprocess
(``targets.modern_fastapi.server`` with the uvicorn reloader ON) serving a generated module from a tmp
``MODERN_LISTS_PATH`` on a free port — the repo's ``targets/modern_fastapi/app/lists.py`` is never touched.
The fuzzer's matrix and the cutover gate's scorecard are synthetic documents created through
``FakeSwitchAgent('fuzzer')`` / ``FakeSwitchAgent('cutover-gate')`` (so the creator-only rule holds).
"""
from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

import httpx
import pytest

from agents import ledger as L
from agents.codegen import QUIRK_TAGS, module_rev, render_lists_module
from agents.commands import MARK, mentions, parse_fuzzer, record_cutover
from agents.divergence import tag_title
from agents.legacy_introspect import introspect_file, introspect_source
from agents.provision import RoomInfo
from agents.switch_client import Settings, SwitchError
from agents.translator_agent import DEFAULT_MODERN_LISTS, TranslatorAgent, defuse, match_route
from tests.fake_llm import FAKE_HEADER
from tests.fakes import FakeRoom
from tests.procs import free_port, service

ROOT = Path(__file__).resolve().parents[1]
FAKE_LLM = ROOT / "tests" / "fake_llm.py"
LEGACY = ROOT / "targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py"
MIGRATE = "@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI"
DECIDE = "@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5"
DEMO_POLICY = {"json_suffix": "preserve", "loose_query": "preserve", "missing_resource_200": "deprecate",
               "non_positive_quantity": "preserve", "empty_body_500": "deprecate"}


# ------------------------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def modern(tmp_path_factory):
    lists = tmp_path_factory.mktemp("modern") / "lists.py"
    port = free_port()
    with service("targets.modern_fastapi.server", port, env={"MODERN_LISTS_PATH": str(lists)}) as url:
        yield url, lists


def make_agent(room: FakeRoom, lists: Path, url: str = "http://127.0.0.1:1", **kw) -> TranslatorAgent:
    """A translator wired to ``room``; every path it writes is under the test's tmp dir (never the repo's)."""
    assert lists.resolve() != DEFAULT_MODERN_LISTS.resolve()
    kw.setdefault("pending_refusals_path", lists.parent / f"pending-{id(room)}.json")
    settings = Settings(*(["unused"] * 8))
    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id=None, channel_url=None,
                    agents=dict(room.agent_ids), human=room.human)
    kw.setdefault("reload_timeout", 30)
    kw.setdefault("approval_lookup_delay", 0.01)
    return TranslatorAgent(settings=settings, room=info, switch=room.agent("translator"),
                           legacy_module_path=LEGACY, modern_lists_path=lists, modern_url=url, **kw)


def pump(room: FakeRoom, agent: TranslatorAgent) -> list[str]:
    before = len(room.messages)
    room.pump({"translator": agent})
    return [m.body for m in room.messages[before:] if m.sender == "translator" and not m.body.startswith("📄")]


def ledger_state(room: FakeRoom) -> dict:
    return L.extract_state(room.doc_by_name(L.LEDGER_DOC)["content"])


def meta(url: str) -> dict:
    return httpx.get(f"{url}/_meta", timeout=5).json()


def assert_mention_hygiene(room: FakeRoom) -> None:
    """The translator only ever addresses the human, or an agent at the very start of a handoff."""
    for m in room.messages:
        if m.sender != "translator":
            continue
        for name in mentions(m.body):
            if name == room.human:
                continue
            assert m.body.startswith(f"@{name} "), f"accidental @{name} in: {m.body[:200]}"
            assert name in ("fuzzer",), m.body[:120]


def cluster(cid: str, tag: str, status: str = "open", decision: str | None = None) -> dict:
    return {"id": cid, "tag": tag, "title": tag_title(tag), "count": 3, "legacy_status": 200, "modern_status": 404,
            "sample_request": "GET /people/george08/lists.json", "sample_diff": ["status: 200 != 404"],
            "decision": decision, "status": status}


def publish_matrix(room: FakeRoom, *, mig: str, rev: str, approved: bool = False,
                   clusters: list[dict] | None = None) -> dict:
    """Create/update the fuzzer's Parity Audit Matrix as the fuzzer agent would."""
    fuzzer = room.agent("fuzzer")
    doc = room.doc_by_name(L.MATRIX_DOC)
    state = L.extract_state(doc["content"]) if doc else {**L.new_matrix(), "migration": mig}
    if clusters is None:
        clusters = [cluster(f"D-{i}", tag, "resolved" if approved else "open") for i, tag in enumerate(QUIRK_TAGS, 1)]
    n = len(clusters)
    report = {"total_requests": 150, "identical": 130, "divergent": 0 if approved else 20, "clusters": clusters,
              "resolved": n if approved else 0, "unresolved": 0 if approved else n, "approved_parity": approved,
              "parity_label": f"100% Approved Parity ({n}/{n})" if approved else f"Parity 0% (0/{n} resolved)",
              "duration_s": 1.5, "legacy_url": "", "modern_url": ""}
    L.add_run(state, rev=rev, trigger={"by": "translator", "kind": "agent", "command": "shadow-test",
                                       "migration": mig, "message_id": "sw_trigger"}, decisions={}, report=report)
    desc, instr = L.DOC_META[L.MATRIX_DOC]
    fuzzer.upsert_document(name=L.MATRIX_DOC, description=desc, instructions=instr, content=L.render_matrix(state))
    return state


def publish_scorecard(room: FakeRoom, *, rev: str, mig: str, approved_by: str = "user", target: str | None = "modern",
                      approval_message_id: str = "sw_approve1", cut_mig: str | None = None) -> dict:
    gate = room.agent("cutover-gate")
    side = {"p50_ms": 4.0, "p95_ms": 9.0, "p99_ms": 14.0, "max_ms": 20.0, "throughput_rps": 900.0, "errors": 0}
    state = L.new_scorecard()
    state["benchmarks"].append({
        "rev": rev, "at": L.now_iso(), "total": 500, "concurrency": 50, "paths": ["/people/george08/lists"],
        "legacy": {**side, "p99_ms": 42.5}, "modern": {**side, "p99_ms": 12.25},
        "compare": {"p50_delta_pct": -10.0, "p95_delta_pct": -20.0, "p99_delta_pct": -71.2,
                    "throughput_delta_pct": 30.0, "modern_faster_p99": True},
        "verdict": "modern is faster"})
    state["cutover"] = None if target is None else {
                        "approved_by": approved_by, "approved_at": L.now_iso(),
                        "approval_message_id": approval_message_id, "migration": cut_mig or mig,
                        "proxy_target": target, "upstream_url": "http://127.0.0.1:9002", "flipped_at": L.now_iso(),
                        "verified": True, "verification": "X-Strangler-Upstream: modern", "rev": rev}
    desc, instr = L.DOC_META[L.SCORECARD_DOC]
    gate.upsert_document(name=L.SCORECARD_DOC, description=desc, instructions=instr, content=L.render_scorecard(state))
    return state


def migrated(room: FakeRoom, modern) -> tuple[TranslatorAgent, str, str]:
    url, lists = modern
    agent = make_agent(room, lists, url)
    room.human_says(MIGRATE)
    pump(room, agent)
    st = ledger_state(room)
    return agent, st["migration"]["id"], st["builds"][-1]["rev"]


# ------------------------------------------------------------------------------------ pure helpers

def test_match_route_templates_and_concrete_paths():
    routes = introspect_file(LEGACY)
    assert [r.template for r in match_route("/people/{user}/lists", routes)] == ["/people/{user}/lists"]
    assert match_route("/people/george08/lists.json", routes)
    assert len(match_route("/people/george08/lists/OL1L/seeds", routes)) == 2  # GET + POST
    assert match_route("/people/george08", routes) == []
    assert match_route("/people/{user}/lists/extra", routes) == []


def test_defuse_breaks_every_mention():
    assert mentions(defuse("ping @fuzzer and @cutover-gate")) == []


def test_introspect_regressions():
    """Plain-name bases do not crash the extractor; non-capturing groups are not path params."""
    routes = introspect_source(
        "class A(page):\n    path = r'/a/(?:x|y)/([^/]+)'\n    def GET(self, k):\n        pass\n"
        "class B(Base):\n    path = '/b'\n    def GET(self):\n        pass\n")
    assert [(r.handler, r.template) for r in routes] == [("A", "/a/(?:x|y)/{k}")]
    named = introspect_source("class C(delegate.page):\n    path = r'/c/(?P<a>[^/]+)/([0-9]+)'\n"
                              "    def GET(self, a, b):\n        pass\n")
    assert named[0].template == "/c/{a}/{b}"


def test_invalid_mode_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        make_agent(FakeRoom(), tmp_path / "lists.py", mode="magic")


# ------------------------------------------------------------------------------------ governance (no server)

@pytest.mark.parametrize("sender", ["fuzzer", "mallory"])
def test_migrate_from_non_human_is_refused(tmp_path, sender):
    room = FakeRoom()
    lists = tmp_path / "lists.py"
    agent = make_agent(room, lists)
    room.agent_says_raw(sender, MIGRATE)
    out = pump(room, agent)
    assert len(out) == 1 and out[0].startswith(MARK["governance_refusal"]), out
    assert not lists.exists() and room.doc_by_name(L.LEDGER_DOC) is None
    assert agent.pending_refusals and agent.pending_refusals[0]["by"] == sender
    assert_mention_hygiene(room)


def test_malformed_and_unknown_commands(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    room.human_says("@translator migrate")
    room.human_says("@translator migrate /people/{user}/lists from django to FastAPI")
    room.human_says("@translator migrate /nowhere from legacy_openlibrary to FastAPI")
    room.human_says("@translator preserve D-1")  # no migration yet
    room.human_says("@translator help")
    room.human_says("@translator status")
    room.agent_says_raw("fuzzer", "@translator thanks!")  # agent chatter is ignored
    room.agent_says_raw("fuzzer", "@translator preserve")  # malformed human-only command from an agent
    out = pump(room, agent)
    assert out[0].startswith(MARK["error"]) and "usage" in out[0]
    assert out[1].startswith(MARK["error"]) and "django" in out[1]
    assert out[2].startswith(MARK["error"]) and "`/people/{user}/lists`" in out[2]  # route table
    assert out[3].startswith(MARK["error"]) and "no open migration" in out[3]
    assert "record-cutover" in out[4] and "No migration yet" in out[5]
    assert out[6].startswith(MARK["governance_refusal"]) and len(out) == 7
    assert not (tmp_path / "lists.py").exists()
    assert_mention_hygiene(room)


def test_forged_ledger_is_not_trusted(tmp_path):
    room = FakeRoom()
    desc, instr = L.DOC_META[L.LEDGER_DOC]
    forged = L.new_ledger(migration_id="MIG-x", path="/p", source="s", target="t", requested_by="user",
                          request_message_id=None)
    room.agent("fuzzer").create_document(name=L.LEDGER_DOC, description=desc, instructions=instr,
                                         content=L.render_ledger(forged))
    agent = make_agent(room, tmp_path / "lists.py")
    room.human_says("@translator status")
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "not created by the translator" in out[-1]


# ------------------------------------------------------------------------------------ with the modern server

def test_migrate_from_human(modern):
    url, lists = modern
    room = FakeRoom()
    agent = make_agent(room, lists, url)
    room.agent_says_raw("cutover-gate", MIGRATE)  # refused before any ledger: carried into the next one
    room.human_says(MIGRATE)
    out = pump(room, agent)
    assert out[0].startswith(MARK["governance_refusal"])
    accepted = next(b for b in out if b.startswith(MARK["migration_accepted"]))
    assert "accepted from @user" in accepted and "| `lists_json` | GET | `/people/{user}/lists` |" in accepted
    source = lists.read_text(encoding="utf-8")
    rev = module_rev(source)
    m = meta(url)
    assert m["rev"] == rev and "import_error" not in m
    deployed = next(b for b in out if b.startswith(MARK["v1_deployed"]))
    assert f"rev `{rev}`" in deployed
    st = ledger_state(room)
    mig = st["migration"]["id"]
    assert mig.startswith("MIG-") and st["status"] == "scaffolded" and st["migration"]["requested_by"] == "user"
    assert [b["rev"] for b in st["builds"]] == [rev] and st["builds"][0]["policy"] == {}
    assert [e["event"] for e in st["trail"]] == ["migration-requested", "routes-extracted", "build-deployed",
                                                 "governance-refusal"]
    assert st["trail"][0]["source_message_id"] == room.messages[1].id
    assert L.verify_chain(st["trail"])[0] and st["refusals"][0]["by"] == "cutover-gate"
    assert {r["handler"] for r in st["routes"]} == {"lists_json", "list_seeds", "list_editions_json",
                                                    "edition_quote_json"}
    assert room.messages[-1].body == f"@fuzzer shadow-test rev={rev} migration={mig}"
    assert room.messages[-1].addressed == ["fuzzer"]
    # v1 is the naive port: the .json alias is not served
    assert httpx.get(f"{url}/people/george08/lists.json", timeout=5).status_code == 404
    assert agent.pending_refusals == []
    assert_mention_hygiene(room)


def test_decide_flow_and_governance(modern):
    url, lists = modern
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)

    room.human_says("@translator preserve D-9 and deprecate D-3")  # unknown id
    room.agent_says_raw("cutover-gate", DECIDE)                     # agent-sent decisions
    out = pump(room, agent)
    assert out[0].startswith(MARK["error"]) and "D-9" in out[0] and "D-1, D-2, D-3, D-4, D-5" in out[0]
    assert out[1].startswith(MARK["governance_refusal"])
    st = ledger_state(room)
    assert st["decisions"] == {} and len(st["builds"]) == 1 and meta(url)["rev"] == v1
    assert st["refusals"][-1]["by"] == "cutover-gate" and st["trail"][-1]["event"] == "governance-refusal"

    msg = room.human_says(DECIDE)
    out = pump(room, agent)
    applied = next(b for b in out if b.startswith(MARK["decisions_applied"]))
    st = ledger_state(room)
    v2 = st["builds"][-1]["rev"]
    assert v2 != v1 and v2 == module_rev(lists.read_text(encoding="utf-8")) == meta(url)["rev"]
    assert f"rev `{v2}`" in applied and "from @user" in applied and "v2 deployed" in applied
    assert st["builds"][-1]["policy"] == DEMO_POLICY and st["status"] == "patching"
    assert {cid: d["decision"] for cid, d in st["decisions"].items()} == {
        "D-1": "preserve", "D-2": "preserve", "D-3": "deprecate", "D-4": "preserve", "D-5": "deprecate"}
    assert all(d["by"] == "user" and d["message_id"] == msg.id for d in st["decisions"].values())
    assert [d["id"] for d in st["divergences"]] == ["D-1", "D-2", "D-3", "D-4", "D-5"]
    decided = [e for e in st["trail"] if e["event"] == "decision-recorded"]
    assert len(decided) == 5 and all(e["actor"] == "user" and e["actor_kind"] == "human" and
                                     e["evidence"]["matrix_digest"] for e in decided)
    assert L.verify_chain(st["trail"])[0]
    handoff = room.messages[-1]
    assert handoff.addressed == ["fuzzer"]
    cmd = parse_fuzzer(handoff.body)
    assert cmd.verb == "re-verify" and cmd.args["rev"] == v2 and cmd.args["migration"] == mig
    assert cmd.args["decisions"] == {cid: d["decision"] for cid, d in st["decisions"].items()}
    # the preserved quirks are live on the modern target
    assert httpx.get(f"{url}/people/george08/lists.json", timeout=5).status_code == 200
    assert httpx.get(f"{url}/books/OL1M/quote", params={"quantity": "-2"}, timeout=5).status_code == 200
    assert_mention_hygiene(room)

    # status never addresses the agents named in the ledger banners
    room.human_says("@translator status")
    out = pump(room, agent)
    assert mig in out[-1] and v2 in out[-1] and "@fuzzer" not in out[-1]
    assert_mention_hygiene(room)


def test_unclassified_cannot_be_preserved(modern):
    url, _ = modern
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, clusters=[cluster("D-1", "json_suffix"),
                                                    cluster("D-6", "unclassified:GET:/x:200:500")])
    room.human_says("@translator preserve D-1 D-6")
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "D-6" in out[-1] and "unclassified" in out[-1]
    assert len(ledger_state(room)["builds"]) == 1 and meta(url)["rev"] == v1


def test_record_cutover_seals_the_ledger(modern):
    url, lists = modern
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)
    room.human_says(DECIDE)
    pump(room, agent)
    v2 = ledger_state(room)["builds"][-1]["rev"]
    body = "@translator " + record_cutover(v2, mig)
    gate = room.agent("cutover-gate")

    def gate_records() -> list[str]:
        gate.send_targeted(record_cutover(v2, mig), ["translator"])
        return pump(room, agent)

    # preconditions not met yet: no approved matrix on v2, no scorecard
    out = gate_records()
    assert out[-1].startswith(MARK["error"]) and "refusing to seal" in out[-1]

    publish_matrix(room, mig=mig, rev=v2, approved=True)
    publish_scorecard(room, rev=v2, mig=mig, approved_by="fuzzer")  # an agent cannot be the approver
    out = gate_records()
    assert out[-1].startswith(MARK["error"]) and "not the human lead" in out[-1]
    publish_scorecard(room, rev=v2, mig=mig, cut_mig="MIG-other")  # cutover of another migration
    out = gate_records()
    assert out[-1].startswith(MARK["error"]) and "MIG-other" in out[-1]
    forged = room.agent_says_raw("fuzzer", "lgtm")  # an approval id that is not the human's approve-cutover
    publish_scorecard(room, rev=v2, mig=mig, approval_message_id=forged.id)
    out = gate_records()
    assert out[-1].startswith(MARK["error"]) and "is not an `approve-cutover`" in out[-1]
    assert ledger_state(room)["sealed"] is None

    approval = room.human_says("@cutover-gate approve-cutover")
    publish_scorecard(room, rev=v2, mig=mig, approval_message_id=approval.id)
    # while :9000 serves modern, neither new decisions nor a new migration may touch the live module
    before = lists.read_bytes()
    room.human_says("@translator deprecate D-1")
    room.human_says(MIGRATE)
    out = pump(room, agent)
    assert all(b.startswith(MARK["error"]) and "rollback" in b for b in out[-2:]), out[-2:]
    assert lists.read_bytes() == before and len(ledger_state(room)["builds"]) == 2

    room.human_says(body)                   # human-sent record-cutover is refused
    room.agent_says_raw("fuzzer", body)     # so is any other agent
    out = pump(room, agent)
    assert all(b.startswith(MARK["governance_refusal"]) for b in out[-2:])
    st = ledger_state(room)
    assert st["sealed"] is None and [r["by"] for r in st["refusals"][-2:]] == ["user", "fuzzer"]

    gate_records()
    sealed = room.messages[-1]
    assert sealed.body.startswith(f"@user {MARK['ledger_sealed']}") and sealed.addressed == ["user"]
    assert mig in sealed.body and "42.5 ms" in sealed.body and "12.2" in sealed.body
    st = ledger_state(room)
    ok, _ = L.verify_chain(st["trail"])
    assert ok and st["status"] == "sealed"
    assert [e["event"] for e in st["trail"][-5:]] == ["parity-verified", "benchmark-recorded", "cutover-approved",
                                                      "proxy-flipped", "ledger-sealed"]
    assert st["trail"][-3]["actor"] == "user" and st["trail"][-3]["source_message_id"] == approval.id
    assert st["cutover"]["approval_message_verified"] is True
    assert st["sealed"]["head_hash"] == st["trail"][-1]["hash"] and st["sealed"]["entries"] == len(st["trail"])
    assert st["parity"]["rev"] == v2 and st["parity"]["evidence"] == L.digest(L.extract_state(
        room.doc_by_name(L.MATRIX_DOC)["content"]))
    assert st["benchmark"]["modern"]["p99_ms"] == 12.25 and st["cutover"]["approved_by"] == "user"

    # idempotent: a second record-cutover changes nothing
    content = room.doc_by_name(L.LEDGER_DOC)["content"]
    out = gate_records()
    assert "already sealed" in out[-1] and room.doc_by_name(L.LEDGER_DOC)["content"] == content

    # a refusal after sealing is recorded without reopening the sealed trail
    room.agent_says_raw("fuzzer", DECIDE)
    pump(room, agent)
    after = ledger_state(room)
    assert after["trail"] == st["trail"] and after["refusals"][-1]["by"] == "fuzzer"

    # restart: a fresh agent rehydrates the sealed ledger from the room document
    fresh = make_agent(room, lists, url)
    fresh.on_connected()
    assert fresh.ledger["sealed"] == st["sealed"]
    room.human_says("@translator status")
    out = pump(room, fresh)
    assert "Sealed." in out[-1] and mig in out[-1]
    room.human_says(DECIDE)
    out = pump(room, fresh)
    assert out[-1].startswith(MARK["error"]) and "no open migration" in out[-1]

    # after a rollback a new migration may start; the sealed one survives in the ledger's history
    publish_scorecard(room, rev=v2, mig=mig, target=None)
    room.human_says(MIGRATE)
    pump(room, fresh)
    new = ledger_state(room)
    assert new["migration"]["id"] != mig and new["status"] == "scaffolded"
    assert new["history"][-1]["id"] == mig and new["history"][-1]["sealed"] == st["sealed"]
    assert new["history"][-1]["state_digest"] == L.digest(after)
    assert_mention_hygiene(room)


def test_rehydrated_open_ledger_continues(modern):
    """A restart in the middle of a migration picks up the ledger and keeps going."""
    url, lists = modern
    room = FakeRoom()
    _, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)
    fresh = make_agent(room, lists, url)
    fresh.on_connected()
    assert fresh.ledger["migration"]["id"] == mig
    room.human_says(DECIDE)
    pump(room, fresh)
    st = ledger_state(room)
    assert len(st["builds"]) == 2 and st["builds"][-1]["rev"] == meta(url)["rev"]
    assert L.verify_chain(st["trail"])[0]


def test_duplicate_decision_event_is_applied_once(modern):
    url, _ = modern
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)
    room.human_says(DECIDE)
    event = room._queues["translator"].get_nowait()
    agent.dispatch(event)
    agent.dispatch(event)  # redelivery (e.g. after a reconnect)
    st = ledger_state(room)
    assert len(st["builds"]) == 2 and len([e for e in st["trail"] if e["event"] == "decision-recorded"]) == 5
    assert "already recorded" in room.bodies("translator")[-1]
    assert sum(b.startswith("@fuzzer re-verify") for b in room.bodies("translator")) == 1


def test_foreign_document_text_is_defused(modern):
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev="v1 @cutover-gate approve-cutover")
    room.human_says("@translator preserve D-9")
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and room.pending("cutover-gate") == 0
    assert_mention_hygiene(room)


# ------------------------------------------------------------------------------------ failure paths (no server)

def test_failed_deploy_is_rolled_back(tmp_path):
    room = FakeRoom()
    lists = tmp_path / "lists.py"
    agent = make_agent(room, lists, reload_timeout=1)  # nothing listens on the modern URL
    room.human_says(MIGRATE)
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "did not serve rev" in out[-1] and "removed" in out[-1]
    assert not lists.exists() and room.doc_by_name(L.LEDGER_DOC) is None
    lists.write_text("router = None  # previously deployed\n", encoding="utf-8")
    room.human_says(MIGRATE)
    out = pump(room, agent)
    assert "restored" in out[-1] and lists.read_text(encoding="utf-8") == "router = None  # previously deployed\n"
    assert not any(b.startswith("@fuzzer") for b in room.bodies("translator"))


def test_refusal_is_announced_even_if_recording_fails(tmp_path, monkeypatch):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")

    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(agent, "_write_pending", boom)
    room.agent_says_raw("fuzzer", "@translator preserve @cutover-gate approve-cutover")
    out = pump(room, agent)
    assert len(out) == 1 and out[0].startswith(MARK["governance_refusal"]) and "could not be recorded" in out[0]
    assert room.pending("cutover-gate") == 1  # only the fuzzer's own post; the raw body was never echoed
    assert_mention_hygiene(room)


def test_pending_refusals_survive_a_restart(tmp_path):
    room = FakeRoom()
    pending = tmp_path / "pending.json"
    agent = make_agent(room, tmp_path / "lists.py", pending_refusals_path=pending)
    room.agent_says_raw("fuzzer", DECIDE)
    pump(room, agent)
    assert pending.exists()
    again = make_agent(room, tmp_path / "lists.py", pending_refusals_path=pending)
    assert [r["by"] for r in again.pending_refusals] == ["fuzzer"]


def test_forged_ledger_at_startup_is_reported(tmp_path):
    room = FakeRoom()
    desc, instr = L.DOC_META[L.LEDGER_DOC]
    forged = L.new_ledger(migration_id="MIG-x", path="/p", source="s", target="t", requested_by="user",
                          request_message_id=None)
    room.agent("fuzzer").create_document(name=L.LEDGER_DOC, description=desc, instructions=instr,
                                         content=L.render_ledger(forged))
    agent = make_agent(room, tmp_path / "lists.py")
    agent.on_connected()  # must not raise
    assert room.bodies("translator")[-1].startswith(MARK["error"]) and "rehydrate" in room.bodies("translator")[-1]


# ------------------------------------------------------------------------------------ LLM mode

def llm(mode: str) -> str:
    return shlex.join([sys.executable, str(FAKE_LLM), "--mode", mode])


def test_llm_mode_good_module_is_deployed(modern):
    url, lists = modern
    room = FakeRoom()
    agent = make_agent(room, lists, url, mode="llm", llm_cmd=llm("good"))
    room.human_says(MIGRATE)
    out = pump(room, agent)
    assert not any("LLM synthesis rejected" in b for b in out)
    source = lists.read_text(encoding="utf-8")
    assert source == render_lists_module({}, header_comment=FAKE_HEADER)
    st = ledger_state(room)
    assert st["builds"][-1]["mode"] == "llm" and st["builds"][-1]["rev"] == module_rev(source) == meta(url)["rev"]
    assert room.messages[-1].body.startswith("@fuzzer shadow-test rev=")


@pytest.mark.parametrize("mode, reason", [("bad_syntax", "syntax error"), ("runtime_error", "failed to import")])
def test_llm_mode_bad_module_falls_back_loudly(modern, mode, reason):
    url, lists = modern
    room = FakeRoom()
    agent = make_agent(room, lists, url, mode="llm", llm_cmd=llm(mode))
    room.human_says(MIGRATE)
    out = pump(room, agent)
    rejected = [b for b in out if b.startswith(f"{MARK['error']} LLM synthesis rejected")]
    assert len(rejected) == 1 and reason in rejected[0] and rejected[0].endswith("— used deterministic codegen")
    source = lists.read_text(encoding="utf-8")
    assert FAKE_HEADER not in source
    m = meta(url)
    assert m["rev"] == module_rev(source) and "import_error" not in m
    st = ledger_state(room)
    assert st["builds"][-1]["mode"] == "deterministic" and "LLM synthesis rejected" in st["builds"][-1]["note"]
    assert any(b.startswith(MARK["v1_deployed"]) for b in out)
    assert_mention_hygiene(room)


# ------------------------------------------------------------------ regressions (integrate:loop protocol review)

def test_seal_requires_an_actual_approve_cutover_command(modern):
    """A human message that merely CONTAINS 'approve-cutover' (verb not first) is not an approval: the cutover
    gate would not have acted on it, so the translator must not seal on it either."""
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, approved=True)
    not_an_approval = room.human_says("@cutover-gate status — do NOT approve-cutover yet")
    publish_scorecard(room, rev=v1, mig=mig, approval_message_id=not_an_approval.id)
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "is not an `approve-cutover`" in out[-1], out[-1]
    assert ledger_state(room)["sealed"] is None

    approval = room.human_says("@cutover-gate please approve-cutover")
    publish_scorecard(room, rev=v1, mig=mig, approval_message_id=approval.id)
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(f"@user {MARK['ledger_sealed']}"), out[-1]
    assert ledger_state(room)["cutover"]["approval_message_verified"] is True


def test_unknown_routing_after_a_failed_cutover_blocks_new_builds(modern):
    """The gate records ``proxy_target: unknown`` when a failed cutover could not be reverted; the module may be
    live behind :9000, so neither decisions nor a new migration may rewrite it."""
    url, lists = modern
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)
    publish_scorecard(room, rev=v1, mig=mig, target="unknown")
    before = lists.read_bytes()
    room.human_says(DECIDE)
    room.human_says(MIGRATE)
    out = pump(room, agent)
    assert len(out) == 2 and all(b.startswith(MARK["error"]) and "may be serving the modern service" in b
                                 and "rollback" in b for b in out), out
    assert lists.read_bytes() == before and len(ledger_state(room)["builds"]) == 1
    assert_mention_hygiene(room)


def test_reposted_identical_decisions_do_not_rebuild(modern):
    """The same decision line posted again as a NEW message (client retry / double post) is a no-op: another build
    would invalidate the re-verification and benchmark of the build that already carries those decisions."""
    url, _ = modern
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)
    room.human_says(DECIDE)
    pump(room, agent)
    st = ledger_state(room)
    v2 = st["builds"][-1]["rev"]
    room.human_says(DECIDE)
    room.human_says("@translator preserve D-2")  # a subset of what is applied: also nothing new
    out = pump(room, agent)
    assert len(out) == 2 and all(b.startswith("ℹ️") and "already applied" in b and f"rev `{v2}`" in b for b in out)
    assert ledger_state(room) == st and meta(url)["rev"] == v2
    assert room.pending("fuzzer") == 2  # the v1 shadow-test + the single re-verify handoff
    room.human_says("@translator deprecate D-2")  # a real change still rebuilds
    pump(room, agent)
    assert len(ledger_state(room)["builds"]) == 3
    assert_mention_hygiene(room)


# ------------------------------------------------------------------ regressions (integrate:live-e2e review)

def _ready_to_seal(room: FakeRoom, modern) -> tuple[TranslatorAgent, str, str]:
    """A migration whose v1 is parity-approved, benchmarked and cut over on the human's real approval message."""
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, approved=True)
    approval = room.human_says("@cutover-gate approve-cutover")
    publish_scorecard(room, rev=v1, mig=mig, approval_message_id=approval.id)
    return agent, mig, v1


def test_seal_retries_a_transient_history_read_failure(modern):
    """The seal is final: one failed read_context must not seal the ledger as `approval_message_verified: null`."""
    room = FakeRoom()
    agent, mig, v1 = _ready_to_seal(room, modern)
    real, calls = agent.switch.read_context, []

    def flaky(room_id, limit=50):
        calls.append(limit)
        if len(calls) == 1:
            raise SwitchError("POST", "read_context", 503, "upstream restarting")
        return real(room_id, limit=limit)

    agent.switch.read_context = flaky
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(f"@user {MARK['ledger_sealed']}"), out[-1]
    assert len(calls) == 2 and ledger_state(room)["cutover"]["approval_message_verified"] is True


def test_seal_waits_for_the_approval_to_appear_in_the_history(modern):
    room = FakeRoom()
    agent, mig, v1 = _ready_to_seal(room, modern)
    real, calls = agent.switch.read_context, []

    def lagging(room_id, limit=50):
        calls.append(limit)
        ctx = real(room_id, limit=limit)
        return {**ctx, "threads": []} if len(calls) == 1 else ctx  # the first read does not show it yet

    agent.switch.read_context = lagging
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    pump(room, agent)
    assert len(calls) == 2 and ledger_state(room)["cutover"]["approval_message_verified"] is True


def test_seal_is_refused_while_the_history_is_unreadable_and_retried_later(modern):
    """critic: the seal attributes the cutover to the human, so it is never written on an approval the room history
    does not show — an unreadable history (after ``approval_lookup_attempts`` reads) refuses loudly, nothing is
    recorded, and the re-sent record seals once Switch answers again."""
    room = FakeRoom()
    agent, mig, v1 = _ready_to_seal(room, modern)
    real, calls = agent.switch.read_context, []

    def down(room_id, limit=50):
        calls.append(limit)
        raise SwitchError("POST", "read_context", 503, "down")

    agent.switch.read_context = down
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "could not be verified" in out[-1], out[-1]
    assert len(calls) == agent.approval_lookup_attempts == 3
    st = ledger_state(room)
    assert st["sealed"] is None and st["cutover"] is None

    agent.switch.read_context = real
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(f"@user {MARK['ledger_sealed']}"), out[-1]
    assert ledger_state(room)["cutover"]["approval_message_verified"] is True


def test_forged_scorecard_without_a_verifiable_human_approval_never_seals(modern):
    """critic: a compromised cutover-gate controls its own scorecard. Claiming ``approved_by: user`` with no approval
    message, an approval message missing from the history, or an unverified flip must not yield a sealed ledger."""
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, approved=True)
    gate = room.agent("cutover-gate")
    for forged, why in ((dict(approval_message_id=""), "names no approval message"),
                        (dict(approval_message_id="sw_never_posted"), "could not be verified")):
        publish_scorecard(room, rev=v1, mig=mig, **forged)
        gate.send_targeted(record_cutover(v1, mig), ["translator"])
        out = pump(room, agent)
        assert out[-1].startswith(MARK["error"]) and why in out[-1], out[-1]
    approval = room.human_says("@cutover-gate approve-cutover")
    state = publish_scorecard(room, rev=v1, mig=mig, approval_message_id=approval.id)
    state["cutover"]["verified"] = False
    desc, instr = L.DOC_META[L.SCORECARD_DOC]
    gate.upsert_document(name=L.SCORECARD_DOC, description=desc, instructions=instr, content=L.render_scorecard(state))
    gate.send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "not verified through the proxy" in out[-1], out[-1]
    assert ledger_state(room)["sealed"] is None


def test_an_approval_older_than_the_build_cannot_seal_it(modern, monkeypatch):
    """critic: a human approval posted before the build being cut over (e.g. reused from an earlier build by a
    compromised cutover-gate) is not an approval of that build."""
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, approved=True)
    approval = room.human_says("@cutover-gate approve-cutover")
    publish_scorecard(room, rev=v1, mig=mig, approval_message_id=approval.id)
    # pretend the build was deployed 30 s after the approval was posted
    st = ledger_state(room)
    st["builds"][-1]["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(approval.at + 30))
    agent._save_ledger(st)
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "predates the build" in out[-1], out[-1]
    assert ledger_state(room)["sealed"] is None


def test_wrong_approval_message_is_refused_without_retrying(modern):
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, approved=True)
    forged = room.agent_says_raw("fuzzer", "@cutover-gate approve-cutover")  # an agent's approve is no approval
    publish_scorecard(room, rev=v1, mig=mig, approval_message_id=forged.id)
    real, calls = agent.switch.read_context, []
    agent.switch.read_context = lambda room_id, limit=50: calls.append(limit) or real(room_id, limit=limit)
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "posted by fuzzer" in out[-1], out[-1]
    assert len(calls) == 1 and ledger_state(room)["sealed"] is None



def test_replays_from_an_earlier_migration_are_never_applied_to_a_newer_one(modern):
    """codex critic review #4: message-id dedupe only covers the current ledger. A redelivered decision or migrate
    message from migration A, arriving while migration B is current, must change nothing."""
    room = FakeRoom()
    agent, mig_a, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig_a, rev=v1)
    room.human_says(DECIDE)
    old_decision = room._queues["translator"].get_nowait()
    agent.dispatch(old_decision)
    old_migrate = room.messages[0]
    assert old_migrate.body == MIGRATE and len(ledger_state(room)["builds"]) == 2
    time.sleep(0.01)  # Switch timestamps are ms; B's request must be strictly later than A's messages
    room.human_says(MIGRATE)                      # migration B supersedes the unfinished A
    pump(room, agent)
    st = ledger_state(room)
    mig_b, builds = st["migration"]["id"], len(st["builds"])
    assert mig_b != mig_a and builds == 1 and isinstance(st["migration"]["request_timestamp"], int)
    publish_matrix(room, mig=mig_b, rev=st["builds"][-1]["rev"])  # B's campaign reuses the D-n ids
    agent.dispatch(old_decision)                   # redelivered decisions of A
    assert "before migration" in room.bodies("translator")[-1] and "never applied" in room.bodies("translator")[-1]
    room.redeliver(old_migrate, "translator")      # redelivered migrate request of A
    pump(room, agent)
    # refused as the request that already started A (request ids of superseded migrations are kept in the
    # ledger history) — before the timestamp floor is even consulted
    assert f"that request already started migration {mig_a}" in room.bodies("translator")[-1]
    st = ledger_state(room)
    assert st["migration"]["id"] == mig_b and len(st["builds"]) == builds and not st["decisions"]


def test_a_replayed_noop_decision_never_overrides_a_newer_decision(modern):
    """codex critic-hardening #5: a re-posted decision line is a no-op (no trail entry); redelivered after the human
    changed a decision, it must not deploy the old policy again."""
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1)
    room.human_says(DECIDE)
    pump(room, agent)
    time.sleep(0.01)
    noop = room.human_says(DECIDE)                 # the same line again: already applied, nothing changes
    pump(room, agent)
    assert "already applied" in room.bodies("translator")[-1]
    time.sleep(0.01)
    room.human_says("@translator deprecate D-1")   # the human changes their mind
    pump(room, agent)
    st = ledger_state(room)
    builds = len(st["builds"])
    assert st["decisions"]["D-1"]["decision"] == "deprecate" and builds == 3
    room.redeliver(noop, "translator")
    pump(room, agent)
    assert "never overrides a newer one" in room.bodies("translator")[-1], room.bodies("translator")[-1]
    st = ledger_state(room)
    assert st["decisions"]["D-1"]["decision"] == "deprecate" and len(st["builds"]) == builds


def test_seal_refuses_an_approval_posted_before_the_benchmark_finished(modern):
    """codex critic-hardening #3: the gate refuses an approval queued during the benchmark; a compromised gate
    citing that genuine-but-early approval in its scorecard must not get a seal either."""
    room = FakeRoom()
    agent, mig, v1 = migrated(room, modern)
    publish_matrix(room, mig=mig, rev=v1, approved=True)
    early = room.human_says("@cutover-gate approve-cutover")
    state = publish_scorecard(room, rev=v1, mig=mig, approval_message_id=early.id)
    state["benchmarks"][-1]["finished_at_ms"] = int(early.at * 1000) + 500   # the card came out after it
    desc, instr = L.DOC_META[L.SCORECARD_DOC]
    room.agent("cutover-gate").upsert_document(name=L.SCORECARD_DOC, description=desc, instructions=instr,
                                               content=L.render_scorecard(state))
    room.agent("cutover-gate").send_targeted(record_cutover(v1, mig), ["translator"])
    out = pump(room, agent)
    assert out[-1].startswith(MARK["error"]) and "build or benchmark" in out[-1], out[-1]
    assert ledger_state(room)["sealed"] is None
