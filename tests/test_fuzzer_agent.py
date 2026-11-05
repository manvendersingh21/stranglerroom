"""Tests for the @fuzzer room agent (``agents/fuzzer_agent.py``, docs/AGENTS_SPEC.md §2).

The agent runs in-process against a :class:`tests.fakes.FakeRoom` (faithful Switch semantics) and REAL
targets started as subprocesses on free ports: the legacy web.py service and two modern FastAPI services
(reloader off), one serving the naive v1 module and one the SPEC §3 demo-policy module, each from its own
``MODERN_LISTS_PATH`` tmp file (never the real ``targets/modern_fastapi/app/lists.py``). The divergence
registry lives in ``tmp_path``. Campaigns use ``n_random=30`` to stay fast.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents import ledger
from agents.codegen import QUIRK_TAGS, module_rev, render_lists_module
from agents.commands import MARK, re_verify, shadow_test
from agents.divergence import Registry
from agents.fuzzer_agent import FuzzerAgent
from agents.provision import RoomInfo
from agents.switch_client import Settings
from tests.fakes import FakeRoom, FakeSwitchAgent
from tests.procs import free_port, service

DEMO_POLICY = {"json_suffix": "preserve", "loose_query": "preserve", "non_positive_quantity": "preserve",
               "missing_resource_200": "deprecate", "empty_body_500": "deprecate"}
DEMO_DECISIONS = {"D-1": "preserve", "D-2": "preserve", "D-3": "deprecate", "D-4": "preserve", "D-5": "deprecate"}
IDS = ["D-1", "D-2", "D-3", "D-4", "D-5"]
MIG_A, MIG_B = "MIG-20260101-000000", "MIG-20260202-000000"
N_RANDOM = 30


# ------------------------------------------------------------------------------------ targets

def _modern(tmp_dir: Path, source: str):
    path = tmp_dir / "lists.py"
    path.write_text(source)
    return service("targets.modern_fastapi.server", free_port(),
                   env={"MODERN_NO_RELOAD": "1", "MODERN_LISTS_PATH": str(path)})


@pytest.fixture(scope="module")
def targets(tmp_path_factory):
    """legacy URL + {"naive": (url, rev), "demo": (url, rev)}."""
    naive_src = render_lists_module({})
    demo_src = render_lists_module(DEMO_POLICY)
    with service("targets.legacy_openlibrary.server", free_port()) as legacy, \
            _modern(tmp_path_factory.mktemp("modern-naive"), naive_src) as naive, \
            _modern(tmp_path_factory.mktemp("modern-demo"), demo_src) as demo:
        yield {"legacy": legacy, "naive": (naive, module_rev(naive_src)), "demo": (demo, module_rev(demo_src))}


# ------------------------------------------------------------------------------------ helpers

SETTINGS = Settings(switch_url="http://switch.invalid", registration_token="x", admin_email="x", admin_password="x",
                    mattermost_url="http://mm.invalid", mattermost_team="switch", mattermost_user="user",
                    mattermost_user_password="x")


def make_fuzzer(room: FakeRoom, targets: dict, registry: Path, *, modern: str = "naive", **kw) -> FuzzerAgent:
    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id=None, channel_url=None,
                    agents=dict(room.agent_ids), human=room.human)
    agent = FuzzerAgent(settings=SETTINGS, room=info, switch=room.agent("fuzzer"), legacy_url=targets["legacy"],
                        modern_url=targets[modern][0], registry_path=registry, n_random=N_RANDOM, **kw)
    agent.on_connected()
    return agent


def write_ledger(room: FakeRoom, migration: str, decisions: dict[str, str] | None = None) -> None:
    """Create/update the translator's ledger exactly like the translator would (it is the only writer)."""
    state = ledger.new_ledger(migration_id=migration, path="/people/{user}/lists", source="legacy_openlibrary",
                              target="FastAPI", requested_by=room.human, request_message_id="sw_req")
    tags = dict(zip(IDS, QUIRK_TAGS))
    state["decisions"] = {cid: {"decision": d, "tag": tags.get(cid), "by": room.human, "message_id": "sw_dec",
                                "at": ledger.now_iso()} for cid, d in (decisions or {}).items()}
    description, instructions = ledger.DOC_META[ledger.LEDGER_DOC]
    room.agent("translator").upsert_document(name=ledger.LEDGER_DOC, description=description,
                                             instructions=instructions, content=ledger.render_ledger(state))


def translator_says(room: FakeRoom, fuzzer: FuzzerAgent, body: str) -> None:
    room.agent("translator").send_targeted(body, ["fuzzer"])
    room.pump({"fuzzer": fuzzer})


def fuzzer_posts(room: FakeRoom, since: int = 0) -> list[str]:
    return [m.body for m in room.messages[since:] if m.sender == "fuzzer" and not m.body.startswith("📄")]


def matrix_state(room: FakeRoom) -> dict:
    doc = room.doc_by_name(ledger.MATRIX_DOC)
    assert doc is not None, "matrix document was not created"
    assert doc["created_by_agent_id"] == room.agent_ids["fuzzer"]
    return ledger.extract_state(doc["content"])


def clusters(run: dict) -> list[tuple[str, str, str]]:
    return [(c["id"], c["tag"], c["status"]) for c in run["report"]["clusters"]]


def assert_mention_hygiene(room: FakeRoom) -> None:
    """The fuzzer only ever addresses the human (decision request) and cutover-gate (handoff)."""
    for m in room.messages:
        if m.sender == "fuzzer":
            assert set(m.addressed) <= {room.human, "cutover-gate"}, m.body
            if room.human in m.addressed:
                assert m.body.startswith(f"@{room.human} {MARK['decision_needed']}"), m.body
            if "cutover-gate" in m.addressed:
                assert m.body.startswith("@cutover-gate benchmark rev="), m.body


@pytest.fixture
def room() -> FakeRoom:
    return FakeRoom()


@pytest.fixture
def registry(tmp_path) -> Path:
    return tmp_path / "divergence_registry.json"


# ------------------------------------------------------------------------------------ the happy path

def test_naive_v1_finds_five_open_clusters_then_demo_policy_reaches_approved_parity(room, registry, targets):
    naive_url, naive_rev = targets["naive"]
    demo_url, demo_rev = targets["demo"]
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)

    # ---- v1: naive textbook port --------------------------------------------------------
    translator_says(room, fuzzer, shadow_test(naive_rev, MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 2, posts
    campaign, ask = posts
    assert campaign.startswith(f"{MARK['campaign']} #1 on rev `{naive_rev}`")
    assert all(f"| {cid} |" in campaign for cid in IDS)
    assert "D-6" not in campaign
    assert ask.startswith(f"@{room.human} {MARK['decision_needed']} on D-1 D-2 D-3 D-4 D-5")
    assert "@translator" not in ask and "translator" in ask
    assert "preserve D-1 D-2 D-4 and deprecate D-3 D-5" in ask

    state = matrix_state(room)
    assert state["migration"] == MIG_A and len(state["runs"]) == 1
    run = state["runs"][0]
    assert run["rev"] == naive_rev and run["decisions"] == {}
    assert run["trigger"]["by"] == "translator" and run["trigger"]["migration"] == MIG_A
    assert clusters(run) == [(cid, tag, "open") for cid, tag in zip(IDS, QUIRK_TAGS)]
    assert not run["report"]["approved_parity"]

    # ---- v2: the human decided, the translator applied the demo policy -------------------
    write_ledger(room, MIG_A, DEMO_DECISIONS)
    fuzzer.modern_url = demo_url  # the translator's v2 is served by the demo-policy target
    mark = len(room.messages)
    translator_says(room, fuzzer, re_verify(demo_rev, MIG_A, DEMO_DECISIONS))
    posts = fuzzer_posts(room, mark)
    assert len(posts) == 3, posts
    campaign, approved, handoff = posts
    assert campaign.startswith(f"{MARK['campaign']} #2 on rev `{demo_rev}`")
    assert approved.startswith(f"{MARK['approved_parity']} (5/5) on rev `{demo_rev}`")
    assert handoff == f"@cutover-gate benchmark rev={demo_rev} migration={MIG_A}"
    assert room.pending("cutover-gate") == 1

    state = matrix_state(room)
    assert [r["run"] for r in state["runs"]] == [1, 2]
    run = state["runs"][-1]
    assert run["decisions"] == DEMO_DECISIONS and run["report"]["approved_parity"]
    expected = {"preserve": "preserved-ok", "deprecate": "deprecated-ok"}
    assert clusters(run) == [(cid, tag, expected[DEMO_DECISIONS[cid]]) for cid, tag in zip(IDS, QUIRK_TAGS)]
    assert run["report"]["parity_label"] == "100% Approved Parity (5/5)"
    assert_mention_hygiene(room)


def test_reverify_decisions_read_from_ledger_when_handoff_carries_none(room, registry, targets):
    """A human-triggered re-verify (no decisions in the message) uses the ledger's decisions."""
    naive_rev = targets["naive"][1]
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A, {"D-1": "preserve"})
    request = room.human_says(f"@fuzzer re-verify rev={naive_rev} migration={MIG_A}")
    room.pump({"fuzzer": fuzzer})
    run = matrix_state(room)["runs"][-1]
    assert run["decisions"] == {"D-1": "preserve"}
    assert run["trigger"] == {"by": room.human, "kind": "human", "command": "re-verify", "migration": MIG_A,
                              "message_id": request.id}
    assert ("D-1", "json_suffix", "preserved-regressed") in clusters(run)
    ask = fuzzer_posts(room)[-1]
    assert ask.startswith(f"@{room.human} {MARK['decision_needed']}")
    assert "D-1 (preserved-regressed)" in ask
    assert_mention_hygiene(room)


# ------------------------------------------------------------------------------------ preconditions

def test_rev_mismatch_warns_and_runs_no_campaign(room, registry, targets):
    fuzzer = make_fuzzer(room, targets, registry, rev_timeout=1.0, rev_poll=0.2)  # naive target
    write_ledger(room, MIG_A)
    translator_says(room, fuzzer, shadow_test(targets["demo"][1], MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"])
    assert f"did not serve rev `{targets['demo'][1]}`" in posts[0]
    assert f"rev `{targets['naive'][1]}`" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    assert not registry.exists()


def test_decisions_mismatch_between_handoff_and_ledger_warns(room, registry, targets):
    naive_rev = targets["naive"][1]
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A, {"D-1": "preserve", "D-3": "deprecate"})
    translator_says(room, fuzzer, re_verify(naive_rev, MIG_A, {"D-1": "preserve", "D-3": "preserve"}))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(f"{MARK['error']} decisions mismatch")
    assert "preserve D-1 D-3" in posts[0] and "preserve D-1 and deprecate D-3" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None


def test_ledger_of_another_migration_warns(room, registry, targets):
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_B)
    translator_says(room, fuzzer, shadow_test(targets["naive"][1], MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and MIG_B in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None


def test_ledger_not_written_by_translator_is_not_trusted(room, registry, targets):
    """A same-named ledger created by another agent (impersonation) must not feed decisions."""
    fuzzer = make_fuzzer(room, targets, registry)
    state = ledger.new_ledger(migration_id=MIG_A, path="/x", source="legacy_openlibrary", target="FastAPI",
                              requested_by="user", request_message_id=None)
    room.agent("cutover-gate").create_document(name=ledger.LEDGER_DOC, description="d", instructions="i",
                                               content=ledger.render_ledger(state))
    translator_says(room, fuzzer, shadow_test(targets["naive"][1], MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "not created by the translator" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None


def test_missing_rev_or_migration_is_rejected(room, registry, targets):
    fuzzer = make_fuzzer(room, targets, registry)
    room.human_says("@fuzzer shadow-test")
    room.human_says(f"@fuzzer shadow-test rev={targets['naive'][1]}")
    room.pump({"fuzzer": fuzzer})
    posts = fuzzer_posts(room)
    assert len(posts) == 2 and all(p.startswith(MARK["error"]) and "migration=MIG-" in p for p in posts)


# ------------------------------------------------------------------------------------ migrations + restart

def test_new_migration_resets_matrix_and_divergence_ids(room, registry, targets):
    naive_rev = targets["naive"][1]
    demo_url, demo_rev = targets["demo"]
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)
    translator_says(room, fuzzer, shadow_test(naive_rev, MIG_A))
    assert [c[0] for c in clusters(matrix_state(room)["runs"][-1])] == IDS

    # A new migration on the demo build: only its two deprecated quirks still diverge from legacy. With a
    # reset registry they are numbered D-1, D-2 (without the reset they would stay D-3, D-5).
    write_ledger(room, MIG_B)
    fuzzer.modern_url = demo_url
    mark = len(room.messages)
    translator_says(room, fuzzer, shadow_test(demo_rev, MIG_B))
    state = matrix_state(room)
    assert state["migration"] == MIG_B
    assert [r["run"] for r in state["runs"]] == [1]
    assert clusters(state["runs"][0]) == [("D-1", "missing_resource_200", "open"), ("D-2", "empty_body_500", "open")]
    posts = fuzzer_posts(room, mark)
    assert posts[0].startswith(f"{MARK['campaign']} #1 on rev `{demo_rev}`") and "ids restart at D-1" in posts[0]
    assert posts[1].startswith(f"@{room.human} {MARK['decision_needed']} on D-1 D-2")
    assert "deprecate D-1 D-2" in posts[1]


def test_rehydrates_matrix_after_restart(room, registry, targets, monkeypatch):
    naive_rev = targets["naive"][1]
    first = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)
    translator_says(room, first, shadow_test(naive_rev, MIG_A))
    before = matrix_state(room)

    second = make_fuzzer(room, targets, registry)  # same FakeRoom + agent identity: a restarted process
    assert second.matrix == before and second.migration == MIG_A
    mark = len(room.messages)
    room.human_says("@fuzzer status")
    room.pump({"fuzzer": second})
    status = fuzzer_posts(room, mark)[0]
    assert f"campaign #1 on rev `{naive_rev}`" in status and MIG_A in status and "D-5 (open)" in status

    # Same migration after restart: history continues, the committed registry is never reset.
    resets: list[Path] = []
    real_reset = Registry.reset
    monkeypatch.setattr(Registry, "reset", lambda self: (resets.append(self.path), real_reset(self))[1])
    translator_says(room, second, shadow_test(naive_rev, MIG_A))
    assert registry not in resets
    state = matrix_state(room)
    assert [r["run"] for r in state["runs"]] == [1, 2]
    assert [c[0] for c in clusters(state["runs"][-1])] == IDS
    assert "ids restart" not in fuzzer_posts(room, mark)[1]


def test_untrusted_matrix_document_is_not_rehydrated(room, registry, targets):
    room.agent("translator").create_document(name=ledger.MATRIX_DOC, description="d", instructions="i",
                                             content=ledger.render_matrix(ledger.new_matrix()))
    fuzzer = make_fuzzer(room, targets, registry)
    assert fuzzer.migration is None and fuzzer.matrix == ledger.new_matrix()
    assert fuzzer_posts(room)[0].startswith(MARK["error"])


# ------------------------------------------------------------------------------------ chatter / governance

def test_ignores_other_agents_and_answers_status_help(room, registry, targets):
    fuzzer = make_fuzzer(room, targets, registry)
    room.agent("cutover-gate").send_targeted(shadow_test(targets["naive"][1], MIG_A), ["fuzzer"])
    room.agent("cutover-gate").send_targeted("great job, thanks", ["fuzzer"])
    room.agent("translator").send_targeted("📄 updated the ledger", ["fuzzer"])
    room.pump({"fuzzer": fuzzer})
    assert fuzzer_posts(room) == []
    assert room.doc_by_name(ledger.MATRIX_DOC) is None

    room.human_says("@fuzzer help")
    room.human_says("@fuzzer status")
    room.human_says("@fuzzer what's up")
    room.pump({"fuzzer": fuzzer})
    help_, status, unknown = fuzzer_posts(room)
    assert "shadow-test rev=" in help_ and "no campaign yet" in status and "help" in unknown
    assert_mention_hygiene(room)
    assert all("@" not in p for p in fuzzer_posts(room))


# ------------------------------------------------------------------------------------ state consistency

def test_failed_campaign_for_a_new_migration_keeps_the_committed_registry(room, registry, targets):
    """A new migration whose campaign cannot run must not wipe/renumber the current migration's ids."""
    naive_rev = targets["naive"][1]
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)
    translator_says(room, fuzzer, shadow_test(naive_rev, MIG_A))
    committed = Registry(registry).all()
    assert [e["id"] for e in committed.values()] == IDS

    write_ledger(room, MIG_B)
    fuzzer.legacy_url = f"http://127.0.0.1:{free_port()}"  # legacy down -> CampaignError
    mark = len(room.messages)
    translator_says(room, fuzzer, shadow_test(naive_rev, MIG_B))
    posts = fuzzer_posts(room, mark)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "could not run" in posts[0]
    assert Registry(registry).all() == committed
    assert fuzzer.migration == MIG_A and matrix_state(room)["migration"] == MIG_A
    assert not list(registry.parent.glob("*.staging"))


def test_lost_registry_is_restored_from_the_matrix_on_restart(room, registry, targets):
    naive_rev = targets["naive"][1]
    first = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)
    translator_says(room, first, shadow_test(naive_rev, MIG_A))
    committed = Registry(registry).all()
    assert matrix_state(room)["registry"] == committed

    registry.write_text("{corrupt")  # Registry._load silently treats this as empty
    mark = len(room.messages)
    second = make_fuzzer(room, targets, registry)
    notice = fuzzer_posts(room, mark)
    assert len(notice) == 1 and notice[0].startswith("ℹ️") and "restored its 5 cluster id(s)" in notice[0]
    assert Registry(registry).all() == committed

    # A stale extra entry (e.g. left by another migration) is not tolerated either.
    Registry(registry).id_for("unclassified:GET:/x:200:500", 500)
    mark = len(room.messages)
    make_fuzzer(room, targets, registry)
    assert "restored" in fuzzer_posts(room, mark)[0]
    assert Registry(registry).all() == committed


def test_matrix_document_unreadable_at_startup_is_announced_and_blocks_campaigns(room, registry, targets):
    naive_rev = targets["naive"][1]
    first = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)
    translator_says(room, first, shadow_test(naive_rev, MIG_A))

    class Flaky(FakeSwitchAgent):
        def load_documents(self, ids):
            raise RuntimeError("switch is down")

    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id=None, channel_url=None,
                    agents=dict(room.agent_ids), human=room.human)
    second = FuzzerAgent(settings=SETTINGS, room=info, switch=Flaky(room, "fuzzer"), legacy_url=targets["legacy"],
                         modern_url=targets["naive"][0], registry_path=registry, n_random=N_RANDOM)
    mark = len(room.messages)
    second.on_connected()
    room.human_says(f"@fuzzer shadow-test rev={naive_rev} migration={MIG_A}")
    room.pump({"fuzzer": second})
    posts = fuzzer_posts(room, mark)
    assert len(posts) == 2 and all(p.startswith(MARK["error"]) for p in posts), posts
    assert "will retry" in posts[0] and "switch is down" in posts[1]
    assert len(matrix_state(room)["runs"]) == 1
    room.human_says("@fuzzer status")
    room.pump({"fuzzer": second})
    status = fuzzer_posts(room)[-1]
    assert status.startswith(f"{MARK['error']} fuzzer status unknown") and "switch is down" in status


def test_malformed_matrix_run_is_not_rehydrated(room, registry, targets):
    room.agent("fuzzer").create_document(name=ledger.MATRIX_DOC, description="d", instructions="i",
                                         content=ledger.embed_state("# m", {"kind": "matrix", "runs": [1]}))
    fuzzer = make_fuzzer(room, targets, registry)
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "run entry 1 is malformed" in posts[0]
    assert fuzzer.migration is None and fuzzer.matrix == ledger.new_matrix()


def test_redelivered_event_runs_only_one_campaign(room, registry, targets):
    """Switch may replay an addressed event after a reconnect/restart: no second campaign or handoff."""
    naive_rev = targets["naive"][1]
    fuzzer = make_fuzzer(room, targets, registry)
    write_ledger(room, MIG_A)
    room.agent("translator").send_targeted(shadow_test(naive_rev, MIG_A), ["fuzzer"])
    event = room._queues["fuzzer"].get()
    fuzzer.dispatch(event)
    mark = len(room.messages)
    make_fuzzer(room, targets, registry).dispatch(event)  # restarted process, replayed event
    posts = fuzzer_posts(room, mark)
    assert len(posts) == 1 and posts[0].startswith("ℹ️") and "already handled by campaign #1" in posts[0]
    assert len(matrix_state(room)["runs"]) == 1


def test_lost_matrix_with_ledger_decisions_does_not_misapply_them(room, registry, targets):
    """Matrix lost -> the migration looks new and ids are renumbered; the ledger's tags expose it."""
    demo_url, demo_rev = targets["demo"]
    room.agent("fuzzer").create_document(name=ledger.MATRIX_DOC, description="d", instructions="i",
                                         content="corrupt")
    fuzzer = make_fuzzer(room, targets, registry, modern="demo")
    write_ledger(room, MIG_A, DEMO_DECISIONS)
    mark = len(room.messages)
    translator_says(room, fuzzer, re_verify(demo_rev, MIG_A, DEMO_DECISIONS))
    posts = fuzzer_posts(room, mark)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "not recorded and not approved" in posts[0]
    assert "D-1 is `missing_resource_200` here but the human decided on `json_suffix`" in posts[0]
    assert room.pending("cutover-gate") == 0
    assert extract_or_none(room) is None
    assert not registry.exists()


def extract_or_none(room: FakeRoom):
    doc = room.doc_by_name(ledger.MATRIX_DOC)
    return ledger.extract_state(doc["content"]) if doc else None
