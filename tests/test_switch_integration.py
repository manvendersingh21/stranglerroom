"""Live-stack integration test for :mod:`agents.switch_client` (docs/SWITCH_PROTOCOL.md).

The live test is ``@pytest.mark.integration`` and runs against a running Switch 0.31.0 (``SWITCH_URL``, default
``http://127.0.0.1:18000``) and is skipped with a clear reason when ``/health`` is down.

Everything uses THROWAWAY agents (``stranglerroom-itest-a-<rand>`` / ``-b-<rand>``) in a
throwaway ``internal_only`` room (no Mattermost channel is created, so the demo channel is
not spammed).  Credentials are written to a tmp state dir, never to ``.state/agents``; the
demo agents ``translator`` / ``fuzzer`` / ``cutover-gate`` and ``.state/room.json`` are
never touched.  Cleanup (archive the room, ``DELETE /agents/{id}`` for both agents) runs in
``finally`` blocks.

One end-to-end test (the steps depend on each other and share one room):

* ``register_agent`` for both agents; gateway login + ``set_addressing_policy(None)``;
* ``create_room`` via ``op(..., internal_only=True)``; ``connect`` both (scope=single);
* a -> b ``send_targeted`` arrives addressed with ``sender_name`` a; ``list_participants``;
* ``post``; create / update / load room documents; b updating a's document -> 403;
* gateway ``room_documents`` / ``room_document`` return the content;
* the 2 s heartbeat keeps the *same* stream alive past the 6 s TTL (> 8 s idle);
* after the stream response is forcibly closed the client reconnects and still
  delivers a subsequently addressed event.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

import agents.switch_client as sc
from agents.switch_client import Event, Settings, SwitchAgent, SwitchError, SwitchGateway, register_agent

from tests.procs import free_port

log = logging.getLogger("test_switch_integration")

REAL_STATE_DIR = sc.STATE_DIR
EVENT_TIMEOUT_S = 30.0


def _switch_url() -> str:
    env = sc.read_env_file(REAL_STATE_DIR / "switch.env")
    return (os.environ.get("SWITCH_URL") or env.get("SWITCH_URL") or "http://127.0.0.1:18000").rstrip("/")


def _require_switch() -> None:
    url = _switch_url()
    try:
        resp = httpx.get(url + "/health", timeout=3)
    except httpx.HTTPError as exc:
        pytest.skip(f"Switch not reachable at {url}/health ({type(exc).__name__}: {exc}); start the live stack")
    if resp.status_code != 200:
        pytest.skip(f"Switch at {url}/health answered HTTP {resp.status_code}; start the live stack")


def wait_for_event(agent: SwitchAgent, predicate: Callable[[Event], bool],
                   timeout: float = EVENT_TIMEOUT_S) -> Event:
    """Return the first event satisfying ``predicate``; fail (listing what arrived) on timeout."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(f"{agent.name}: no matching event within {timeout:.0f}s; got {seen}")
        event = agent.next_event(timeout=min(remaining, 1.0))
        if event is None:
            continue
        if predicate(event):
            return event
        seen.append(f"{event.name}:{event.body[:60]!r}")


def wait_until(condition: Callable[[], bool], what: str, timeout: float = EVENT_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(0.1)


def addressed_from(sender: str, token: str) -> Callable[[Event], bool]:
    return lambda e: e.is_message and e.addressed and e.sender_name == sender and token in e.body


def delete_agent(settings: Settings, creds: sc.AgentCredentials) -> None:
    """``DELETE /agents/{id}`` with the agent's own key; 404 (already gone) is fine."""
    resp = httpx.delete(f"{settings.switch_url}/agents/{creds.agent_id}",
                        headers={"Authorization": f"Bearer {creds.api_key}"}, timeout=15)
    if resp.status_code not in (200, 404):
        raise SwitchError("DELETE", str(resp.request.url), resp.status_code, resp.text)


@pytest.fixture
def settings() -> Settings:
    """Settings via :meth:`Settings.load` from the real ``.state/switch.env`` (read-only; env wins).

    Loaded *before* :func:`isolated_state` repoints ``STATE_DIR`` (``Settings.load`` reads
    ``STATE_DIR / "switch.env"``)."""
    _require_switch()
    assert sc.STATE_DIR == REAL_STATE_DIR
    try:
        return Settings.load()
    except RuntimeError as exc:
        pytest.skip(f"live-stack settings incomplete: {exc}")


@pytest.fixture
def isolated_state(settings, tmp_path, monkeypatch) -> Path:
    """Point the client's state dir at tmp so ``.state/agents`` is never written."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("STRANGLER_STATE_DIR", str(state))
    monkeypatch.setattr(sc, "STATE_DIR", state)
    return state


def test_failed_connect_raises_the_connection_error_and_closes_cleanly(tmp_path, monkeypatch):
    """Regression (no live stack): a stream that never opens must surface the *connection*
    error, and ``close()`` must not crash joining the never-started heartbeat thread."""
    monkeypatch.setattr(sc, "STATE_DIR", tmp_path)
    dead = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    settings = Settings(switch_url=dead, registration_token="x", admin_email="x", admin_password="x",
                        mattermost_url=dead, mattermost_team="x", mattermost_user="x", mattermost_user_password="x")
    agent = SwitchAgent(settings, sc.AgentCredentials(name="itest-dead", agent_id="00000000", api_key="k"))
    with pytest.raises(RuntimeError, match="did not open within"):
        agent.connect(["room-x"], timeout=1.5)
    assert not agent.is_live
    agent.close()  # idempotent second close must not raise either



def _fingerprint(directory: Path) -> dict[str, str]:
    """name -> sha256 of every file, so a rotated key is caught, not just a renamed file."""
    if not directory.is_dir():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob("*")) if p.is_file()}

@pytest.mark.integration
def test_switch_client_end_to_end(settings, isolated_state):
    real_agents_before = _fingerprint(REAL_STATE_DIR / "agents")
    rand = secrets.token_hex(4)
    name_a, name_b = f"stranglerroom-itest-a-{rand}", f"stranglerroom-itest-b-{rand}"

    registered: list[sc.AgentCredentials] = []
    clients: list[SwitchAgent] = []
    gateway: SwitchGateway | None = None
    room_id: str | None = None
    try:
        # ---- registration (credentials land in the tmp state dir only) ----------------------
        creds_a = register_agent(settings, name_a, "StranglerRoom integration test agent A", f"itest A {rand}")
        registered.append(creds_a)
        creds_b = register_agent(settings, name_b, "StranglerRoom integration test agent B", f"itest B {rand}")
        registered.append(creds_b)
        assert creds_a.agent_id != creds_b.agent_id
        assert (isolated_state / "agents" / f"{name_a}.json").is_file()
        assert (isolated_state / "agents" / f"{name_b}.json").is_file()
        assert (isolated_state / "agents" / f"{name_a}.json").stat().st_mode & 0o777 == 0o600
        # A second call reuses the stored, still-valid key instead of rotating it.
        assert register_agent(settings, name_a, "StranglerRoom integration test agent A", f"itest A {rand}") == creds_a

        # ---- gateway: open both addressing policies ------------------------------------------
        gateway = SwitchGateway(settings)
        for creds in (creds_a, creds_b):
            detail = gateway.set_addressing_policy(creds.agent_id, None)
            assert detail.get("addressing_policy") is None, detail
        listed = {a["id"]: a for a in gateway.agents()}
        assert listed[creds_a.agent_id].get("addressing_policy") is None
        assert listed[creds_b.agent_id].get("addressing_policy") is None

        agent_a, agent_b = SwitchAgent(settings, creds_a), SwitchAgent(settings, creds_b)
        clients += [agent_a, agent_b]

        # ---- room (internal_only: no Mattermost channel) ------------------------------------
        room = agent_a.op("create_room", {
            "name": f"stranglerroom-itest-{rand}",
            "description": "throwaway room for tests/test_switch_integration.py",
            "agent_names": [name_a, name_b], "user_names": [],
            "instructions": "Integration test room; archived when the test ends.",
            "internal_only": True,
        })
        room_id = room["id"]
        assert room_id and not room.get("failed_attachments"), room

        # ---- connect (scope=single, filter=addressed) ---------------------------------------
        agent_a.connect([room_id])
        agent_b.connect([room_id])
        assert agent_a.is_live and agent_b.is_live
        assert isinstance(agent_b._generation, int)

        participants = {p["name"]: p for p in agent_a.participants()}
        assert participants[name_a]["type"] == "agent", participants
        assert participants[name_b]["type"] == "agent", participants

        # ---- targeted message a -> b --------------------------------------------------------
        token = f"ping-{secrets.token_hex(3)}"
        sent = agent_a.send_targeted(f"hello {token}", [name_b])
        assert sent.get("event_id"), sent
        assert sent.get("target_statuses", {}).get(name_b) == "live", sent
        event = wait_for_event(agent_b, addressed_from(name_a, token))
        assert event.room_id == room_id
        assert event.body.startswith(f"@{name_b}"), event.body  # Switch prepends the mention itself
        assert event.sequence is not None

        # ---- plain broadcast (no mention) -----------------------------------------------------
        plain = f"broadcast {secrets.token_hex(3)} without mentions"
        assert agent_a.post(plain)
        ctx = agent_b.read_context(room_id, limit=20)
        bodies = [m["body"] for t in ctx["threads"] for m in [t["root"], *t["replies"]]]
        assert plain in bodies, bodies

        # ---- room documents --------------------------------------------------------------------
        doc_name = f"itest ledger {rand}"
        doc_id = agent_a.create_document(name=doc_name, description="v1", instructions="test doc",
                                         content="# v1\n")
        assert doc_id
        agent_a.update_document(doc_id, content="# v2\nupdated\n", description="v2")
        loaded = agent_a.load_documents([doc_id])
        assert [d["id"] for d in loaded] == [doc_id] and loaded[0]["content"] == "# v2\nupdated\n", loaded
        assert agent_a.upsert_document(name=doc_name, description="v3", instructions="test doc",
                                       content="# v3\n") == doc_id
        assert agent_b.load_documents([doc_id])[0]["content"] == "# v3\n"  # any member may read
        with pytest.raises(SwitchError) as excinfo:
            agent_b.update_document(doc_id, content="hijack")
        assert excinfo.value.status == 403, excinfo.value
        with pytest.raises(RuntimeError, match="created by"):
            agent_b.upsert_document(name=doc_name, description="x", instructions="x", content="hijack")
        assert agent_a.load_documents([doc_id])[0]["content"] == "# v3\n"

        docs = gateway.room_documents(room_id)
        assert any(d.get("id") == doc_id and d.get("name") == doc_name for d in docs), docs
        doc = gateway.room_document(room_id, doc_id)
        assert doc.get("content") == "# v3\n", doc

        # ---- heartbeat keeps the same stream alive past the 6 s TTL ---------------------------
        stream_before, generation_before = agent_b._stream_resp, agent_b._generation
        assert stream_before is not None
        time.sleep(8.5)
        assert agent_b.is_live and agent_a.is_live
        assert agent_b._stream_resp is stream_before, "stream was re-opened: the heartbeat did not keep it alive"
        assert agent_b._generation == generation_before
        token = f"alive-{secrets.token_hex(3)}"
        sent = agent_a.send_targeted(f"still there {token}", [name_b])
        assert sent["target_statuses"][name_b] == "live", sent
        wait_for_event(agent_b, addressed_from(name_a, token))
        assert agent_b._stream_resp is stream_before

        # ---- forced stream drop -> reconnect -> still delivered --------------------------------
        # Note: closing the httpx response from another thread does not interrupt the reader's
        # blocking socket read; the reader notices at the next frame (keepalive after <= 15 s
        # idle), then reconnects after its 1 s backoff -- hence EVENT_TIMEOUT_S = 30.
        stream_before.close()
        wait_until(lambda: agent_b.is_live and agent_b._stream_resp is not None
                   and agent_b._stream_resp is not stream_before, "agent b to reconnect its stream")
        assert agent_b._fatal is None
        token = f"after-reconnect-{secrets.token_hex(3)}"
        sent = agent_a.send_targeted(f"back again {token}", [name_b])
        assert sent["target_statuses"][name_b] == "live", sent
        wait_for_event(agent_b, addressed_from(name_a, token))
        assert all(t.is_alive() for t in agent_b._threads), "reader/heartbeat thread died"
    finally:
        errors: list[str] = []
        if room_id is not None and clients:
            try:
                clients[0].archive_room(room_id)
            except Exception as exc:  # noqa: BLE001 - keep cleaning up, report at the end
                errors.append(f"archive_room {room_id}: {exc}")
        for client in clients:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 - never skip the remote cleanup below
                errors.append(f"close {client.name}: {exc}")
        if gateway is not None:
            try:
                gateway.close()
            except Exception as exc:  # noqa: BLE001 - never skip the agent deletion below
                errors.append(f"gateway close: {exc}")
        for creds in registered:
            try:
                delete_agent(settings, creds)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"DELETE /agents/{creds.agent_id} ({creds.name}): {exc}")
        if _fingerprint(REAL_STATE_DIR / "agents") != real_agents_before:
            errors.append(".state/agents was modified (a real agent's credentials changed)")
        if errors:
            if sys.exc_info()[0] is not None:  # the test already failed: report, don't mask it
                log.error("cleanup problems after a failure: %s", "; ".join(errors))
            else:
                pytest.fail("cleanup failed: " + "; ".join(errors))
