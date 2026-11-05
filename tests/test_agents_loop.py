"""The whole StranglerRoom loop in-process: three REAL agents in a FakeRoom, real targets (docs/AGENTS_SPEC.md §2–§3).

``FakeRoom`` (tests/fakes.py, faithful Switch addressing + creator-only documents) hosts the real
:class:`TranslatorAgent`, :class:`FuzzerAgent` and :class:`CutoverAgent`, each wired with
``switch=room.agent(name)`` and explicit URLs. The targets are real subprocesses on free ports:

* legacy web.py (``targets.legacy_openlibrary.server``, tmp log file);
* modern FastAPI with the reloader ON, serving the translator's output from a tmp ``MODERN_LISTS_PATH``
  (the repo's ``targets/modern_fastapi/app/lists.py`` is never touched);
* the strangler proxy (tmp ``ROUTER_STATE_FILE``, test-only ``ROUTER_ADMIN_TOKEN``).

The test scripts the human exactly like the e2e driver (§3) does against live Switch:

1. ``migrate`` → v1 deployed, shadow campaign, the fuzzer asks for decisions on exactly D-1..D-5;
2. ``preserve D-1 D-2 D-4 and deprecate D-3 D-5`` → v2, re-verify, ``100% Approved Parity (5/5)``,
   benchmark card + approval request;
3. governance probe: the fuzzer (a "compromised agent") posts ``@cutover-gate approve-cutover`` →
   ``🛑 Governance gate``, the proxy stays on legacy;
4. the human's ``approve-cutover`` → proxy on modern, ``record-cutover`` handoff, ledger sealed;

then reads the three room documents and checks they tell one consistent story, and checks the
room transcript for protocol discipline: the exact sequence of agent messages, every agent→agent
address is a deliberate handoff built by :mod:`agents.commands`, every agent message starts with a
``MARK`` prefix (or is a ``📄`` document notice / ``ℹ️`` help-status-no-op answer). The last phases restart
all three agents from the room documents (silently, with identical state) and re-send ``approve-cutover``:
nothing flips or seals twice; then the human rolls back and a replay of the original approval event leaves
the proxy on legacy.
"""
from __future__ import annotations

import contextlib
import dataclasses
import re

import httpx
import pytest

from agents import ledger as L
from agents import proxy_control
from agents.commands import (APPROVE_EXAMPLE, MARK, MIGRATE_EXAMPLE, benchmark, mentions, record_cutover, re_verify,
                             shadow_test)
from agents.cutover_agent import CutoverAgent
from agents.fuzzer_agent import FuzzerAgent
from agents.provision import RoomInfo
from agents.switch_client import Settings
from agents.translator_agent import DEFAULT_MODERN_LISTS, TranslatorAgent
from tests.fakes import FakeMessage, FakeRoom
from tests.procs import free_port, service

TOKEN = "test-agents-loop-router-token"
DECIDE = "@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5"
DEMO_DECISIONS = {"D-1": "preserve", "D-2": "preserve", "D-3": "deprecate", "D-4": "preserve", "D-5": "deprecate"}
DEMO_TAGS = {"D-1": "json_suffix", "D-2": "loose_query", "D-3": "missing_resource_200",
             "D-4": "non_positive_quantity", "D-5": "empty_body_500"}
DEMO_STATUSES = {"D-1": "preserved-ok", "D-2": "preserved-ok", "D-3": "deprecated-ok", "D-4": "preserved-ok",
                 "D-5": "deprecated-ok"}
IDS = list(DEMO_DECISIONS)
AGENTS = ("translator", "fuzzer", "cutover-gate")
#: The only agent each agent may address (its deliberate handoff); the human may always be addressed.
HANDOFF_TARGET = {"translator": "fuzzer", "fuzzer": "cutover-gate", "cutover-gate": "translator"}
N_RANDOM = 20
BENCH_TOTAL, BENCH_CONCURRENCY = 40, 8
REV_RE = re.compile(r"rev `([0-9a-f]{12,64})`")


# ------------------------------------------------------------------------------------------- fixtures

@pytest.fixture()
def stack(tmp_path):
    """Legacy + modern (reloader ON, empty tmp lists dir) + proxy, all on free ports."""
    lists_dir = tmp_path / "modern_lists"  # its own dir: the modern reloader watches it
    lists_dir.mkdir()
    lists_path = lists_dir / "lists.py"
    assert lists_path.resolve() != DEFAULT_MODERN_LISTS.resolve()
    with contextlib.ExitStack() as es:
        legacy = es.enter_context(service("targets.legacy_openlibrary.server", free_port(),
                                          args=["--log-file", str(tmp_path / "legacy.log")]))
        modern = es.enter_context(service("targets.modern_fastapi.server", free_port(),
                                          env={"MODERN_LISTS_PATH": str(lists_path), "MODERN_NO_RELOAD": "0"}))
        proxy = es.enter_context(service("targets.proxy.router", free_port(), health="/_router/status",
                                         env={"LEGACY_URL": legacy, "MODERN_URL": modern, "ROUTER_ADMIN_TOKEN": TOKEN,
                                              "ROUTER_STATE_FILE": str(tmp_path / "router_state.json")}))
        yield {"legacy": legacy, "modern": modern, "proxy": proxy, "lists_path": lists_path, "tmp": tmp_path}


def make_agents(room: FakeRoom, stack: dict) -> dict:
    """The three real agents wired to ``room``; on_connected() runs like after a real (re)start."""
    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id=None, channel_url=None,
                    agents=dict(room.agent_ids), human=room.human)
    settings = Settings(**{f.name: "unused-in-tests" for f in dataclasses.fields(Settings)})
    common = {"settings": settings, "room": info}
    tmp = stack["tmp"]
    agents = {
        "translator": TranslatorAgent(switch=room.agent("translator"), modern_lists_path=stack["lists_path"],
                                      modern_url=stack["modern"], reload_timeout=45,
                                      pending_refusals_path=tmp / "translator_pending_refusals.json", **common),
        "fuzzer": FuzzerAgent(switch=room.agent("fuzzer"), legacy_url=stack["legacy"], modern_url=stack["modern"],
                              registry_path=tmp / "divergence_registry.json", n_random=N_RANDOM, rev_timeout=45,
                              **common),
        "cutover-gate": CutoverAgent(switch=room.agent("cutover-gate"), legacy_url=stack["legacy"],
                                     modern_url=stack["modern"], proxy_url=stack["proxy"], router_token=TOKEN,
                                     bench_total=BENCH_TOTAL, bench_concurrency=BENCH_CONCURRENCY, **common),
    }
    for agent in agents.values():
        agent.on_connected()
    return agents


# -------------------------------------------------------------------------------------------- helpers

def step(room: FakeRoom, agents: dict, msg: FakeMessage) -> list[FakeMessage]:
    """Deliver everything ``msg`` set in motion; return the agents' new messages (minus 📄 notices)."""
    before = len(room.messages)
    room.pump(agents)
    new = room.messages[before:]
    return [m for m in new if m.sender in AGENTS and not m.body.startswith("📄")]


def transcript(msgs: list[FakeMessage]) -> str:
    return "\n---\n".join(f"[{m.sender}] {m.body[:400]}" for m in msgs)


def find(msgs: list[FakeMessage], sender: str, prefix: str) -> FakeMessage:
    hits = [m for m in msgs if m.sender == sender and m.body.startswith(prefix)]
    assert len(hits) == 1, f"expected exactly one {sender} message starting {prefix!r}:\n{transcript(msgs)}"
    return hits[0]


def doc_state(room: FakeRoom, name: str, writer: str) -> dict:
    doc = room.doc_by_name(name)
    assert doc is not None, f"no room document {name!r}"
    assert doc["created_by_agent_id"] == room.agent_ids[writer], f"{name!r} not written by {writer}"
    state = L.extract_state(doc["content"])
    assert isinstance(state, dict), f"{name!r} has no state block"
    return state


def proxy_target(stack: dict) -> str:
    return proxy_control.status(stack["proxy"])["target"]


def classify(msg: FakeMessage, human: str) -> str:
    """The protocol role of an agent message: a MARK key, ``handoff:<verb>``, ``notice`` (📄), ``info`` (ℹ️
    help/status/no-op answers) — anything else (including every ``⚠️``) fails the test."""
    body = msg.body
    for target in (*AGENTS, human):
        if body.startswith(f"@{target} "):
            body = body[len(target) + 2:]
            if target != human:
                return "handoff:" + body.split(" ", 1)[0]
            break
    if body.startswith("📄"):
        return "notice"
    if body.startswith("ℹ️"):
        return "info"
    for key, prefix in MARK.items():
        if key != "error" and body.startswith(prefix):
            return key
    raise AssertionError(f"{msg.sender} message starts with no MARK prefix: {msg.body[:300]}")


# ------------------------------------------------------------------------------------------- the loop

def test_full_strangler_loop(stack):
    room = FakeRoom()
    agents = make_agents(room, stack)
    human = room.human
    assert proxy_target(stack) == "legacy"
    assert [m.body for m in room.messages] == [], "agents must start silently on an empty room"

    # ---- 1. migrate → v1 → shadow campaign → decision request on exactly D-1..D-5 ------------------
    migrate = room.human_says(MIGRATE_EXAMPLE)
    out = step(room, agents, migrate)
    accepted = find(out, "translator", MARK["migration_accepted"])
    mig = re.match(r"🧭 Migration (MIG-[0-9A-Za-z-]+) accepted from @user", accepted.body).group(1)
    v1 = REV_RE.search(find(out, "translator", MARK["v1_deployed"]).body).group(1)
    find(out, "translator", f"@fuzzer {shadow_test(v1, mig)}")
    assert find(out, "fuzzer", MARK["campaign"]).body.startswith(f"{MARK['campaign']} #1 on rev `{v1}`")
    ask = find(out, "fuzzer", f"@{human} {MARK['decision_needed']}")
    asked = re.match(rf"@{human} decision needed on ((?:D-\d+ ?)+) \(rev `{v1}`", ask.body)
    assert asked and asked.group(1).split() == IDS, ask.body
    assert "`preserve D-1 D-2 D-4 and deprecate D-3 D-5`" in ask.body
    assert not any(m.body.startswith(MARK["error"]) for m in out), transcript(out)

    # ---- 2. the human decides → v2 → re-verify → 100% Approved Parity → benchmark card ------------
    decide = room.human_says(DECIDE)
    out = step(room, agents, decide)
    applied = find(out, "translator", MARK["decisions_applied"])
    assert applied.body.startswith(f"{MARK['decisions_applied']} from @{human}: preserve D-1 D-2 D-4 and deprecate D-3 D-5")
    v2 = REV_RE.search(applied.body.split("v2 deployed", 1)[1]).group(1)
    assert v2 != v1
    find(out, "translator", f"@fuzzer {re_verify(v2, mig, DEMO_DECISIONS)}")
    assert find(out, "fuzzer", MARK["campaign"]).body.startswith(f"{MARK['campaign']} #2 on rev `{v2}`")
    assert find(out, "fuzzer", MARK["approved_parity"]).body.startswith(
        f"{MARK['approved_parity']} (5/5) on rev `{v2}`")
    find(out, "fuzzer", f"@cutover-gate {benchmark(v2, mig)}")
    card = find(out, "cutover-gate", MARK["benchmark_card"])
    assert card.body.startswith(f"{MARK['benchmark_card']} — rev `{v2}` (migration {mig})")
    assert "p99" in card.body and "100% Approved Parity (5/5)" in card.body
    find(out, "cutover-gate", f"@{human} {MARK['approval_needed']}")
    assert not any(m.body.startswith(MARK["error"]) for m in out), transcript(out)
    assert proxy_target(stack) == "legacy"

    # ---- 3. governance probe: a compromised fuzzer tries to approve the cutover --------------------
    probe = room.agent_says_raw("fuzzer", APPROVE_EXAMPLE)
    out = step(room, agents, probe)
    refusal = find(out, "cutover-gate", MARK["governance_refusal"])
    assert "human-only" in refusal.body and "stays on `legacy`" in refusal.body
    assert [m for m in out if m is not refusal] == [], transcript(out)
    assert proxy_target(stack) == "legacy"
    assert doc_state(room, L.SCORECARD_DOC, "cutover-gate")["cutover"] is None

    # ---- 4. the human approves → flip → record-cutover → ledger sealed -----------------------------
    approve = room.human_says(APPROVE_EXAMPLE)
    out = step(room, agents, approve)
    done = find(out, "cutover-gate", MARK["cutover_complete"])
    assert f"approved by @{human} (message `{approve.id}`)" in done.body
    find(out, "cutover-gate", f"@translator {record_cutover(v2, mig)}")
    sealed_msg = find(out, "translator", f"@{human} {MARK['ledger_sealed']}")
    assert f"Migration {mig} complete" in sealed_msg.body
    assert "preserve D-1 D-2 D-4 and deprecate D-3 D-5" in sealed_msg.body
    assert not any(m.body.startswith(MARK["error"]) for m in out), transcript(out)
    assert proxy_target(stack) == "modern"
    via_proxy = httpx.get(stack["proxy"] + "/people/george08/lists.json", timeout=10)
    assert via_proxy.headers.get("X-Strangler-Upstream") == "modern"
    assert via_proxy.json() == httpx.get(stack["modern"] + "/people/george08/lists.json", timeout=10).json()

    # ---- 5. the three documents tell one consistent story ------------------------------------------
    ledger = doc_state(room, L.LEDGER_DOC, "translator")
    matrix = doc_state(room, L.MATRIX_DOC, "fuzzer")
    scorecard = doc_state(room, L.SCORECARD_DOC, "cutover-gate")
    check_ledger(ledger, mig=mig, v1=v1, v2=v2, human=human, migrate=migrate, decide=decide, approve=approve)
    check_matrix(matrix, ledger, mig=mig, v1=v1, v2=v2)
    check_scorecard(scorecard, ledger, matrix, mig=mig, v2=v2, human=human, approve=approve, probe=probe)
    assert sealed_msg.body.endswith(f"head `sha256:{ledger['sealed']['head_hash']}`. Full record in the "
                                    f"“{L.LEDGER_DOC}”.")

    # ---- 6. the transcript: exact protocol, deliberate addressing only, MARK prefixes ---------------
    check_transcript(room, probe, LOOP_ROLES)

    # ---- 7. restart every agent from the room documents; a repeated approval changes nothing -------
    before_docs = {d["name"]: (d["versions"], d["content"]) for d in room.documents.values()}
    mark = len(room.messages)
    restarted = make_agents(room, stack)
    assert room.messages[mark:] == [], f"a clean restart is silent:\n{transcript(room.messages[mark:])}"
    assert restarted["translator"].ledger == ledger
    assert restarted["fuzzer"].matrix == matrix and restarted["fuzzer"].migration == mig
    assert restarted["cutover-gate"].scorecard == scorecard
    again = room.human_says(APPROVE_EXAMPLE)
    out = step(room, restarted, again)
    assert not any(m.body.startswith((MARK["error"], MARK["governance_refusal"])) for m in out), transcript(out)
    assert proxy_target(stack) == "modern"
    find(out, "cutover-gate", f"@translator {record_cutover(v2, mig)}")
    assert any(m.sender == "translator" and "already sealed" in m.body for m in out), transcript(out)
    assert not any(m.body.startswith((MARK["cutover_complete"], MARK["ledger_sealed"]))
                   or m.body.startswith(f"@{human} {MARK['ledger_sealed']}") for m in out), transcript(out)
    after_docs = {d["name"]: (d["versions"], d["content"]) for d in room.documents.values()}
    assert after_docs == before_docs, "a repeated approve-cutover must not rewrite any room document"
    assert L.extract_state(room.doc_by_name(L.LEDGER_DOC)["content"]) == ledger

    # ---- 8. the human rolls back; a replay of the original approval never flips traffic again ------
    rollback = room.human_says("@cutover-gate rollback")
    out = step(room, restarted, rollback)
    assert find(out, "cutover-gate", MARK["rollback_complete"]).body.startswith(
        f"{MARK['rollback_complete']}: ")
    assert proxy_target(stack) == "legacy"
    room.redeliver(approve, "cutover-gate")  # e.g. a stream replay after a reconnect
    out = step(room, restarted, approve)
    assert len(out) == 1 and out[0].body.startswith("ℹ️") and "already handled" in out[0].body, transcript(out)
    assert proxy_target(stack) == "legacy"
    final = doc_state(room, L.SCORECARD_DOC, "cutover-gate")
    assert final["cutover"] is None and final["rollbacks"][-1]["message_id"] == rollback.id
    assert final["cutover_history"][-1]["rolled_back_by"] == human
    assert doc_state(room, L.LEDGER_DOC, "translator") == ledger  # the sealed record stays as it was

    check_transcript(room, probe, LOOP_ROLES + [
        ("cutover-gate", "info"), ("cutover-gate", "handoff:record-cutover"), ("translator", "info"),
        ("cutover-gate", "rollback_complete"), ("cutover-gate", "info"),
    ])


# ----------------------------------------------------------------------------------------- doc checks

def check_ledger(ledger: dict, *, mig: str, v1: str, v2: str, human: str, migrate: FakeMessage,
                 decide: FakeMessage, approve: FakeMessage) -> None:
    assert ledger["kind"] == "ledger" and ledger["status"] == "sealed"
    assert ledger["migration"]["id"] == mig
    assert ledger["migration"]["requested_by"] == human
    assert ledger["migration"]["request_message_id"] == migrate.id
    assert [b["rev"] for b in ledger["builds"]] == [v1, v2]
    assert ledger["builds"][0]["policy"] == {}
    assert ledger["builds"][1]["policy"] == {DEMO_TAGS[c]: d for c, d in DEMO_DECISIONS.items()}
    # 5 decisions, all by the human, from the decision message
    assert list(ledger["decisions"]) == IDS
    for cid, rec in ledger["decisions"].items():
        assert rec["decision"] == DEMO_DECISIONS[cid] and rec["tag"] == DEMO_TAGS[cid], (cid, rec)
        assert rec["by"] == human and rec["message_id"] == decide.id, (cid, rec)
    assert [d["id"] for d in ledger["divergences"]] == IDS
    assert ledger["refusals"] == []  # the probe targeted the cutover gate; it records its own refusals
    # parity
    parity = ledger["parity"]
    assert parity["approved"] is True and parity["label"] == "100% Approved Parity (5/5)" and parity["rev"] == v2
    assert {c["id"]: c["status"] for c in parity["clusters"]} == DEMO_STATUSES
    assert {c["id"]: c["decision"] for c in parity["clusters"]} == DEMO_DECISIONS
    # benchmark p99s
    bm = ledger["benchmark"]
    assert bm["rev"] == v2 and bm["migration"] == mig
    for side in ("legacy", "modern"):
        assert isinstance(bm[side]["p99_ms"], (int, float)) and bm[side]["p99_ms"] > 0, bm[side]
        assert bm[side]["total"] == BENCH_TOTAL and bm[side]["errors"] == 0, bm[side]
    # cutover sign-off
    cut = ledger["cutover"]
    assert cut["approved_by"] == human and cut["approval_message_id"] == approve.id
    assert cut["proxy_target"] == "modern" and cut["verified"] is True
    assert cut["approval_message_verified"] is True
    # hash chain + seal
    ok, why = L.verify_chain(ledger["trail"])
    assert ok, why
    assert ledger["sealed"]["head_hash"] == ledger["trail"][-1]["hash"]
    assert ledger["sealed"]["entries"] == len(ledger["trail"])
    events = [(e["event"], e["actor"], e["actor_kind"]) for e in ledger["trail"]]
    assert events == [
        ("migration-requested", human, "human"), ("routes-extracted", "translator", "agent"),
        ("build-deployed", "translator", "agent"),
        *[("decision-recorded", human, "human")] * 5,
        ("build-deployed", "translator", "agent"),
        ("parity-verified", "fuzzer", "agent"), ("benchmark-recorded", "cutover-gate", "agent"),
        ("cutover-approved", human, "human"), ("proxy-flipped", "cutover-gate", "agent"),
        ("ledger-sealed", "translator", "agent"),
    ], events
    by_event = {e["event"]: e for e in ledger["trail"]}
    assert by_event["migration-requested"]["source_message_id"] == migrate.id
    assert by_event["cutover-approved"]["source_message_id"] == approve.id


def check_matrix(matrix: dict, ledger: dict, *, mig: str, v1: str, v2: str) -> None:
    assert matrix["kind"] == "matrix" and matrix["migration"] == mig
    first, second = matrix["runs"]
    assert (first["run"], first["rev"], second["run"], second["rev"]) == (1, v1, 2, v2)
    assert first["trigger"]["by"] == "translator" and first["trigger"]["command"] == "shadow-test"
    assert second["trigger"]["by"] == "translator" and second["trigger"]["command"] == "re-verify"
    assert all(r["trigger"]["migration"] == mig for r in (first, second))
    assert first["decisions"] == {} and second["decisions"] == DEMO_DECISIONS
    r1, r2 = first["report"], second["report"]
    assert [c["id"] for c in r1["clusters"]] == IDS
    assert {c["id"]: c["tag"] for c in r1["clusters"]} == DEMO_TAGS
    assert all(c["status"] == "open" for c in r1["clusters"]) and r1["approved_parity"] is False
    assert {c["id"]: c["status"] for c in r2["clusters"]} == DEMO_STATUSES
    assert r2["approved_parity"] is True and r2["parity_label"] == "100% Approved Parity (5/5)"
    assert {tag: e["id"] for tag, e in matrix["registry"].items()} == {t: c for c, t in DEMO_TAGS.items()}
    # the ledger mirrors exactly this matrix
    assert ledger["parity"]["evidence"] == L.digest(matrix)
    assert (ledger["parity"]["run"], ledger["parity"]["total_requests"]) == (2, r2["total_requests"])
    assert [(d["id"], d["tag"]) for d in ledger["divergences"]] == [(c["id"], c["tag"]) for c in r1["clusters"]]
    decided_on = {e["evidence"]["cluster"]: e["evidence"] for e in ledger["trail"] if e["event"] == "decision-recorded"}
    assert all(ev["matrix_run"] == 1 and ev["matrix_rev"] == v1 for ev in decided_on.values()), decided_on


def check_scorecard(scorecard: dict, ledger: dict, matrix: dict, *, mig: str, v2: str, human: str,
                    approve: FakeMessage, probe: FakeMessage) -> None:
    assert scorecard["kind"] == "scorecard"
    assert len(scorecard["benchmarks"]) == 1
    bm = scorecard["benchmarks"][0]
    assert (bm["rev"], bm["migration"], bm["requested_by"]) == (v2, mig, "fuzzer")
    assert (bm["total"], bm["concurrency"]) == (BENCH_TOTAL, BENCH_CONCURRENCY)
    assert bm["parity"]["label"] == "100% Approved Parity (5/5)" and bm["parity"]["evidence"] == L.digest(matrix["runs"][-1])
    assert {k: v for k, v in ledger["benchmark"].items() if k != "evidence"} == bm
    assert ledger["benchmark"]["evidence"] == L.digest(scorecard)
    cut = scorecard["cutover"]
    assert (cut["rev"], cut["migration"], cut["proxy_target"]) == (v2, mig, "modern")
    assert cut["approved_by"] == human and cut["approved_by_kind"] == "human"
    assert cut["approval_message_id"] == approve.id and cut["verified"] is True
    for key in ("approved_by", "approved_at", "approval_message_id", "proxy_target", "flipped_at"):
        assert ledger["cutover"][key] == cut[key], key
    assert scorecard["rollbacks"] == []
    (refusal,) = scorecard["refusals"]
    assert (refusal["by"], refusal["kind"], refusal["command"], refusal["message_id"], refusal["proxy_target"]) == \
        ("fuzzer", "agent", "approve-cutover", probe.id, "legacy")


# ---------------------------------------------------------------------------------- transcript checks

#: The agent messages of one clean loop (📄 notices excluded), in order.
LOOP_ROLES = [
    ("translator", "migration_accepted"), ("translator", "v1_deployed"), ("translator", "handoff:shadow-test"),
    ("fuzzer", "campaign"), ("fuzzer", "decision_needed"),
    ("translator", "decisions_applied"), ("translator", "handoff:re-verify"),
    ("fuzzer", "campaign"), ("fuzzer", "approved_parity"), ("fuzzer", "handoff:benchmark"),
    ("cutover-gate", "benchmark_card"), ("cutover-gate", "approval_needed"),
    ("cutover-gate", "governance_refusal"),
    ("cutover-gate", "cutover_complete"), ("cutover-gate", "handoff:record-cutover"),
    ("translator", "ledger_sealed"),
]


def check_transcript(room: FakeRoom, probe: FakeMessage, expected: list[tuple[str, str]]) -> None:
    human = room.human
    agent_msgs = [m for m in room.messages if m.sender in AGENTS and m.id != probe.id]
    for m in agent_msgs:
        # Addressing: the human, or this agent's single deliberate handoff target at the very start.
        agents_addressed = [t for t in m.addressed if t in AGENTS]
        assert set(m.addressed) <= {human, HANDOFF_TARGET[m.sender]}, (m.addressed, m.body[:300])
        if agents_addressed:
            assert agents_addressed == [HANDOFF_TARGET[m.sender]] and m.body.startswith(f"@{agents_addressed[0]} "), \
                m.body[:300]
            assert mentions(m.body).count(agents_addressed[0]) == 1, f"handoff mentions its target twice: {m.body}"
        # No other participant appears as a live @mention anywhere in the body.
        assert set(mentions(m.body)) <= {human, HANDOFF_TARGET[m.sender]}, m.body[:300]
    roles = [(m.sender, classify(m, human)) for m in agent_msgs]
    talk = [r for r in roles if r[1] != "notice"]
    assert talk == expected, talk
    # Every 📄 notice is about the sender's own document.
    own = {"translator": L.LEDGER_DOC, "fuzzer": L.MATRIX_DOC, "cutover-gate": L.SCORECARD_DOC}
    for m in agent_msgs:
        if m.body.startswith("📄"):
            assert own[m.sender] in m.body, m.body
    # The human only ever addressed the agent its command is for; the probe only the cutover gate.
    for m in room.messages:
        if m.sender == human:
            assert m.addressed == mentions(m.body)[:1] and len(m.addressed) == 1, m.body
    assert probe.addressed == ["cutover-gate"]
    # Typing indicators are switched off again by every agent that used them.
    for name in AGENTS:
        states = [on for who, on in room.typing if who == name]
        assert states and states[-1] is False, (name, states)
