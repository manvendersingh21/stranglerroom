"""Framework tests for agents/runtime.py: sender classification and loud, mention-safe failure reports."""
from agents.commands import defuse_mentions, mentions
from agents.provision import RoomInfo
from agents.runtime import RoomAgent
from agents.switch_client import Settings
from tests.fakes import FakeRoom

SETTINGS = Settings(switch_url="http://x", registration_token="t", admin_email="a", admin_password="p",
                    mattermost_url="m", mattermost_team="switch", mattermost_user="user", mattermost_user_password="pw")


def _info(room: FakeRoom) -> RoomInfo:
    return RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id="c", channel_url=None,
                    agents=room.agent_ids, human=room.human)


class Boom(RoomAgent):
    name = "fuzzer"

    def handle(self, event, sender):
        raise RuntimeError("bad input from @cutover-gate")


class Recorder(RoomAgent):
    name = "translator"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = []

    def handle(self, event, sender):
        self.seen.append((sender.name, sender.kind))


def test_defuse_mentions_is_inert_but_readable():
    text = defuse_mentions("ping @cutover-gate and @user")
    assert mentions(text) == [] and text.replace("⁠", "") == "ping @cutover-gate and @user"


def test_failure_report_never_addresses_anyone():
    room = FakeRoom()
    boom = Boom(settings=SETTINGS, room=_info(room), switch=room.agent("fuzzer"))
    room.human_says("@fuzzer shadow-test please, cc @cutover-gate")   # the human addresses both
    assert room.pending("cutover-gate") == 1
    room.pump({"fuzzer": boom}, max_events=1)
    report = room.messages[-1]
    assert report.sender == "fuzzer" and report.body.startswith("⚠️")
    assert "cutover-gate" in report.body.replace("\u2060", "")          # quoted for the human to read...
    assert report.addressed == [] and room.pending("cutover-gate") == 1  # ...but nobody new is addressed


def test_classification_uses_participant_type_not_sender_kind():
    room = FakeRoom()
    rec = Recorder(settings=SETTINGS, room=_info(room), switch=room.agent("translator"))
    room.human_says("@translator status")
    room.agent_says_raw("fuzzer", "@translator status")
    room.pump({"translator": rec})
    assert rec.seen == [("user", "human"), ("fuzzer", "agent")]


def test_ignores_events_for_other_rooms_and_own_messages():
    room = FakeRoom()
    rec = Recorder(settings=SETTINGS, room=_info(room), switch=room.agent("translator"))
    ev = room._event(room.human_says("@translator hi"))
    ev.data["room_id"] = "another-room"
    rec.dispatch(ev)
    assert rec.seen == []


def test_register_agent_only_rotates_on_401(tmp_path, monkeypatch):
    """A transient error while probing stored creds must never trigger a key-rotating re-registration."""
    import httpx
    import pytest

    import agents.switch_client as sc

    monkeypatch.setattr(sc, "STATE_DIR", tmp_path)
    sc.AgentCredentials(name="translator", agent_id="a1", api_key="k1").save()
    calls = []

    def fake_get(url, **kw):
        calls.append(("GET", url))
        return httpx.Response(status, request=httpx.Request("GET", url), text="busy")

    def fake_post(url, **kw):
        calls.append(("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url), json={"id": "a1", "api_key": "k2"})

    monkeypatch.setattr(sc.httpx, "get", fake_get)
    monkeypatch.setattr(sc.httpx, "post", fake_post)
    for status in (429, 503, 409):
        with pytest.raises(sc.SwitchError):
            sc.register_agent(SETTINGS, "translator", "d", "T")
        assert not [c for c in calls if c[0] == "POST"], f"re-registered on HTTP {status}"
    status = 401
    creds = sc.register_agent(SETTINGS, "translator", "d", "T")
    assert creds.api_key == "k2" and [c for c in calls if c[0] == "POST"]


def test_only_the_room_lead_is_human_and_ambiguous_names_fail_closed():
    """critic: human-only gates key on ``Sender.is_human``. Another person in the bridged channel (Mattermost
    open server) is a ``guest``, never the lead; names that collide after case folding are ``unknown``."""
    room = FakeRoom()
    rec = Recorder(settings=SETTINGS, room=_info(room), switch=room.agent("translator"))
    real = rec.switch.participants
    rec.switch.participants = lambda: [*real(), {"id": "ext-2", "name": "alice", "type": "user"},
                                       {"id": "a-x", "name": "USER", "type": "agent"}]
    assert rec.classify("user").kind == "unknown"           # "user" (human) vs "USER" (agent): ambiguous
    assert rec.classify("alice").kind == "guest" and not rec.classify("alice").is_human
    rec.switch.participants = lambda: [*real(), {"id": "ext-2", "name": "alice", "type": "user"}]
    rec._participants = {}
    assert rec.classify("User").is_human and rec.classify("alice").kind == "guest"
    assert rec.classify("fuzzer").is_agent and rec.classify("nobody").kind == "unknown"


def test_participant_changes_are_never_served_from_a_stale_cache_and_lookup_failures_stay_loud():
    """codex critic-hardening #1/#7: a colliding participant that joins after ``user`` was classified makes the name
    ambiguous at once (same-type duplicates too); a failing participants lookup is reported and the agent survives."""
    room = FakeRoom()
    rec = Recorder(settings=SETTINGS, room=_info(room), switch=room.agent("translator"))
    real = rec.switch.participants
    assert rec.classify("user").is_human
    rec.switch.participants = lambda: [*real(), {"id": "a-x", "name": "USER", "type": "agent"}]
    assert rec.classify("user").kind == "unknown"
    rec.switch.participants = lambda: [*real(), {"id": "ext-9", "name": "User", "type": "user"}]
    assert rec.classify("user").kind == "unknown"

    def down():
        raise RuntimeError("list_participants: 503")

    rec.switch.participants = down
    room.human_says("@translator status")
    room.pump({"translator": rec})
    assert rec.seen == [] and room.messages[-1].sender == "translator"
    assert room.messages[-1].body.startswith("⚠️") and "503" in room.messages[-1].body
