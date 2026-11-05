"""Regression tests: approval lookup pages back through long room histories, and migrate replay protection
survives superseded migrations, same-millisecond ties and ledgers written before request timestamps existed."""
import time

from agents import ledger as L
from agents.ledger import LEDGER_DOC
from tests.fakes import FakeMessage, FakeRoom
from tests.test_translator_agent import make_agent

MIGRATE = "@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI"


def _post(room: FakeRoom, sender: str, body: str, at: float) -> FakeMessage:
    msg = room._post(sender, body)
    msg.at = at  # deterministic, strictly increasing timestamps (1 ms apart)
    return msg


def test_approval_older_than_one_history_page_is_found(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    agent.HISTORY_PAGE = 200  # > one page of rows: the lookup must really page
    base = time.time() - 3600
    approval = _post(room, "user", "@cutover-gate approve-cutover", base)
    for i in range(1, 451):  # 450 later messages: the approval is ~2 pages back
        _post(room, "fuzzer", f"chatter {i}", base + i / 1000)
    cut = {"approval_message_id": approval.id, "approved_by": "user"}
    assert agent._check_approval(cut) == (True, None)
    # a message that is not the approver's approve-cutover is still a problem, wherever it sits
    other = room.messages[5]
    assert agent._check_approval({"approval_message_id": other.id, "approved_by": "user"})[1]


def test_an_approval_older_than_the_build_is_refused_with_that_reason(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    base = time.time() - 3600
    old = _post(room, "user", "@cutover-gate approve-cutover", base)
    for i in range(1, 451):  # one message every 100 ms: 45 s of history after the old approval
        _post(room, "fuzzer", f"chatter {i}", base + i / 10)
    floor_ms = int((base + 20) * 1000)  # the build is 20 s newer than the old approval
    found, problem = agent._scan_history(old.id, {"approval_message_id": old.id, "approved_by": "user"},
                                         not_before_ms=floor_ms)
    assert found is False and problem and "predates the build" in problem


def test_a_long_transaction_committed_after_the_approval_does_not_end_the_scan(tmp_path):
    """codex re-review #2: a page's oldest sent_at (a transaction START time) does not bound older-seq rows."""
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    floor_ms = int((time.time() - 3600) * 1000)
    for i in range(50):
        _post(room, "fuzzer", f"old {i}", (floor_ms - 60_000 + i) / 1000)
    approval = _post(room, "user", "@cutover-gate approve-cutover", (floor_ms + 100) / 1000)
    _post(room, "fuzzer", "long transaction", (floor_ms - 6000) / 1000)  # started 6 s early, committed later
    for i in range(520):
        _post(room, "fuzzer", f"new {i}", (floor_ms + 200 + i) / 1000)
    cut = {"approval_message_id": approval.id, "approved_by": "user"}
    assert agent._scan_history(approval.id, cut, not_before_ms=floor_ms) == (True, None)


def test_same_millisecond_buckets_up_to_a_page_are_traversed_and_larger_ones_fail_closed(tmp_path):
    """codex re-review #1: Switch pages by timestamp only. Up to HISTORY_PAGE rows in one ms are covered;
    beyond that the approval cannot be reached and the lookup must fail closed (never crash or loop)."""
    for burst, expected in ((300, (True, None)), (600, (False, None))):
        room = FakeRoom()
        agent = make_agent(room, tmp_path / f"lists-{burst}.py")
        t_ms = int((time.time() - 3600) * 1000)
        approval = _post(room, "user", "@cutover-gate approve-cutover", t_ms / 1000)
        for i in range(burst):
            _post(room, "fuzzer", f"burst {i}", t_ms / 1000)  # the approval shares their millisecond
        reads = []
        real = agent.switch.read_context
        agent.switch.read_context = lambda *a, **k: reads.append(1) or real(*a, **k)
        cut = {"approval_message_id": approval.id, "approved_by": "user"}
        assert agent._scan_history(approval.id, cut) == expected, burst
        assert len(reads) <= agent.HISTORY_MAX_READS


def _approval_scenario(room: FakeRoom, base_ms: int, racer_ms: int) -> FakeMessage:
    """50 old messages, then the approval (stamped +105 ms) and a 'racer' committed after it but stamped earlier,
    then 199 newer messages — so the page boundary falls between the racer and the approval."""
    for i in range(50):
        _post(room, "fuzzer", f"old {i}", (base_ms + i) / 1000)
    approval = _post(room, "user", "@cutover-gate approve-cutover", (base_ms + 105) / 1000)
    _post(room, "fuzzer", "racer", racer_ms / 1000)
    for i in range(199):
        _post(room, "fuzzer", f"new {i}", (base_ms + 200 + i) / 1000)
    return approval


def test_commit_order_and_timestamp_inversion_cannot_hide_the_approval(tmp_path):
    """Claude reviewer #3: Switch returns rows by commit seq but filters on sent_at (transaction START time)."""
    base_ms = int((time.time() - 3600) * 1000)
    for floor in (0, base_ms + 104):
        room = FakeRoom()
        agent = make_agent(room, tmp_path / f"lists-{floor}.py")
        agent.HISTORY_PAGE = 200  # the scenario puts the inversion at a 200-row page boundary
        approval = _approval_scenario(room, base_ms, base_ms + 102)
        cut = {"approval_message_id": approval.id, "approved_by": "user"}
        assert agent._scan_history(approval.id, cut, not_before_ms=floor) == (True, None), floor


def test_a_millisecond_shared_across_the_page_boundary_hides_nothing(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    agent.HISTORY_PAGE = 200  # > one page of rows: the lookup must really page
    base_ms = int((time.time() - 3600) * 1000)
    for i in range(50):
        _post(room, "fuzzer", f"old {i}", (base_ms + i) / 1000)
    approval = None
    for j in range(5):  # five messages in one millisecond, straddling the first page boundary
        m = _post(room, "user" if j == 0 else "fuzzer", "@cutover-gate approve-cutover" if j == 0 else "x",
                  (base_ms + 100) / 1000)
        approval = approval or m
    for i in range(198):
        _post(room, "fuzzer", f"new {i}", (base_ms + 200 + i) / 1000)
    assert agent._check_approval({"approval_message_id": approval.id, "approved_by": "user"}) == (True, None)


def _seed_ledger(room: FakeRoom, *, request_id: str, request_ts, history_ids=()) -> None:
    state = L.new_ledger(migration_id="MIG-OLD", path="/people/{user}/lists", source="legacy_openlibrary",
                         target="FastAPI", requested_by="user", request_message_id=request_id)
    if request_ts is not None:
        state["migration"]["request_timestamp"] = request_ts
    state["history"] = [{"id": f"MIG-H{i}", "request_message_id": hid, "request_timestamp": 1}
                        for i, hid in enumerate(history_ids)]
    desc, instr = L.DOC_META[LEDGER_DOC]
    room.agent("translator").create_document(name=LEDGER_DOC, description=desc, instructions=instr,
                                             content=L.render_ledger(state))


def _replies(room: FakeRoom, agent, msg: FakeMessage) -> list[str]:
    n = len(room.messages)
    agent.dispatch(room._event(msg))
    return [m.body for m in room.messages[n:] if m.sender == "translator" and not m.body.startswith("📄")]


def test_request_of_a_superseded_migration_is_never_replayed(tmp_path):
    room = FakeRoom()
    _seed_ledger(room, request_id="sw_current", request_ts=2_000, history_ids=["sw_superseded"])
    agent = make_agent(room, tmp_path / "lists.py")
    replay = FakeMessage(id="sw_superseded", sender="user", body=MIGRATE, at=9_999)  # even if it looks newer
    out = _replies(room, agent, replay)
    assert out and "already started migration MIG-H0" in out[0]
    assert not (tmp_path / "lists.py").exists()


def test_same_millisecond_request_is_stale(tmp_path):
    room = FakeRoom()
    _seed_ledger(room, request_id="sw_current", request_ts=5_000)
    agent = make_agent(room, tmp_path / "lists.py")
    out = _replies(room, agent, FakeMessage(id="sw_twin", sender="user", body=MIGRATE, at=5.0))  # 5 000 ms
    assert out and "same millisecond" in out[0] and not (tmp_path / "lists.py").exists()


def test_unstamped_ledger_falls_back_to_requested_at(tmp_path):
    room = FakeRoom()
    _seed_ledger(room, request_id="sw_current", request_ts=None)  # a ledger written before timestamps were kept
    agent = make_agent(room, tmp_path / "lists.py")
    out = _replies(room, agent, FakeMessage(id="sw_old", sender="user", body=MIGRATE, at=1.0))  # 1970
    assert out and "before migration MIG-OLD began" in out[0] and not (tmp_path / "lists.py").exists()


def test_a_full_page_within_one_millisecond_does_not_hide_an_older_approval(tmp_path):
    """codex lead-final-delta #2: 200+ messages sharing one ms used to stall the cursor before the approval."""
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    agent.HISTORY_PAGE = 200  # > one page of rows: the lookup must really page
    base = time.time() - 3600
    approval = _post(room, "user", "@cutover-gate approve-cutover", base)
    for i in range(250):
        _post(room, "fuzzer", f"burst {i}", base + 0.5)   # one millisecond, more than a page
    assert agent._check_approval({"approval_message_id": approval.id, "approved_by": "user"}) == (True, None)


def _dense_room_with_a_long_transaction(room: FakeRoom) -> FakeMessage:
    """Claude re-reviewer's trace: 1500 old rows 10 ms apart, the approval A, 1200 newer rows; after the 499th
    newer row a transaction that started 6 s before A commits (its sent_at drags a page's minimum far back)."""
    a_ms = int((time.time() - 7200) * 1000)
    for i in range(1500):
        _post(room, "fuzzer", f"old {i}", (a_ms - 15_000 + i * 10) / 1000)
    approval = _post(room, "user", "@cutover-gate approve-cutover", a_ms / 1000)
    for i in range(1200):
        _post(room, "fuzzer", f"new {i}", (a_ms + 10 + i * 10) / 1000)
        if i == 499:
            _post(room, "fuzzer", "long transaction", (a_ms - 6000) / 1000)
    return approval


def test_recorded_approval_timestamp_finds_it_in_one_read(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    approval = _dense_room_with_a_long_transaction(room)
    reads = []
    real = agent.switch.read_context
    agent.switch.read_context = lambda *a, **k: reads.append(k) or real(*a, **k)
    cut = {"approval_message_id": approval.id, "approved_by": "user",
           "approval_timestamp_ms": int(approval.at * 1000)}
    assert agent._check_approval(cut) == (True, None) and len(reads) == 1


def test_a_wrong_recorded_timestamp_cannot_fake_a_verification(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    approval = _dense_room_with_a_long_transaction(room)
    impostor = room.messages[10]  # a fuzzer message: the window holds it, but it is not the human's approval
    cut = {"approval_message_id": impostor.id, "approved_by": "user", "approval_timestamp_ms": int(impostor.at * 1000)}
    found, problem = agent._check_approval(cut)
    assert found is False and problem and "not an `approve-cutover`" in problem
    assert approval.id != impostor.id


def test_fallback_scan_without_a_recorded_timestamp_terminates_and_never_forges(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    approval = _dense_room_with_a_long_transaction(room)
    reads = []
    real = agent.switch.read_context
    agent.switch.read_context = lambda *a, **k: reads.append(k) or real(*a, **k)
    found, problem = agent._scan_history(approval.id, {"approval_message_id": approval.id, "approved_by": "user"})
    assert problem is None and found in (True, False) and len(reads) <= agent.HISTORY_MAX_READS
    befores = [r["before"] for r in reads if "before" in r]
    assert befores == sorted(befores, reverse=True) and len(set(befores)) == len(befores)  # strictly backwards


def test_fake_read_context_reports_the_page_minimum_like_switch():
    room = FakeRoom()
    late = _post(room, "fuzzer", "committed first, stamped late", 2.0)
    early = _post(room, "fuzzer", "committed later, stamped early", 1.0)
    ctx = room.agent("translator").read_context(room.room_id, limit=2)
    assert ctx["oldest_timestamp"] == 1000 and late.id != early.id


def test_fallback_does_not_skip_a_partly_read_timestamp_bucket(tmp_path):
    """codex final re-review #1: 100 rows at T-1000, 450 at T (the approval first), 100 at T+100, 400 at
    T+1100 — every bucket fits a page, so the fallback scan must find the approval."""
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    t = int((time.time() - 3600) * 1000)
    for i in range(100):
        _post(room, "fuzzer", f"older {i}", (t - 1000) / 1000)
    approval = _post(room, "user", "@cutover-gate approve-cutover", t / 1000)
    for i in range(449):
        _post(room, "fuzzer", f"bucket {i}", t / 1000)
    for i in range(100):
        _post(room, "fuzzer", f"t+100 {i}", (t + 100) / 1000)
    for i in range(400):
        _post(room, "fuzzer", f"t+1100 {i}", (t + 1100) / 1000)
    reads = []
    real = agent.switch.read_context
    agent.switch.read_context = lambda *a, **k: reads.append(k) or real(*a, **k)
    assert agent._scan_history(approval.id, {"approval_message_id": approval.id, "approved_by": "user"}) == (True, None)
    befores = [r["before"] for r in reads if "before" in r]
    assert befores == sorted(befores, reverse=True) and len(set(befores)) == len(befores)


def test_fallback_advances_through_a_dense_room_within_budget(tmp_path):
    """One message per millisecond for 30 000 ms: the scan must still reach an approval at the very start."""
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    t = int((time.time() - 7200) * 1000)
    approval = _post(room, "user", "@cutover-gate approve-cutover", (t - 1) / 1000)
    for i in range(30_000):
        _post(room, "fuzzer", f"m {i}", (t + i) / 1000)
    reads = []
    real = agent.switch.read_context
    agent.switch.read_context = lambda *a, **k: reads.append(k) or real(*a, **k)
    assert agent._scan_history(approval.id, {"approval_message_id": approval.id, "approved_by": "user"}) == (True, None)
    assert len(reads) < agent.HISTORY_MAX_READS


def test_a_missed_or_unusable_recorded_timestamp_falls_back_to_the_scan(tmp_path):
    """Claude final re-review: the recorded timestamp may only narrow the search — a window that misses the
    approval (here: a timestamp older than it) or a corrupt value must fall back to the scan and still verify."""
    for bad_ts in ("older", 10**18, -(10**15), 2**63):
        room = FakeRoom()
        agent = make_agent(room, tmp_path / f"lists-{bad_ts}.py")
        agent.HISTORY_PAGE = 50
        base = time.time() - 3600
        for i in range(120):
            _post(room, "fuzzer", f"before {i}", base + i / 1000)
        approval = _post(room, "user", "@cutover-gate approve-cutover", base + 0.5)
        for i in range(120):
            _post(room, "fuzzer", f"after {i}", base + 0.6 + i / 1000)
        ts = int((base + 0.05) * 1000) if bad_ts == "older" else bad_ts
        reads = []
        real = agent.switch.read_context
        agent.switch.read_context = lambda *a, **k: reads.append(k) or real(*a, **k)
        cut = {"approval_message_id": approval.id, "approved_by": "user", "approval_timestamp_ms": ts}
        assert agent._check_approval(cut) == (True, None), bad_ts
        assert len(reads) > 1, bad_ts  # it really fell back to the scan


def test_a_non_conforming_page_stops_the_scan_fail_closed(tmp_path):
    room = FakeRoom()
    agent = make_agent(room, tmp_path / "lists.py")
    pages = iter([{"threads": [], "truncated": True, "oldest_timestamp": 1000},
                  {"threads": [], "truncated": True, "oldest_timestamp": 5000}])  # later than the cursor
    agent.switch.read_context = lambda *a, **k: next(pages)
    assert agent._scan_history("sw_x", {"approval_message_id": "sw_x", "approved_by": "user"}) == (False, None)
