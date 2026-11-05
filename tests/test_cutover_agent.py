"""Tests for ``@cutover-gate`` (agents/cutover_agent.py, docs/AGENTS_SPEC.md §2).

A :class:`tests.fakes.FakeRoom` stands in for Switch; the targets are REAL subprocesses on free ports:
legacy web.py, modern FastAPI serving the demo-policy module rendered by :mod:`agents.codegen` into a tmp
``MODERN_LISTS_PATH`` (never the real ``app/lists.py``), and the strangler proxy with a tmp
``ROUTER_STATE_FILE`` and a test-only admin token. Benchmarks are small (40 requests, concurrency 8).
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from agents import proxy_control
from agents.codegen import module_rev, render_lists_module
from agents.commands import MARK, mentions
from agents.cutover_agent import BENCH_PATHS, CutoverAgent
from agents.ledger import (
    DOC_META, MATRIX_DOC, SCORECARD_DOC, add_run, extract_state, new_matrix, render_matrix,
)
from agents.provision import RoomInfo
from agents.switch_client import Settings
from tests.fakes import FakeRoom
from tests.procs import free_port, service

TOKEN = "test-cutover-router-token"
MIG = "MIG-20261105-120000"
DEMO_POLICY = {"json_suffix": "preserve", "loose_query": "preserve", "non_positive_quantity": "preserve",
               "missing_resource_200": "deprecate", "empty_body_500": "deprecate"}


# ----------------------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def targets(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cutover-targets")
    source = render_lists_module(DEMO_POLICY, header_comment="test_cutover_agent demo policy")
    lists_path = tmp / "lists.py"
    lists_path.write_text(source)
    with service("targets.legacy_openlibrary.server", free_port(),
                 args=["--log-file", str(tmp / "legacy.log")]) as lu, \
         service("targets.modern_fastapi.server", free_port(),
                 env={"MODERN_NO_RELOAD": "1", "MODERN_LISTS_PATH": str(lists_path)}) as mu:
        meta = httpx.get(mu + "/_meta").json()
        assert meta["rev"] == module_rev(source) and not meta.get("import_error")
        yield {"legacy": lu, "modern": mu, "rev": meta["rev"]}


@pytest.fixture()
def stack(targets, tmp_path):
    with service("targets.proxy.router", free_port(), health="/_router/status",
                 env={"LEGACY_URL": targets["legacy"], "MODERN_URL": targets["modern"],
                      "ROUTER_STATE_FILE": str(tmp_path / "router.json"), "ROUTER_ADMIN_TOKEN": TOKEN}) as pu:
        yield {**targets, "proxy": pu}


@pytest.fixture()
def room():
    return FakeRoom()


def make_agent(room: FakeRoom, stack: dict) -> CutoverAgent:
    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id=None, channel_url=None,
                    agents=dict(room.agent_ids), human=room.human)
    settings = Settings(**{f.name: "unused-in-tests" for f in dataclasses.fields(Settings)})
    agent = CutoverAgent(settings=settings, room=info, switch=room.agent("cutover-gate"),
                         legacy_url=stack["legacy"], modern_url=stack["modern"], proxy_url=stack["proxy"],
                         router_token=TOKEN, bench_total=40, bench_concurrency=8)
    agent.on_connected()
    return agent


def publish_matrix(room: FakeRoom, rev: str, *, approved: bool = True, author: str = "fuzzer",
                   migration: str = MIG) -> None:
    """The fuzzer's Parity Audit Matrix with one (synthetic) campaign run on ``rev``."""
    state = new_matrix()
    label = "100% Approved Parity (5/5)" if approved else "Parity 60% (3/5 clusters resolved)"
    add_run(state, rev=rev, trigger={"by": "translator", "kind": "agent", "migration": migration},
            decisions={}, report={"parity_label": label, "approved_parity": approved, "total_requests": 180,
                                  "identical": 180 if approved else 170, "divergent": 0 if approved else 10,
                                  "duration_s": 1.5, "clusters": []})
    desc, instr = DOC_META[MATRIX_DOC]
    room.agent(author).upsert_document(name=MATRIX_DOC, description=desc, instructions=instr,
                                       content=render_matrix(state))


def gate_posts(room: FakeRoom) -> list[str]:
    """What cutover-gate said, minus the 📄 document notices."""
    return [b for b in room.bodies("cutover-gate") if not b.startswith("📄")]


def scorecard(room: FakeRoom) -> dict:
    doc = room.doc_by_name(SCORECARD_DOC)
    assert doc is not None, "no scorecard document"
    assert doc["created_by_agent_id"] == room.agent_ids["cutover-gate"]
    return extract_state(doc["content"])


def proxy_target(stack: dict) -> str:
    return proxy_control.status(stack["proxy"])["target"]


def say_and_pump(room: FakeRoom, agent: CutoverAgent, body: str, *, sender: str | None = None):
    msg = room.agent_says_raw(sender, body) if sender else room.human_says(body)
    room.pump({"cutover-gate": agent})
    return msg


# ------------------------------------------------------------------------------------ preconditions

def test_benchmark_refused_without_matrix(room, stack):
    agent = make_agent(room, stack)
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    posts = gate_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and MATRIX_DOC in posts[0]
    assert not any(MARK["benchmark_card"] in p for p in room.bodies())
    assert room.doc_by_name(SCORECARD_DOC) is None
    assert room.pending("translator") == 0


def test_benchmark_refused_for_unapproved_wrong_rev_or_forged_matrix(room, stack):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"], approved=False)
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    assert "not 100% approved parity" in gate_posts(room)[-1]

    publish_matrix(room, "abcdef123456", approved=True)  # approved, but for another build
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    assert "not `" + stack["rev"] + "`" in gate_posts(room)[-1]

    # approved on a rev the modern service is not serving
    say_and_pump(room, agent, f"@cutover-gate benchmark rev=abcdef123456 migration={MIG}", sender="fuzzer")
    assert gate_posts(room)[-1].startswith(MARK["error"]) and "serves rev" in gate_posts(room)[-1]

    # approved, right rev, other migration
    publish_matrix(room, stack["rev"], approved=True, migration="MIG-19990101-000000")
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    assert "MIG-19990101-000000" in gate_posts(room)[-1]
    assert not any(MARK["benchmark_card"] in p for p in room.bodies())


def test_matrix_forged_by_another_agent_is_not_trusted(room, stack):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"], approved=True, author="translator")
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}")
    assert "not created by the fuzzer" in gate_posts(room)[-1]
    assert room.doc_by_name(SCORECARD_DOC) is None


def test_benchmark_only_from_fuzzer_or_human_and_needs_rev(room, stack):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="translator")
    assert "fuzzer agent or the human" in gate_posts(room)[-1]
    say_and_pump(room, agent, "@cutover-gate benchmark please")
    assert "usage" in gate_posts(room)[-1]
    assert room.doc_by_name(SCORECARD_DOC) is None


def test_approve_before_any_benchmark_is_explained(room, stack):
    agent = make_agent(room, stack)
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert gate_posts(room)[-1].startswith(MARK["error"]) and "no benchmark" in gate_posts(room)[-1]
    assert proxy_target(stack) == "legacy"


# ------------------------------------------------------------------------------------ the full gate

def test_full_gate_benchmark_refusal_approval_rehydration_rollback(room, stack):
    rev = stack["rev"]
    agent = make_agent(room, stack)
    publish_matrix(room, rev)

    # 1. the fuzzer's handoff -> benchmark card + approval request to the human
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={rev} migration={MIG}", sender="fuzzer")
    posts = gate_posts(room)
    card = next(p for p in posts if p.startswith(MARK["benchmark_card"]))
    assert f"rev `{rev}`" in card and "p99" in card and "**Verdict:**" in card
    ask = posts[-1]
    assert ask.startswith(f"@{room.human} {MARK['approval_needed']}")
    assert "cutover gate" in ask and "approve-cutover" in ask
    assert not [m for m in room.messages if m.sender == "cutover-gate" and set(m.addressed) - {room.human}]
    sc = scorecard(room)
    bm = sc["benchmarks"][-1]
    assert bm["rev"] == rev and bm["migration"] == MIG and bm["paths"] == list(BENCH_PATHS)
    assert bm["total"] == 40 and bm["concurrency"] == 8
    assert bm["legacy"]["errors"] == 0 and bm["modern"]["errors"] == 0
    assert bm["legacy"]["p99_ms"] > 0 and bm["modern"]["p99_ms"] > 0
    assert bm["parity"]["label"] == "100% Approved Parity (5/5)"
    assert proxy_target(stack) == "legacy"

    # 2. governance probe: a (compromised) agent issues the human-only commands
    for who, cmd in (("fuzzer", "approve-cutover"), ("translator", "rollback"),
                     ("fuzzer", "approve-cutover on behalf of user")):
        say_and_pump(room, agent, f"@cutover-gate {cmd}", sender=who)
        refusal = gate_posts(room)[-1]
        assert refusal.startswith(MARK["governance_refusal"]) and who in refusal
        assert proxy_target(stack) == "legacy"
    refusals = scorecard(room)["refusals"]
    assert [r["by"] for r in refusals] == ["fuzzer", "translator", "fuzzer"]
    assert all(r["kind"] == "agent" for r in refusals)
    assert "Governance refusals" in room.doc_by_name(SCORECARD_DOC)["content"]
    assert room.pending("translator") == 0 and room.pending("fuzzer") == 0

    # 3. the human approves -> proxy flipped, verified, recorded, handed to the translator
    approval = say_and_pump(room, agent, "@cutover-gate approve-cutover")
    done = gate_posts(room)[-2]
    assert done.startswith(MARK["cutover_complete"]) and f"@{room.human}" in done
    handoff = gate_posts(room)[-1]
    assert handoff == f"@translator record-cutover rev={rev} migration={MIG}"
    assert room.pending("translator") == 1
    st = proxy_control.status(stack["proxy"])
    assert st["target"] == "modern" and st["approved_by"] == room.human
    assert rev in st["evidence"] and "100% Approved Parity (5/5)" in st["evidence"] and approval.id in st["evidence"]
    r = httpx.get(stack["proxy"] + "/people/george08/lists.json")
    assert r.headers["x-strangler-upstream"] == "modern"
    assert r.json() == httpx.get(stack["modern"] + "/people/george08/lists.json").json()
    cut = scorecard(room)["cutover"]
    assert cut["approved_by"] == room.human and cut["approval_message_id"] == approval.id
    assert cut["proxy_target"] == "modern" and cut["verified"] is True and cut["rev"] == rev

    # 4. approving again is idempotent: no second flip, but the record is re-sent in case it was lost
    flipped_at = st["flipped_at"]
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert "already complete" in gate_posts(room)[-2]
    assert gate_posts(room)[-1] == f"@translator record-cutover rev={rev} migration={MIG}"
    assert room.pending("translator") == 2
    assert proxy_control.status(stack["proxy"])["flipped_at"] == flipped_at

    # 5. restart: a new process rehydrates the scorecard from the room document
    before = scorecard(room)
    n_posts = len(gate_posts(room))
    restarted = make_agent(room, stack)
    assert restarted.scorecard == before
    assert len(gate_posts(room)) == n_posts  # proxy matches the record: nothing to announce
    say_and_pump(room, restarted, "@cutover-gate status")
    status = gate_posts(room)[-1]
    assert "modern" in status and rev in status and "governance refusals recorded: 3" in status

    # 6. the human rolls back
    say_and_pump(room, restarted, "@cutover-gate rollback")
    assert gate_posts(room)[-1].startswith(MARK["rollback_complete"])
    assert proxy_target(stack) == "legacy"
    r = httpx.get(stack["proxy"] + "/people/george08/lists.json")
    assert r.headers["x-strangler-upstream"] == "legacy"
    sc = scorecard(room)
    assert sc["cutover"] is None and sc["rollbacks"][-1]["by"] == room.human and sc["rollbacks"][-1]["verified"]
    assert sc["cutover_history"][-1]["rolled_back_by"] == room.human
    say_and_pump(room, restarted, "@cutover-gate rollback")
    assert "nothing to roll back" in gate_posts(room)[-1]
    assert room.pending("translator") == 2  # rollback hands nothing to anyone

    # 7. and survives another restart
    assert make_agent(room, stack).scorecard == scorecard(room)


def test_restart_announces_router_drift(room, stack):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert proxy_target(stack) == "modern"
    proxy_control.flip(stack["proxy"], TOKEN, "legacy", "someone", "out-of-band")  # behind the gate's back
    make_agent(room, stack)
    drift = gate_posts(room)[-1]
    assert drift.startswith(MARK["error"]) and "outside the gate" in drift


def test_failed_verification_reverts_to_legacy_without_handoff(room, stack, monkeypatch):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    monkeypatch.setattr(proxy_control, "verify_routing", lambda *a, **k: (False, "body mismatch (simulated)"))
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    last = gate_posts(room)[-1]
    assert last.startswith(MARK["error"]) and "NOT completed" in last and "simulated" in last
    assert proxy_target(stack) == "legacy"
    assert room.pending("translator") == 0
    sc = scorecard(room)
    assert sc["cutover"] is None and "automatic revert" in sc["rollbacks"][-1]["note"]


# ------------------------------------------------------------------------------------ hygiene / misc

def test_mention_hygiene_is_enforced(room, stack):
    agent = make_agent(room, stack)
    with pytest.raises(RuntimeError, match="mention hygiene"):
        agent.say("please ask @translator to do it")
    with pytest.raises(RuntimeError, match="mention hygiene"):
        agent.ask_human("reply to @cutover-gate")
    agent.say(f"hello @{room.human}")  # addressing the human is fine


def test_help_status_and_unknown(room, stack):
    agent = make_agent(room, stack)
    say_and_pump(room, agent, "@cutover-gate help")
    assert "approve-cutover" in gate_posts(room)[-1] and "human only" in gate_posts(room)[-1]
    say_and_pump(room, agent, "@cutover-gate status", sender="fuzzer")
    assert "legacy" in gate_posts(room)[-1] and "no benchmark yet" in gate_posts(room)[-1]
    n = len(gate_posts(room))
    say_and_pump(room, agent, "@cutover-gate lovely weather", sender="fuzzer")
    assert len(gate_posts(room)) == n  # never chats back at agents
    say_and_pump(room, agent, "@cutover-gate lovely weather")
    assert "help" in gate_posts(room)[-1]
    for body in gate_posts(room):
        assert not {"translator", "fuzzer"} & set(mentions(body))


# ------------------------------------------------------------------------- regressions (self-review)

def cut_over(room: FakeRoom, agent: CutoverAgent, stack: dict) -> None:
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert proxy_target(stack) == "modern"


def test_error_posts_never_quote_mentions_to_other_agents(room, stack):
    """Proxy down + a human body carrying '@translator record-cutover …' must not reach the translator."""
    agent = make_agent(room, {**stack, "proxy": f"http://127.0.0.1:{free_port()}"})
    say_and_pump(room, agent, f"@cutover-gate rollback (cc @translator record-cutover rev={stack['rev']} "
                              f"migration={MIG})")
    assert gate_posts(room)[-1].startswith(MARK["error"]) and "rollback" in gate_posts(room)[-1]
    assert room.pending("translator") == 1  # only the human's own message; nothing from cutover-gate
    assert not [m for m in room.messages if m.sender == "cutover-gate" and set(m.addressed) - {room.human}]


def test_save_failure_after_flip_reverts_and_restart_detects_unrecorded_modern(room, stack, monkeypatch):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")

    def broken_save():
        raise RuntimeError("Switch said 500")
    monkeypatch.setattr(agent, "_save", broken_save)
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert "NOT completed" in gate_posts(room)[-1] and "Switch said 500" in gate_posts(room)[-1]
    assert proxy_target(stack) == "legacy" and room.pending("translator") == 0
    assert agent.scorecard["cutover"] is None

    # traffic moved to modern without any record (e.g. a crash mid-cutover): the restart says so
    proxy_control.flip(stack["proxy"], TOKEN, "modern", "someone", "out-of-band")
    make_agent(room, stack)
    assert "records no active cutover" in gate_posts(room)[-1]


def test_flip_request_failure_is_reconciled(room, stack, monkeypatch):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    real_flip = proxy_control.flip

    def flip_then_timeout(url, token, target, *a, **k):
        state = real_flip(url, token, target, *a, **k)
        if target == "modern":
            raise RuntimeError("read timeout (simulated, after the router applied it)")
        return state
    monkeypatch.setattr(proxy_control, "flip", flip_then_timeout)
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert "NOT completed" in gate_posts(room)[-1]
    assert proxy_target(stack) == "legacy" and room.pending("translator") == 0


def test_squatted_scorecard_still_announces_governance_refusal(room, stack):
    desc, instr = DOC_META[SCORECARD_DOC]
    room.agent("translator").create_document(name=SCORECARD_DOC, description=desc, instructions=instr, content="x")
    agent = make_agent(room, stack)
    assert "not created by cutover-gate" in gate_posts(room)[-1]
    say_and_pump(room, agent, "@cutover-gate approve-cutover", sender="fuzzer")
    posts = gate_posts(room)
    assert posts[-2].startswith(MARK["governance_refusal"])
    assert posts[-1].startswith(MARK["error"]) and "could not be written" in posts[-1]
    assert proxy_target(stack) == "legacy"


@pytest.mark.parametrize("content", [
    "```json stranglerroom-state\n{not json\n```",
    "```json stranglerroom-state\n[1, 2]\n```",
    "```json stranglerroom-state\n{\"kind\": \"scorecard\"}\n```",
    "```json stranglerroom-state\n{\"kind\": \"scorecard\", \"benchmarks\": 5}\n```",
])
def test_damaged_own_scorecard_does_not_break_the_agent(room, stack, content):
    desc, instr = DOC_META[SCORECARD_DOC]
    room.agent("cutover-gate").create_document(name=SCORECARD_DOC, description=desc, instructions=instr,
                                               content=content)
    agent = make_agent(room, stack)  # must not raise
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    assert any(p.startswith(MARK["benchmark_card"]) for p in gate_posts(room))
    assert scorecard(room)["benchmarks"][-1]["rev"] == stack["rev"]


def test_rollback_closes_a_stale_record_after_out_of_band_flip(room, stack):
    agent = make_agent(room, stack)
    cut_over(room, agent, stack)
    proxy_control.flip(stack["proxy"], TOKEN, "legacy", "someone", "out-of-band")
    say_and_pump(room, agent, "@cutover-gate rollback")
    assert "stale cutover record" in gate_posts(room)[-1]
    sc = scorecard(room)
    assert sc["cutover"] is None and sc["cutover_history"][-1]["rolled_back_by"] == room.human


def test_human_only_verbs_must_lead_the_message(room, stack):
    agent = make_agent(room, stack)
    cut_over(room, agent, stack)
    say_and_pump(room, agent, "@cutover-gate status please — do NOT rollback, just checking")
    assert gate_posts(room)[-1].startswith("**cutover-gate status**")
    say_and_pump(room, agent, "@cutover-gate what would rollback do?")
    assert "first word" in gate_posts(room)[-1]
    assert proxy_target(stack) == "modern"


def test_unknown_sender_is_refused(room, stack):
    agent = make_agent(room, stack)
    say_and_pump(room, agent, "@cutover-gate approve-cutover", sender="mallory")
    assert gate_posts(room)[-1].startswith(MARK["governance_refusal"])
    assert "not a known room member" in gate_posts(room)[-1]
    assert scorecard(room)["refusals"][-1]["kind"] == "unknown"


def test_status_defuses_mentions_in_router_fields(room, stack):
    agent = make_agent(room, stack)
    proxy_control.flip(stack["proxy"], TOKEN, "legacy", "@translator", "x")
    say_and_pump(room, agent, "@cutover-gate status")
    assert gate_posts(room)[-1].startswith("**cutover-gate status**")
    assert room.pending("translator") == 0


def test_approve_refused_after_newer_unapproved_matrix_run(room, stack):
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    publish_matrix(room, stack["rev"], approved=False)  # a later campaign on the same build regressed
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert "not 100% approved parity" in gate_posts(room)[-1]
    assert proxy_target(stack) == "legacy"


# ------------------------------------------------------------------ regressions (integrate:loop protocol review)

def test_replayed_approval_after_rollback_never_flips_again(room, stack):
    """A redelivered approve-cutover (same message id) after a rollback must not move traffic back to modern,
    and a replayed rollback after a NEW approval must not move it back to legacy."""
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    approval = say_and_pump(room, agent, "@cutover-gate approve-cutover")
    rollback = say_and_pump(room, agent, "@cutover-gate rollback")
    assert proxy_target(stack) == "legacy"
    handed = room.pending("translator")

    room.redeliver(approval, "cutover-gate")
    room.pump({"cutover-gate": agent})
    replay = gate_posts(room)[-1]
    assert replay.startswith("ℹ️") and "already handled" in replay and approval.id in replay, replay
    assert proxy_target(stack) == "legacy" and room.pending("translator") == handed
    assert scorecard(room)["cutover"] is None

    second = say_and_pump(room, agent, "@cutover-gate approve-cutover")  # a fresh human approval works
    assert gate_posts(room)[-2].startswith(MARK["cutover_complete"]) and proxy_target(stack) == "modern"
    assert scorecard(room)["cutover"]["approval_message_id"] == second.id
    room.redeliver(rollback, "cutover-gate")
    room.pump({"cutover-gate": agent})
    assert gate_posts(room)[-1].startswith("ℹ️") and "already handled" in gate_posts(room)[-1]
    assert proxy_target(stack) == "modern" and scorecard(room)["cutover"]["approval_message_id"] == second.id


def test_failed_revert_records_unknown_routing_until_a_human_settles_it(room, stack, monkeypatch):
    """Flip succeeded, verification failed AND the automatic revert failed: the proxy may serve modern, so the
    scorecard must keep a blocking ``unknown`` cutover (not None) — the translator reads it — until a rollback."""
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    monkeypatch.setattr(proxy_control, "verify_routing", lambda *a, **k: (False, "body mismatch (simulated)"))
    real_flip = proxy_control.flip

    def flip(url, token, target, *a, **k):
        if target == "legacy":
            raise RuntimeError("router unreachable (simulated)")
        return real_flip(url, token, target, *a, **k)
    monkeypatch.setattr(proxy_control, "flip", flip)
    approval = say_and_pump(room, agent, "@cutover-gate approve-cutover")
    last = gate_posts(room)[-1]
    assert last.startswith(MARK["error"]) and "NOT completed" in last and "revert" in last
    assert proxy_target(stack) == "modern" and room.pending("translator") == 0
    cut = scorecard(room)["cutover"]
    assert cut["proxy_target"] == "unknown" and cut["verified"] is False and cut["rev"] == stack["rev"]
    assert cut["approval_message_id"] == approval.id
    say_and_pump(room, agent, "@cutover-gate status")
    assert "routing `unknown` after a failed cutover" in gate_posts(room)[-1]

    monkeypatch.undo()  # router reachable again, verification real again
    say_and_pump(room, agent, "@cutover-gate rollback")
    assert gate_posts(room)[-1].startswith(MARK["rollback_complete"]) and proxy_target(stack) == "legacy"
    assert scorecard(room)["cutover"] is None


def test_approval_sent_before_the_benchmark_finished_never_flips(room, stack, monkeypatch):
    """critic: an approve-cutover queued while the gate was still benchmarking (sent before the card existed)
    is not an approval of those numbers — refused loudly, proxy untouched; a later approval works."""
    import time as _time

    from agents import cutover_agent

    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    # the benchmark "finishes" 30 s from now, so the approval queued behind it predates the card
    future = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() + 30))
    monkeypatch.setattr(cutover_agent, "now_iso", lambda: future)
    room.agent_says_raw("fuzzer", f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}")
    early = room.human_says("@cutover-gate approve-cutover")
    room.pump({"cutover-gate": agent})
    posts = gate_posts(room)
    assert posts[-3].startswith(MARK["benchmark_card"]) and MARK["approval_needed"] in posts[-2], posts[-3:]
    assert posts[-1].startswith(MARK["error"]) and "before the benchmark" in posts[-1], posts[-1]
    assert proxy_target(stack) == "legacy" and scorecard(room)["cutover"] is None
    assert room.pending("translator") == 0

    monkeypatch.undo()
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    late = say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert gate_posts(room)[-2].startswith(MARK["cutover_complete"]) and proxy_target(stack) == "modern"
    assert scorecard(room)["cutover"]["approval_message_id"] == late.id != early.id


def test_a_human_other_than_the_lead_cannot_approve_or_roll_back(room, stack):
    """critic: anyone can join a bridged Mattermost channel; only ``RoomInfo.human`` may move traffic."""
    agent = make_agent(room, stack)
    real = agent.switch.participants
    agent.switch.participants = lambda: [*real(), {"id": "ext-2", "name": "alice", "type": "user"}]
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    say_and_pump(room, agent, "@cutover-gate approve-cutover", sender="alice")
    refusal = gate_posts(room)[-1]
    assert refusal.startswith(MARK["governance_refusal"]) and "not the room's lead" in refusal, refusal
    assert proxy_target(stack) == "legacy" and scorecard(room)["refusals"][-1]["kind"] == "guest"


def test_replayed_noop_rollback_never_undoes_a_later_cutover(room, stack):
    """codex critic review #3: a rollback posted while the proxy was already on legacy is a no-op and is not in the
    rollback history; redelivered after a later approval it must not flip traffic back (it predates the flip)."""
    agent = make_agent(room, stack)
    early_rollback = say_and_pump(room, agent, "@cutover-gate rollback")
    assert "nothing to roll back" in gate_posts(room)[-1]
    publish_matrix(room, stack["rev"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert proxy_target(stack) == "modern"
    room.redeliver(early_rollback, "cutover-gate")
    room.pump({"cutover-gate": agent})
    replay = gate_posts(room)[-1]
    assert replay.startswith("ℹ️") and "before the latest routing change" in replay, replay
    assert proxy_target(stack) == "modern" and scorecard(room)["cutover"] is not None
    say_and_pump(room, agent, "@cutover-gate rollback")      # a fresh rollback still works
    assert gate_posts(room)[-1].startswith(MARK["rollback_complete"]) and proxy_target(stack) == "legacy"


def test_redelivered_benchmark_request_is_not_rerun(room, stack):
    """codex critic-hardening #8: re-running a redelivered benchmark would move the approval floor past a
    legitimate approval of the first card."""
    agent = make_agent(room, stack)
    publish_matrix(room, stack["rev"])
    request = say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}",
                           sender="fuzzer")
    first = scorecard(room)["benchmarks"]
    room.redeliver(request, "cutover-gate")
    room.pump({"cutover-gate": agent})
    assert gate_posts(room)[-1].startswith("ℹ️") and "already handled" in gate_posts(room)[-1]
    assert scorecard(room)["benchmarks"] == first and isinstance(first[-1]["finished_at_ms"], int)
    say_and_pump(room, agent, "@cutover-gate approve-cutover")
    assert proxy_target(stack) == "modern"


def test_no_benchmark_while_production_is_cut_over(room, stack):
    """Claude reviewer (lead delta): a newer benchmark would supersede the approved evidence and block the seal."""
    agent = make_agent(room, stack)
    cut_over(room, agent, stack)
    n_bench = len(scorecard(room)["benchmarks"])
    say_and_pump(room, agent, f"@cutover-gate benchmark rev={stack['rev']} migration={MIG}", sender="fuzzer")
    assert "production is cut over" in gate_posts(room)[-1] and "Roll back first" in gate_posts(room)[-1]
    assert len(scorecard(room)["benchmarks"]) == n_bench and proxy_target(stack) == "modern"


def test_cutover_records_the_approval_timestamp(room, stack):
    """The translator verifies the approval with ONE history read of the window ending at this timestamp."""
    agent = make_agent(room, stack)
    cut_over(room, agent, stack)
    ts = scorecard(room)["cutover"]["approval_timestamp_ms"]
    approval = next(m for m in reversed(room.messages) if m.sender == room.human and "approve-cutover" in m.body)
    assert isinstance(ts, int) and ts == int(approval.at * 1000)
