"""State layout of docs/ISOLATION.md section 2: every runtime file lives in its component's own directory.

* default paths: ``<STRANGLER_STATE_DIR or <repo>/.state>/{fuzzer,proxy,translator,legacy}/…``, resolved at call time,
  with every explicit override still winning;
* the published ``lists.py`` is 0644 whatever the umask (the modern server runs as another user);
* ``AgentCredentials.save`` never puts the API key in a file with group/other bits and never leaves a temp file;
* ``Settings.load_agent`` needs only ``SWITCH_URL`` and survives an unreadable ``switch.env``;
* a ``RoomAgent`` with no settings uses ``Settings.load_agent``.

Every file written here is under ``tmp_path``: never the real ``.state``, the real ``app/lists.py`` or a port.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents.switch_client as sc
from agents import divergence
from agents.fuzzer_agent import FuzzerAgent
from agents.provision import RoomInfo
from agents.runtime import RoomAgent
from agents.switch_client import AgentCredentials, Settings
from agents.translator_agent import DEFAULT_MODERN_LISTS, TranslatorAgent, default_pending_refusals_path
from targets.legacy_openlibrary import server as legacy_server
from targets.proxy import router
from tests.fakes import FakeRoom

ROOT = Path(__file__).resolve().parents[1]
REAL_STATE = ROOT / ".state"
SETTINGS = Settings(*(["unused"] * 8))
SECRET_VARS = ("SWITCH_URL", "AGENT_REGISTRATION_TOKEN", "GATEWAY_ADMIN_EMAIL", "GATEWAY_ADMIN_PASSWORD",
               "MATTERMOST_URL", "MATTERMOST_TEAM_NAME", "MATTERMOST_USER", "MATTERMOST_USER_PASSWORD")


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    """A private STRANGLER_STATE_DIR (also the switch client's import-time STATE_DIR)."""
    path = tmp_path / "state"
    monkeypatch.setenv("STRANGLER_STATE_DIR", str(path))
    monkeypatch.setattr(sc, "STATE_DIR", path)
    return path


def _info(room: FakeRoom) -> RoomInfo:
    return RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id=None, channel_url=None,
                    agents=dict(room.agent_ids), human=room.human)


# ------------------------------------------------------------------------------------------ default paths

def test_defaults_without_state_dir_are_the_component_directories(monkeypatch):
    monkeypatch.delenv("STRANGLER_STATE_DIR", raising=False)
    assert divergence.default_registry_path() == REAL_STATE / "fuzzer" / "divergence_registry.json"
    assert router.default_state_file({}) == REAL_STATE / "proxy" / "router.json"
    assert default_pending_refusals_path() == REAL_STATE / "translator" / "pending_refusals.json"
    assert legacy_server.default_log_file() == REAL_STATE / "legacy" / "legacy_openlibrary.log"


def test_defaults_follow_state_dir_at_call_time(state):
    assert divergence.default_registry_path() == state / "fuzzer" / "divergence_registry.json"
    assert divergence.Registry().path == state / "fuzzer" / "divergence_registry.json"
    assert router.default_state_file() == state / "proxy" / "router.json"
    assert default_pending_refusals_path() == state / "translator" / "pending_refusals.json"
    assert legacy_server.default_log_file() == state / "legacy" / "legacy_openlibrary.log"


def test_registry_default_is_created_with_its_directory(state):
    reg = divergence.Registry()
    assert reg.id_for("json_suffix", 404) == "D-1"
    path = state / "fuzzer" / "divergence_registry.json"
    assert json.loads(path.read_text())["json_suffix"]["id"] == "D-1"
    assert divergence.Registry(state / "elsewhere.json").path == state / "elsewhere.json"   # explicit wins


def test_registry_cli_default_follows_state_dir(state, monkeypatch):
    seen = {}

    async def fake_campaign(legacy, modern, decisions, *, n_random, seed, registry):
        seen["path"] = registry.path
        return SimpleNamespace(to_markdown=lambda: "", to_dict=lambda: {})

    monkeypatch.setattr(divergence, "run_campaign", fake_campaign)
    divergence.main(["--legacy", "http://127.0.0.1:1", "--modern", "http://127.0.0.1:1"])
    assert seen["path"] == state / "fuzzer" / "divergence_registry.json"
    divergence.main(["--legacy", "http://127.0.0.1:1", "--modern", "http://127.0.0.1:1",
                     "--registry", str(state / "cli.json")])
    assert seen["path"] == state / "cli.json"


def test_fuzzer_agent_default_registry(state):
    room = FakeRoom()
    agent = FuzzerAgent(settings=SETTINGS, room=_info(room), switch=room.agent("fuzzer"))
    assert agent.registry.path == state / "fuzzer" / "divergence_registry.json"
    explicit = FuzzerAgent(settings=SETTINGS, room=_info(room), switch=room.agent("fuzzer"),
                           registry_path=state / "x.json")
    assert explicit.registry.path == state / "x.json"


def test_router_state_file_default_and_override(state):
    base = {"STRANGLER_STATE_DIR": str(state), "ROUTER_ADMIN_TOKEN": "test-only"}
    assert router.RouterConfig.from_env(base).state_file == state / "proxy" / "router.json"
    other = state / "other" / "r.json"
    assert router.RouterConfig.from_env({**base, "ROUTER_STATE_FILE": str(other)}).state_file == other
    store = router.StateStore(router.RouterConfig.from_env(base).state_file)
    assert store.load().target == "legacy"
    store.save(router.RouterState(target="modern"))
    assert json.loads((state / "proxy" / "router.json").read_text())["target"] == "modern"


def test_translator_pending_refusals_default_and_overrides(state, tmp_path, monkeypatch):
    monkeypatch.delenv("STRANGLER_TRANSLATOR_PENDING", raising=False)
    room = FakeRoom()
    lists = tmp_path / "app" / "lists.py"
    assert lists.resolve() != DEFAULT_MODERN_LISTS.resolve()

    def make(**kw) -> TranslatorAgent:
        return TranslatorAgent(settings=SETTINGS, room=_info(room), switch=room.agent("translator"),
                               modern_lists_path=lists, modern_url="http://127.0.0.1:1", **kw)

    agent = make()
    assert agent.pending_refusals_path == state / "translator" / "pending_refusals.json"
    agent.pending_refusals = [{"by": "fuzzer"}]
    agent._write_pending()
    assert json.loads((state / "translator" / "pending_refusals.json").read_text()) == [{"by": "fuzzer"}]

    monkeypatch.setenv("STRANGLER_TRANSLATOR_PENDING", str(tmp_path / "env.json"))
    assert make().pending_refusals_path == tmp_path / "env.json"
    assert make(pending_refusals_path=tmp_path / "arg.json").pending_refusals_path == tmp_path / "arg.json"


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_legacy_log_default_creates_its_directory(state, monkeypatch, restore_root_logging):
    class Stop(Exception):
        pass

    seen = []

    def fake_configure(log_file):
        seen.append(log_file)
        raise Stop

    with monkeypatch.context() as m:
        m.setattr(legacy_server, "configure_logging", fake_configure)
        with pytest.raises(Stop):
            legacy_server.main(["--port", "1"])
        with pytest.raises(Stop):
            legacy_server.main(["--port", "1", "--log-file", str(state / "explicit.log")])
    log_file = state / "legacy" / "legacy_openlibrary.log"
    assert seen == [str(log_file), str(state / "explicit.log")]

    legacy_server.configure_logging(str(legacy_server.default_log_file()))
    logging.getLogger("legacy_openlibrary").error("probe line")
    assert "probe line" in log_file.read_text()


# ------------------------------------------------------------------------------- published module mode

def test_published_module_is_world_readable_under_umask_077(tmp_path):
    target = tmp_path / "app" / "lists.py"
    fake = SimpleNamespace(modern_lists_path=target)
    old = os.umask(0o077)
    try:
        TranslatorAgent._write_atomic(fake, "router = None\n")
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        target.chmod(0o600)  # a replacement (e.g. a restore) is published 0644 too
        TranslatorAgent._write_atomic(fake, b"router = 1\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644 and target.read_bytes() == b"router = 1\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["lists.py"]


# ------------------------------------------------------------------------------------- credential files

def _private(mode: int) -> bool:
    return stat.S_IMODE(mode) & 0o077 == 0


@pytest.mark.parametrize("umask", [0o000, 0o022])
def test_credentials_never_readable_by_others(state, monkeypatch, umask):
    key = "sk-secret-key-value"
    path = AgentCredentials.path_for("translator")
    checks: list[str] = []
    real_replace, real_fsync = os.replace, os.fsync

    def spy_fsync(fd):
        assert _private(os.fstat(fd).st_mode), "key written to a file others can read"
        checks.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst, *a, **kw):
        if Path(dst) == path:
            src_path = Path(src)
            assert src_path.parent == path.parent and src_path != path.with_suffix(".tmp")
            assert key in src_path.read_text() and _private(src_path.stat().st_mode)
            checks.append("replace")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    old = os.umask(umask)
    try:
        AgentCredentials(name="translator", agent_id="a1", api_key=key).save()
    finally:
        os.umask(old)
    assert checks == ["fsync", "replace"]
    assert _private(path.stat().st_mode)
    assert AgentCredentials.load("translator") == AgentCredentials("translator", "a1", key)
    assert sorted(p.name for p in path.parent.iterdir()) == ["translator.json"]


@pytest.mark.parametrize("fail", ["fsync", "replace"])
def test_credentials_save_failure_leaves_no_temp_file(state, monkeypatch, fail):
    previous = AgentCredentials(name="fuzzer", agent_id="a0", api_key="old-key")
    previous.save()

    def boom(*a, **kw):
        raise OSError("simulated failure")

    with monkeypatch.context() as m:
        m.setattr(os, fail, boom)
        with pytest.raises(OSError, match="simulated"):
            AgentCredentials(name="fuzzer", agent_id="a1", api_key="new-key").save()
    agents_dir = state / "agents"
    assert sorted(p.name for p in agents_dir.iterdir()) == ["fuzzer.json"]
    assert AgentCredentials.load("fuzzer") == previous


# ------------------------------------------------------------------------------------ agent settings

@pytest.fixture
def no_switch_env(monkeypatch):
    for var in SECRET_VARS:
        monkeypatch.delenv(var, raising=False)


def _secretless(settings: Settings) -> bool:
    return all(getattr(settings, f) == "" for f in ("registration_token", "admin_email", "admin_password",
                                                     "mattermost_url", "mattermost_team", "mattermost_user",
                                                     "mattermost_user_password"))


def test_load_agent_needs_no_secrets(state, no_switch_env):
    with pytest.raises(RuntimeError, match="missing required setting"):
        Settings.load()  # the full loader still insists on them
    settings = Settings.load_agent()
    assert settings.switch_url == sc.DEFAULT_SWITCH_URL and _secretless(settings)


def test_load_agent_reads_only_switch_url_from_the_file(state, no_switch_env, monkeypatch):
    state.mkdir(parents=True)
    (state / "switch.env").write_text("SWITCH_URL=http://switch.example:18000/\nAGENT_REGISTRATION_TOKEN=reg\n"
                                      "GATEWAY_ADMIN_PASSWORD=admin\nMATTERMOST_USER_PASSWORD=human\n")
    settings = Settings.load_agent()
    assert settings.switch_url == "http://switch.example:18000" and _secretless(settings)
    monkeypatch.setenv("SWITCH_URL", "http://env.example:1")
    assert Settings.load_agent().switch_url == "http://env.example:1"


def test_load_agent_tolerates_an_unreadable_switch_env(state, no_switch_env, monkeypatch):
    state.mkdir(parents=True)
    env_file = state / "switch.env"
    env_file.write_text("SWITCH_URL=http://switch.example:18000\n")
    real_read_text = Path.read_text

    def read_text(self, *a, **kw):
        if self == env_file:   # root bypasses file modes: simulate what an isolated agent gets
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    settings = Settings.load_agent()
    assert settings.switch_url == sc.DEFAULT_SWITCH_URL and _secretless(settings)
    monkeypatch.setenv("SWITCH_URL", "http://env.example:1")   # the launcher passes it in the environment
    assert Settings.load_agent().switch_url == "http://env.example:1"


def test_load_agent_propagates_other_errors(state, no_switch_env, monkeypatch):
    def broken(path):
        raise OSError(5, "Input/output error", str(path))

    monkeypatch.setattr(sc, "read_env_file", broken)
    with pytest.raises(OSError, match="Input/output"):
        Settings.load_agent()


def test_room_agent_default_settings_come_from_load_agent(state, no_switch_env, monkeypatch):
    def forbidden(cls):
        raise AssertionError("a room agent must not load the full settings")

    monkeypatch.setattr(Settings, "load", classmethod(forbidden))
    monkeypatch.setenv("SWITCH_URL", "http://env.example:1")

    class Probe(RoomAgent):
        name = "translator"

        def handle(self, event, sender):  # pragma: no cover - never dispatched
            pass

    room = FakeRoom()
    agent = Probe(room=_info(room), switch=room.agent("translator"))
    assert agent.settings.switch_url == "http://env.example:1" and _secretless(agent.settings)


def test_importing_the_proxy_touches_no_state(tmp_path):
    """Building the router creates its state directory (and maybe the token): that must not happen on import."""
    state = tmp_path / "state"
    env = {k: v for k, v in os.environ.items() if k not in ("ROUTER_ADMIN_TOKEN", "ROUTER_STATE_FILE",
                                                            "ROUTER_TOKEN_FILE")}
    env.update(STRANGLER_STATE_DIR=str(state), PYTHONPATH=str(ROOT))
    subprocess.run([sys.executable, "-c", "import targets.proxy.router"], env=env, cwd=ROOT, check=True, timeout=60)
    assert not state.exists()
    subprocess.run([sys.executable, "-c", "import targets.proxy.router as r; assert r.app is r.get_app()"],
                   env=env, cwd=ROOT, check=True, timeout=60)
    assert (state / "proxy").is_dir() and (state / "router.token").is_file()
