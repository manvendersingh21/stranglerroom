"""Per-component OS users ("isolation"): agents/isolation.py against docs/ISOLATION.md §3-§7 (revision 2).

Nothing here touches the host: no user or group is created, nothing outside temporary directories is chmod-ed or
chown-ed, the real .state, /srv and /etc are never written. Privilege drops happen in forked children, to ``nobody``
or to synthetic numeric uids/gids (47001…, which need no /etc/passwd entry). A "service area" and a fake checkout
live in a world-traversable temporary directory under /tmp, so the dropped children can reach the former but, as on
the real host, not the latter (it is 0700, like /root).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from agents import isolation as iso
from tests.procs import free_port
from tests.test_targets_sh import FAKE_SERVER_SOURCE

ROOT = Path(__file__).resolve().parents[1]
NOBODY = 65534
LOCK_GID = 47100
UIDS = {name: 47001 + i for i, name in enumerate(iso.COMPONENTS)}
BASE_PYTHON = os.path.realpath(getattr(sys, "_base_executable", sys.executable))

requires_root = pytest.mark.skipif(os.geteuid() != 0, reason="needs root (chown, setuid) like the launcher itself")


def ids_for(component: iso.Component | str) -> iso.Ids:
    comp = iso._component(component)
    uid, translator = UIDS[comp.name], UIDS["translator"]
    return iso.Ids(uid, uid, (LOCK_GID,) if comp.production_lock else (), LOCK_GID if comp.production_lock else None,
                   (translator, translator) if comp.uses_module else None)


# ----------------------------------------------------------------------------------------------------- fixtures

@pytest.fixture
def world_tmp():
    """A root-owned 0755 directory under /tmp: reachable by any uid (unlike pytest's 0700 tmp_path)."""
    path = Path(tempfile.mkdtemp(prefix="sr-iso-test-", dir="/tmp"))
    path.chmod(0o755)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


PROBE = r'''"""Fake component: reports what it sees (run as __main__); importing it does nothing."""
import json, os, sys, time


def main():
    def is_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    old = os.umask(0)
    os.umask(old)
    args = sys.argv[1:]
    reach = {}
    for i, a in enumerate(args):
        if a == "--probe-path":
            try:
                os.close(os.open(args[i + 1], os.O_RDONLY))
                reach[args[i + 1]] = True
            except OSError:
                reach[args[i + 1]] = False
    info = {
        "argv": sys.argv, "executable": sys.executable, "cwd": os.getcwd(), "resuid": os.getresuid(),
        "resgid": os.getresgid(), "groups": sorted(os.getgroups()), "umask": old, "pid": os.getpid(),
        "sid": os.getsid(0), "fds": [fd for fd in sorted(int(f) for f in os.listdir("/proc/self/fd")) if is_open(fd)],
        "env": dict(os.environ), "cmdline": open("/proc/self/cmdline", "rb").read().decode(errors="replace"),
        "status": open("/proc/self/status").read(), "reach": reach,
    }
    os.write(1, json.dumps(info).encode())
    if "--sleep" in args:
        os.close(1)
        time.sleep(60)


if __name__ == "__main__":
    main()
'''

AGENT_KEYS = {c.name: f"api-key-of-{c.name}-0123456789" for c in iso.COMPONENTS.values() if c.agent}
ROUTER_TOKEN = "router-token-abcdef0123456789"
SWITCH_SECRETS = {"GATEWAY_ADMIN_PASSWORD": "gw-admin-secret-123", "MATTERMOST_USER_PASSWORD": "mm-user-secret-456",
                  "AGENT_REGISTRATION_TOKEN": "reg-token-789xyz00"}


def make_checkout(base: Path) -> Path:
    """A fake operator checkout (0700, like /root): code trees, a .venv whose python is the system interpreter,
    and a .state with secrets."""
    repo = base / "checkout"
    for d in ("agents/__pycache__", "targets/proxy", "targets/legacy_openlibrary", "targets/modern_fastapi/app",
              "docs", ".state/agents", ".venv/bin", ".venv/lib/python3.12/site-packages"):
        (repo / d).mkdir(parents=True, exist_ok=True)
    for pkg in ("agents", "targets", "targets/proxy", "targets/legacy_openlibrary", "targets/modern_fastapi",
                "targets/modern_fastapi/app"):
        (repo / pkg / "__init__.py").write_text("")
    for comp in iso.COMPONENTS.values():
        (repo / (comp.module.replace(".", "/") + ".py")).write_text(PROBE)
    (repo / "targets/modern_fastapi/app/lists.py").write_text("GENERATED = True\n")
    (repo / "agents/__pycache__/runtime.cpython-312.pyc").write_bytes(b"\0" * 16)
    (repo / "agents/stray.pyc").write_bytes(b"\0" * 16)
    (repo / "docs/SPEC.md").write_text("# spec\n")
    (repo / ".venv/bin/python").symlink_to(BASE_PYTHON)
    (repo / ".venv/bin/python3").symlink_to("python")
    (repo / ".venv/pyvenv.cfg").write_text(f"home = {os.path.dirname(BASE_PYTHON)}\n"
                                           "include-system-site-packages = false\n")
    odd = repo / ".venv/lib/python3.12/site-packages/odd.txt"
    odd.write_text("world-writable in the checkout\n")
    odd.chmod(0o666)
    state = repo / ".state"
    (state / "switch.env").write_text("SWITCH_URL=http://from-switch-env:1\n"
                                      + "".join(f"{k}={v}\n" for k, v in SWITCH_SECRETS.items()))
    (state / "switch.env").chmod(0o600)
    (state / "connection.env").write_text("SWITCH_URL=http://10.0.0.5:18000\n")
    (state / "router.token").write_text(ROUTER_TOKEN + "\n")
    (state / "router.token").chmod(0o600)
    (state / "room.json").write_text('{"room_id": "r-1"}')
    for name, key in AGENT_KEYS.items():
        path = state / "agents" / f"{name}.json"
        path.write_text(json.dumps({"name": name, "agent_id": f"id-{name}", "api_key": key}))
        path.chmod(0o600)
    repo.chmod(0o700)
    return repo


@pytest.fixture
def checkout(world_tmp):
    return make_checkout(world_tmp)


@pytest.fixture
def area(world_tmp, checkout):
    """A synced and fully prepared service area for the synthetic ids."""
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    iso.sync_mirrors(checkout, paths)
    for comp in iso.COMPONENTS.values():
        iso.prepare(comp, ids_for(comp), paths, checkout / ".state")
    return paths


def write_marker(tmp_path: Path, *, repo: str | None = None, state: str = "ready",
                 service_home: str = "/srv/stranglerroom", mode: int = 0o644, raw: str | None = None) -> dict:
    marker_dir = tmp_path / "etc"
    marker_dir.mkdir(exist_ok=True)
    marker = marker_dir / "isolation.json"
    marker.write_text(raw if raw is not None else json.dumps(
        {"version": 2, "state": state, "repo": repo or str(ROOT), "service_home": service_home}))
    marker.chmod(mode)
    return {"STRANGLER_ISOLATION_MARKER": str(marker)}


def in_child(fn, *args):
    """Run fn(*args) in a forked child; return its JSON result (or {"error": ...})."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            try:
                payload = {"result": fn(*args)}
            except BaseException as exc:  # noqa: BLE001
                payload = {"error": f"{type(exc).__name__}: {exc}"}
            os.write(w, json.dumps(payload).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return json.loads(data or b'{"error": "child died"}')


def st(path: Path) -> tuple[int, int, int]:
    s = os.lstat(path)
    return stat.S_IMODE(s.st_mode), s.st_uid, s.st_gid


# ----------------------------------------------------------------------------------------------------- §3 mode

def test_off_ignores_even_a_ready_marker(tmp_path):
    env = {**write_marker(tmp_path), "STRANGLER_ISOLATION": "off"}
    assert iso.isolation_on(env) is False


def test_auto_follows_a_ready_marker_for_this_checkout_only(tmp_path):
    assert iso.isolation_on({"STRANGLER_ISOLATION_MARKER": str(tmp_path / "none.json")}) is False
    assert iso.isolation_on(write_marker(tmp_path)) is True
    assert iso.isolation_on({**write_marker(tmp_path), "STRANGLER_ISOLATION": "AUTO"}) is True
    assert iso.isolation_on(write_marker(tmp_path, repo="/some/other/checkout")) is False  # CI / another checkout


def test_on_without_a_matching_ready_marker_fails_loudly(tmp_path):
    with pytest.raises(iso.IsolationError, match="no marker"):
        iso.isolation_on({"STRANGLER_ISOLATION": "on", "STRANGLER_ISOLATION_MARKER": str(tmp_path / "none.json")})
    with pytest.raises(iso.IsolationError, match="belongs to /some/other"):
        iso.isolation_on({**write_marker(tmp_path, repo="/some/other"), "STRANGLER_ISOLATION": "on"})
    assert iso.isolation_on({**write_marker(tmp_path), "STRANGLER_ISOLATION": "on"}) is True


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_a_marker_that_is_not_ready_is_an_error(tmp_path, mode):
    env = {**write_marker(tmp_path, state="setting-up"), "STRANGLER_ISOLATION": mode}
    with pytest.raises(iso.IsolationError, match="isolation setup incomplete"):
        iso.isolation_on(env)


@pytest.mark.parametrize("raw, match", [
    ("{not json", "not valid JSON"),
    ('["a list"]', "no \"repo\""),
    ('{"repo": "/x", "state": "ready"}', "service_home"),
    ('{"repo": "/x", "state": "half", "service_home": "/srv/x"}', "unknown state"),
])
def test_a_malformed_marker_is_never_ignored(tmp_path, raw, match):
    env = write_marker(tmp_path, raw=raw)
    with pytest.raises(iso.IsolationError, match=match):
        iso.isolation_on(env)


def test_an_untrusted_marker_is_refused(tmp_path):
    env = write_marker(tmp_path, mode=0o666)
    with pytest.raises(iso.IsolationError, match="group/other-writable"):
        iso.isolation_on(env)
    marker = Path(env["STRANGLER_ISOLATION_MARKER"])
    marker.chmod(0o644)
    real = marker.with_name("real.json")
    marker.rename(real)
    marker.symlink_to(real)
    with pytest.raises(iso.IsolationError, match="symlinks are refused"):
        iso.isolation_on(env)


@requires_root
@pytest.mark.parametrize("mode", ["auto", "on"])
def test_a_marker_under_a_service_controlled_ancestor_is_an_error_never_off(tmp_path, mode):
    """codex isolation-impl #1: a service owning any ancestor could rename the marker's directory away and turn
    `auto` into off. The whole chain is checked, also when the marker is (now) missing."""
    control = tmp_path / "control"
    (control / "markers").mkdir(parents=True)
    marker = control / "markers" / "isolation.json"
    marker.write_text(json.dumps({"state": "ready", "repo": str(ROOT), "service_home": "/srv/x"}))
    os.chown(control, NOBODY, NOBODY)  # the compromised service's directory; markers/ stays root's
    env = {"STRANGLER_ISOLATION": mode, "STRANGLER_ISOLATION_MARKER": str(marker)}
    with pytest.raises(iso.IsolationError, match="owned by uid 65534"):
        iso.isolation_on(env)
    (control / "markers").rename(control / "moved")  # what the service would do
    with pytest.raises(iso.IsolationError, match="owned by uid 65534"):
        iso.isolation_on(env)
    with pytest.raises(iso.IsolationError, match="owned by uid 65534"):
        iso.write_marker({"state": "ready"}, env)
    assert not (control / "markers").exists()
    # a group/other-writable (non-sticky) ancestor is refused the same way
    os.chown(control, 0, 0)
    control.chmod(0o777)
    with pytest.raises(iso.IsolationError, match="group/other-writable"):
        iso.isolation_on(env)


def test_bad_mode_value_fails(tmp_path):
    with pytest.raises(iso.IsolationError, match="off, on or auto"):
        iso.isolation_on({"STRANGLER_ISOLATION": "yes"})


def _cli(*args, env=None, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith("STRANGLER_")}
    e.update(env or {})
    e["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-m", "agents.isolation", *args], cwd=ROOT, env=e, capture_output=True,
                          text=True, timeout=120, **kw)


def test_cli_mode_prints_on_or_off_and_exits_2_on_misconfiguration(tmp_path):
    r = _cli("mode", env={"STRANGLER_ISOLATION": "off"})
    assert (r.returncode, r.stdout) == (0, "off\n")
    r = _cli("mode", env={"STRANGLER_ISOLATION": "on", "STRANGLER_ISOLATION_MARKER": str(tmp_path / "x.json")})
    assert r.returncode == 2 and r.stdout == "" and "no marker" in r.stderr
    r = _cli("mode", env={**write_marker(tmp_path, state="setting-up")})
    assert r.returncode == 2 and r.stdout == "" and "isolation setup incomplete" in r.stderr
    r = _cli("mode", env={"STRANGLER_ISOLATION": "maybe"})
    assert r.returncode == 2 and "off, on or auto" in r.stderr


@requires_root
def test_cli_mode_prepares_a_trusted_run_dir_when_on(tmp_path):
    env = write_marker(tmp_path)
    run = tmp_path / "state" / "run"
    r = _cli("mode", "--run-dir", str(run), env=env)
    assert (r.returncode, r.stdout) == (0, "on\n"), r.stderr
    assert st(run) == (0o700, 0, 0)
    run.chmod(0o755)
    assert _cli("mode", "--run-dir", str(run), env=env).returncode == 0
    assert st(run)[0] == 0o700
    # off: the run dir is not touched
    other = tmp_path / "untouched"
    assert _cli("mode", "--run-dir", str(other), env={"STRANGLER_ISOLATION": "off"}).stdout == "off\n"
    assert not other.exists()


@requires_root
def test_run_dir_on_an_untrusted_path_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # a service could rename the run dir away and plant its own
    with pytest.raises(iso.IsolationError, match="group/other-writable"):
        iso.ensure_run_dir(shared / "run")
    shared.chmod(0o755)
    (shared / "real").mkdir()
    (shared / "run").symlink_to(shared / "real")
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.ensure_run_dir(shared / "run")
    owned = shared / "owned"
    owned.mkdir()
    os.chown(owned, NOBODY, NOBODY)
    with pytest.raises(iso.IsolationError, match="owned by uid 65534"):
        iso.ensure_run_dir(owned / "run")


@requires_root
def test_run_dir_creation_never_follows_an_unchecked_ancestor(tmp_path):
    """codex isolation-impl #2: missing components are created descriptor by descriptor, each checked before the
    next is created, so a service-owned ancestor holding `slot -> /etc` gets no root mkdir through it."""
    target = tmp_path / "etc-like"
    target.mkdir()
    sr_run = tmp_path / "sr-run"
    sr_run.mkdir()
    (sr_run / "slot").symlink_to(target)
    os.chown(sr_run, NOBODY, NOBODY)  # planted by a service after an earlier check would have seen nothing
    with pytest.raises(iso.IsolationError, match="owned by uid 65534"):
        iso.ensure_run_dir(sr_run / "slot" / "run")
    assert list(target.iterdir()) == []
    os.chown(sr_run, 0, 0)  # root's directory, but the next component is a symlink: refused, not followed
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.ensure_run_dir(sr_run / "slot" / "run")
    assert list(target.iterdir()) == []
    created = iso.ensure_run_dir(tmp_path / "fresh" / "a" / "run")
    assert created and st(tmp_path / "fresh" / "a" / "run") == (0o700, 0, 0) and st(tmp_path / "fresh")[0] == 0o700


@requires_root
def test_run_dir_holding_more_than_launcher_files_is_not_locked_down(tmp_path):
    logs = tmp_path / "var-log"
    logs.mkdir(mode=0o755)
    for name in ("syslog", "translator.log", ".lock"):
        (logs / name).write_text("x")
    with pytest.raises(iso.IsolationError, match="holds more than launcher files"):
        iso.ensure_run_dir(logs)
    assert st(logs)[0] == 0o755
    (logs / "syslog").unlink()
    iso.ensure_run_dir(logs)
    assert st(logs)[0] == 0o700


def test_mode_on_as_non_root_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STRANGLER_ISOLATION", "on")
    monkeypatch.setenv("STRANGLER_ISOLATION_MARKER", write_marker(tmp_path)["STRANGLER_ISOLATION_MARKER"])
    monkeypatch.setattr(iso.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(iso, "_require_trusted", lambda st, what: None)  # the marker is root's, we "are" uid 1000
    assert iso.main(["mode"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "not root" in captured.err


# ------------------------------------------------------------------------------------------ §4.4 environment

PARENT_ENV = {
    "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "TERM": "xterm",
    "PYTHONUNBUFFERED": "1", "STRANGLER_LOG_LEVEL": "DEBUG", "STRANGLER_AGENT_PACE": "0.5",
    "LEGACY_URL": "http://127.0.0.1:9001", "MODERN_URL": "http://127.0.0.1:9002", "PROXY_URL": "http://127.0.0.1:9000",
    "STRANGLER_TRANSLATOR_MODE": "llm", "STRANGLER_LLM_CMD": "claude -p", "MODERN_NO_RELOAD": "1",
    "STRANGLER_TRANSLATOR_PASS_ENV": "ANTHROPIC_API_KEY, OTHER_LLM_KEY",
    "ANTHROPIC_API_KEY": "sk-ant-secret", "OTHER_LLM_KEY": "other-secret", "GH_TOKEN": "gh-secret",
    "GATEWAY_ADMIN_PASSWORD": "gw-secret", "MATTERMOST_USER_PASSWORD": "mm-secret", "ROUTER_ADMIN_TOKEN": "root-token",
    "ROUTER_TOKEN_FILE": "/root/token", "ROUTER_STATE_FILE": "/root/router.json", "STRANGLER_STATE_DIR": "/root/.state",
    "MODERN_LISTS_PATH": "/root/lists.py", "PYTHONPATH": "/root/checkout", "HOME": "/root", "USER": "root",
    "LD_PRELOAD": "/root/evil.so", "STRANGLER_PRODUCTION_LOCK": "/root/lock", "SWITCH_URL": "http://env-switch:1",
}
COMMON = {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONUNBUFFERED", "STRANGLER_LOG_LEVEL"}
LAUNCHER = {"STRANGLER_STATE_DIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH", "HOME", "USER",
            "LOGNAME"}
EXPECTED_KEYS = {
    "translator": COMMON | LAUNCHER | {"STRANGLER_AGENT_PACE", "LEGACY_URL", "MODERN_URL", "PROXY_URL", "SWITCH_URL",
                                       "STRANGLER_TRANSLATOR_MODE", "STRANGLER_LLM_CMD", "ANTHROPIC_API_KEY",
                                       "OTHER_LLM_KEY", "MODERN_LISTS_PATH"},
    "fuzzer": COMMON | LAUNCHER | {"STRANGLER_AGENT_PACE", "LEGACY_URL", "MODERN_URL", "PROXY_URL", "SWITCH_URL"},
    "cutover-gate": COMMON | LAUNCHER | {"STRANGLER_AGENT_PACE", "LEGACY_URL", "MODERN_URL", "PROXY_URL",
                                         "SWITCH_URL", "ROUTER_ADMIN_TOKEN"},
    "proxy": COMMON | LAUNCHER | {"LEGACY_URL", "MODERN_URL", "ROUTER_ADMIN_TOKEN"},
    "legacy": COMMON | LAUNCHER,
    "modern": COMMON | LAUNCHER | {"MODERN_NO_RELOAD", "MODERN_LISTS_PATH"},
}


@pytest.mark.parametrize("name", list(iso.COMPONENTS))
def test_child_env_follows_the_component_schema(name):
    comp = iso.COMPONENTS[name]
    paths = iso.service_paths("/srv/sr")
    env = iso.child_env(comp, PARENT_ENV, paths=paths, router_token="  the-token\n", switch_url="http://10.1.2.3:18000")
    assert set(env) == EXPECTED_KEYS[name]
    assert env["HOME"] == f"/srv/sr/state/{name}" and env["USER"] == env["LOGNAME"] == comp.user
    assert env["STRANGLER_STATE_DIR"] == "/srv/sr/state" and env["PYTHONPATH"] == "/srv/sr/app"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert ("ROUTER_ADMIN_TOKEN" in env) == comp.router_token
    if comp.router_token:
        assert env["ROUTER_ADMIN_TOKEN"] == "the-token"  # resolved by the launcher, never root's environment value
    assert ("SWITCH_URL" in env) == comp.agent
    if comp.agent:
        assert env["SWITCH_URL"] == "http://10.1.2.3:18000"
    if comp.uses_module:
        assert env["MODERN_LISTS_PATH"] == "/srv/sr/state/module/lists.py"
    for secret in ("GATEWAY_ADMIN_PASSWORD", "MATTERMOST_USER_PASSWORD", "GH_TOKEN", "ROUTER_TOKEN_FILE",
                   "LD_PRELOAD", "ROUTER_STATE_FILE", "STRANGLER_PRODUCTION_LOCK", "TERM"):
        assert secret not in env
    if name != "translator":
        assert "ANTHROPIC_API_KEY" not in env and "STRANGLER_LLM_CMD" not in env
    assert "/root" not in json.dumps(env)


def test_child_env_requires_the_token_and_the_switch_url():
    paths = iso.service_paths("/srv/sr")
    with pytest.raises(iso.IsolationError, match="router token"):
        iso.child_env("proxy", PARENT_ENV, paths=paths)
    with pytest.raises(iso.IsolationError, match="SWITCH_URL"):
        iso.child_env("fuzzer", PARENT_ENV, paths=paths)
    # a token passed for a component without that authority is never added
    assert "ROUTER_ADMIN_TOKEN" not in iso.child_env("legacy", PARENT_ENV, paths=paths, router_token="t")


@pytest.mark.parametrize("name", ["ROUTER_ADMIN_TOKEN", "ROUTER_TOKEN_FILE", "GATEWAY_ADMIN_PASSWORD", "HOME",
                                  "PYTHONPATH", "STRANGLER_STATE_DIR", "MODERN_LISTS_PATH", "SWITCH_URL",
                                  "LD_PRELOAD", "PYTHONSTARTUP", "bad-name", "AGENT_REGISTRATION_TOKEN"])
def test_pass_env_can_never_set_a_launcher_set_or_forbidden_variable(name):
    env = {**PARENT_ENV, "STRANGLER_TRANSLATOR_PASS_ENV": f"ANTHROPIC_API_KEY,{name}"}
    with pytest.raises(iso.IsolationError, match="may not forward"):
        iso.child_env("translator", env, paths=iso.service_paths("/srv/sr"), switch_url="http://10.1.2.3:1")


def test_switch_url_from_env_then_connection_env_never_switch_env(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "switch.env").write_text("SWITCH_URL=http://secret-file:1\nGATEWAY_ADMIN_PASSWORD=x\n")
    env = {"STRANGLER_STATE_DIR": str(state)}
    assert iso.resolve_switch_url(env) == iso.DEFAULT_SWITCH_URL
    (state / "connection.env").write_text("SWITCH_URL=http://public:18000/\n")
    assert iso.resolve_switch_url(env) == "http://public:18000"
    assert iso.resolve_switch_url({**env, "SWITCH_URL": "http://env:1/"}) == "http://env:1"


def test_service_argv_rewrites_the_interpreter_and_refuses_anything_else():
    paths = iso.service_paths("/srv/sr")
    assert iso.service_argv("proxy", ["/root/c/.venv/bin/python", "-m", "targets.proxy.router", "--port", "9000"],
                            paths) == ["/srv/sr/venv/bin/python", "-m", "targets.proxy.router", "--port", "9000"]
    for argv in (["python", "-m", "agents.fuzzer_agent"], ["python", "-c", "import os"], ["python"],
                 ["sh", "-c", "python -m targets.proxy.router"], ["python", "-m", "tests.fake_agent_translator_agent"]):
        with pytest.raises(iso.IsolationError):
            iso.service_argv("proxy", argv, paths)


def test_component_table_matches_the_spec():
    assert {c.name: c.user for c in iso.COMPONENTS.values()} == {n: "sr-" + n for n in iso.COMPONENTS}
    assert {n for n, c in iso.COMPONENTS.items() if c.groups == (iso.LOCK_GROUP,)} == {"translator", "cutover-gate"}
    assert {n for n, c in iso.COMPONENTS.items() if c.groups} == {"translator", "cutover-gate"}
    assert {n for n, c in iso.COMPONENTS.items() if c.router_token} == {"proxy", "cutover-gate"}
    assert {n for n, c in iso.COMPONENTS.items() if c.agent} == {"translator", "fuzzer", "cutover-gate"}
    assert {n for n, c in iso.COMPONENTS.items() if c.module_writer} == {"translator"}
    for comp in iso.COMPONENTS.values():  # every module exists in the checkout
        assert (ROOT / (comp.module.replace(".", "/") + ".py")).is_file()


# ------------------------------------------------------------------------------------------------- §2.2 mirrors

@requires_root
def test_sync_mirrors_copies_code_and_venv_root_owned_and_only_what_changed(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    actions = iso.sync_mirrors(checkout, paths)
    assert actions
    assert (paths.app / "targets/proxy/router.py").read_text() == PROBE
    assert (paths.app / "docs/SPEC.md").is_file()
    assert not (paths.app / "agents/__pycache__").exists() and not (paths.app / "agents/stray.pyc").exists()
    assert not (paths.app / "targets/modern_fastapi/app/lists.py").exists()  # the checkout's generated module
    assert not (paths.app / "tests").exists()
    assert os.readlink(paths.python) == BASE_PYTHON and os.readlink(paths.venv / "bin/python3") == "python"
    assert st(paths.venv / "lib/python3.12/site-packages/odd.txt") == (0o644, 0, 0)  # no world-writable copy
    for tree in (paths.app, paths.venv, paths.home):
        assert st(tree) == (0o755, 0, 0)
    assert iso._not_root_only(paths.app) == [] and iso._not_root_only(paths.venv) == []
    assert iso.sync_mirrors(checkout, paths) == []  # nothing changed: nothing written
    # a changed file is replaced (temp file + rename), a removed one disappears, a planted one is removed
    (checkout / "agents/translator_agent.py").write_text("CHANGED = 1\n")
    (checkout / "docs/SPEC.md").unlink()
    (paths.app / "agents/planted.py").write_text("x")
    actions = iso.sync_mirrors(checkout, paths)
    assert (paths.app / "agents/translator_agent.py").read_text() == "CHANGED = 1\n"
    assert not (paths.app / "docs/SPEC.md").exists() and not (paths.app / "agents/planted.py").exists()
    assert any("removed stale" in a for a in actions)
    # a mirror file that is not root's is replaced even if size and mtime match
    victim = paths.app / "agents/fuzzer_agent.py"
    os.chown(victim, UIDS["fuzzer"], UIDS["fuzzer"])
    iso.sync_mirrors(checkout, paths)
    assert st(victim)[1] == 0


@requires_root
def test_sync_mirrors_refuses_a_symlinked_or_foreign_mirror_dir(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    elsewhere = world_tmp / "elsewhere"
    elsewhere.mkdir()
    paths.app.symlink_to(elsewhere)
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.sync_mirrors(checkout, paths)
    assert list(elsewhere.iterdir()) == []
    paths.app.unlink()
    paths.app.mkdir()
    os.chown(paths.app, NOBODY, NOBODY)
    with pytest.raises(iso.IsolationError, match="not root"):
        iso.sync_mirrors(checkout, paths)


@requires_root
@pytest.mark.parametrize("make_link", ["absolute-writable", "relative-escape"])
def test_sync_mirrors_refuses_a_symlink_a_service_could_steer(world_tmp, checkout, make_link):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    site = checkout / ".venv/lib/python3.12/site-packages"
    if make_link == "absolute-writable":
        planted = world_tmp / "drop"
        planted.mkdir()
        planted.chmod(0o777)  # any service could write the module it points at
        (site / "evil.py").symlink_to(planted / "evil.py")
    else:
        (site / "escape.py").symlink_to("../../../../outside.py")
    with pytest.raises(iso.IsolationError, match="refusing to mirror"):
        iso.sync_mirrors(checkout, paths)


@requires_root
def test_absolute_link_through_mutable_indirection_is_refused_by_sync_and_verify(world_tmp, checkout):
    """codex isolation-impl #3: `gate.py -> /x/linkbox/gate.py`, where the service owns linkbox and gate.py is a
    symlink to a genuine root module. realpath looks trustworthy, but the service can swap gate.py later."""
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    linkbox = world_tmp / "linkbox"
    linkbox.mkdir()
    (linkbox / "gate.py").symlink_to(BASE_PYTHON)  # currently resolves to a root-owned file
    os.chown(linkbox, NOBODY, NOBODY)
    site = checkout / ".venv/lib/python3.12/site-packages"
    (site / "gate.py").symlink_to(linkbox / "gate.py")
    with pytest.raises(iso.IsolationError, match="refusing to mirror"):
        iso.sync_mirrors(checkout, paths)
    # the same rule in verify: a mirror link whose chain a service could change is reported
    (site / "gate.py").unlink()
    iso.sync_mirrors(checkout, paths)
    assert iso._not_root_only(paths.venv) == []
    os.symlink(linkbox / "gate.py", paths.venv / "lib/python3.12/site-packages/gate.py")
    bad = iso._not_root_only(paths.venv)
    assert len(bad) == 1 and "gate.py" in bad[0] and "65534" in bad[0]
    # and the checks themselves: root-only chains pass, a link owned by a service or a missing entry in a
    # world-writable directory does not
    assert iso.check_link_target(BASE_PYTHON, Path("/")) == Path(BASE_PYTHON)
    assert iso.check_link_target("python", paths.venv / "bin") == Path(BASE_PYTHON)
    with pytest.raises(iso.IsolationError, match="missing in /tmp"):
        iso.check_link_target("/tmp/sr-iso-not-there/x.py", Path("/"))
    with pytest.raises(iso.IsolationError, match="more than"):
        (world_tmp / "loop").symlink_to(world_tmp / "loop")
        iso.check_link_target(str(world_tmp / "loop"), Path("/"))


@requires_root
def test_service_home_on_an_untrusted_path_is_refused(world_tmp, checkout):
    shared = world_tmp / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(iso.IsolationError, match="group/other-writable"):
        iso.ensure_service_home(iso.service_paths(shared / "srv"))
    closed = world_tmp / "closed"
    closed.mkdir()
    closed.chmod(0o700)
    with pytest.raises(iso.IsolationError, match="not searchable"):
        iso.ensure_service_home(iso.service_paths(closed / "srv"))


# ------------------------------------------------------------------------------------------------- §4.2 prepare

@requires_root
def test_prepare_lays_out_each_components_part(world_tmp, checkout, area):
    paths = area
    t = UIDS["translator"]
    assert st(paths.state) == (0o755, 0, 0) and st(paths.agents)[:2] == (0o711, 0)
    assert st(paths.room) == (0o644, 0, 0) and paths.room.read_bytes() == (checkout / ".state/room.json").read_bytes()
    assert st(paths.lock) == (0o660, 0, LOCK_GID)
    assert st(paths.module) == (0o755, t, t)
    for comp in iso.COMPONENTS.values():
        uid = UIDS[comp.name]
        assert st(paths.private(comp)) == (0o700, uid, uid)
        if comp.agent:
            assert st(paths.key(comp)) == (0o600, uid, uid)
            assert paths.key(comp).read_bytes() == (checkout / ".state/agents" / f"{comp.name}.json").read_bytes()
    assert sorted(os.listdir(paths.agents)) == sorted(f"{n}.json" for n in AGENT_KEYS)  # no temp files left
    for name in ("switch.env", "router.token", "connection.env"):
        assert not (paths.state / name).exists()


@requires_root
def test_modern_alone_gets_the_module_dir_owned_by_the_translator(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    actions = iso.prepare("modern", ids_for("modern"), paths, checkout / ".state")
    t = UIDS["translator"]
    assert st(paths.module) == (0o755, t, t) and any("module" in a for a in actions)
    assert not paths.lock.exists() and not paths.key("translator").exists() and not paths.room.exists()


@requires_root
def test_prepare_never_repairs_or_writes_inside_a_service_owned_dir(world_tmp, checkout, area):
    paths = area
    private = paths.private("fuzzer")
    secret = world_tmp / "root-secret"
    secret.write_text("root only")
    secret.chmod(0o600)
    (private / "link").symlink_to(secret)
    (private / "rootfile").write_text("planted by root")  # e.g. a leftover; root never re-owns it
    before = {p.name: (os.lstat(p).st_uid, os.lstat(p).st_mode) for p in private.iterdir()}
    iso.prepare("fuzzer", ids_for("fuzzer"), paths, checkout / ".state")
    assert {p.name: (os.lstat(p).st_uid, os.lstat(p).st_mode) for p in private.iterdir()} == before
    assert st(secret) == (0o600, 0, 0)
    # a private dir owned by someone else is refused, not repaired
    os.chown(private, UIDS["proxy"], UIDS["proxy"])
    with pytest.raises(iso.IsolationError, match="no ownership repair"):
        iso.prepare("fuzzer", ids_for("fuzzer"), paths, checkout / ".state")
    assert st(private)[1] == UIDS["proxy"]


@requires_root
def test_prepare_restores_the_mode_of_a_service_owned_dir(world_tmp, checkout, area):
    paths = area
    paths.module.chmod(0o777)  # a compromised translator opened the module dir to every service
    paths.private("proxy").chmod(0o755)
    iso.prepare("modern", ids_for("modern"), paths, checkout / ".state")
    iso.prepare("proxy", ids_for("proxy"), paths, checkout / ".state")
    assert st(paths.module)[0] == 0o755 and st(paths.private("proxy"))[0] == 0o700


@requires_root
def test_prepare_refuses_symlinks_where_it_writes(world_tmp, checkout, area):
    paths = area
    elsewhere = world_tmp / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(paths.private("legacy"))
    paths.private("legacy").symlink_to(elsewhere)
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.prepare("legacy", ids_for("legacy"), paths, checkout / ".state")
    assert st(elsewhere)[1] == 0
    shutil.rmtree(paths.agents)
    paths.agents.symlink_to(elsewhere)
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.prepare("fuzzer", ids_for("fuzzer"), paths, checkout / ".state")
    assert list(elsewhere.iterdir()) == []


@requires_root
def test_prepare_hands_over_only_an_empty_root_leftover(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    paths.private("proxy").mkdir(mode=0o700)  # an interrupted hand-over
    iso.prepare("proxy", ids_for("proxy"), paths, checkout / ".state")
    assert st(paths.private("proxy")) == (0o700, UIDS["proxy"], UIDS["proxy"])
    paths.private("legacy").mkdir(mode=0o700)
    (paths.private("legacy") / "something").write_text("x")
    with pytest.raises(iso.IsolationError, match="no ownership repair"):
        iso.prepare("legacy", ids_for("legacy"), paths, checkout / ".state")


@requires_root
def test_key_and_room_copies_follow_the_checkout(world_tmp, checkout, area):
    paths = area
    key = paths.key("translator")
    key.chmod(0o644)  # the service (its owner) loosened its copy: the next start rewrites it
    iso.prepare("translator", ids_for("translator"), paths, checkout / ".state")
    assert st(key)[0] == 0o600
    src = checkout / ".state/agents/translator.json"
    src.write_text(json.dumps({"name": "translator", "agent_id": "id", "api_key": "rotated-key-0123456789"}))
    iso.prepare("translator", ids_for("translator"), paths, checkout / ".state")
    assert key.read_bytes() == src.read_bytes() and st(key)[0] == 0o600
    assert iso.prepare("translator", ids_for("translator"), paths, checkout / ".state") == []  # idempotent
    src.unlink()
    (checkout / ".state/room.json").unlink()
    actions = iso.prepare("translator", ids_for("translator"), paths, checkout / ".state")
    assert not key.exists() and not paths.room.exists() and any("run provision" in a for a in actions)


# ------------------------------------------------------------------------------------------------ reset / clean

@requires_root
def test_reset_unlinks_known_names_through_checked_dirs(world_tmp, checkout, area):
    paths = area
    owners = {"proxy": UIDS["proxy"], "fuzzer": UIDS["fuzzer"], "module": UIDS["translator"]}
    target = world_tmp / "precious"
    target.write_text("keep me")
    (paths.private("proxy") / "router.json").symlink_to(target)  # unlink removes the link, never its target
    (paths.private("fuzzer") / "divergence_registry.json").write_text("{}")
    (paths.private("fuzzer") / "divergence_registry.json.staging").write_text("{}")
    (paths.private("fuzzer") / "other.json").write_text("{}")
    paths.module_file.write_text("x = 1\n")
    actions = iso.reset(paths, owners=owners)
    assert len([a for a in actions if a.startswith("removed")]) == 4
    assert target.read_text() == "keep me"
    assert sorted(os.listdir(paths.private("fuzzer"))) == ["other.json"]
    assert not paths.module_file.exists() and not os.path.lexists(paths.private("proxy") / "router.json")
    os.chown(paths.private("proxy"), UIDS["legacy"], UIDS["legacy"])
    with pytest.raises(iso.IsolationError, match="refusing to reset"):
        iso.reset(paths, owners=owners)
    shutil.rmtree(paths.private("proxy"))
    paths.private("proxy").symlink_to(world_tmp)
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.reset(paths, owners=owners)


@requires_root
def test_clean_empties_the_service_state_without_following_links(world_tmp, checkout, area):
    paths = area
    outside = world_tmp / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (paths.private("modern") / "escape").symlink_to(outside)
    (paths.private("modern") / "nested").mkdir()
    iso.clean(paths)
    assert os.listdir(paths.state) == [] and (outside / "keep").read_text() == "x"
    assert st(paths.state) == (0o755, 0, 0)


# ----------------------------------------------------------------------------------------- §4.5 privileges

def _dropped_view(path):
    out = {"resuid": os.getresuid(), "resgid": os.getresgid(), "groups": sorted(os.getgroups()),
           "umask": None, "caps": iso._cap_sets()}
    old = os.umask(0)
    os.umask(old)
    out["umask"] = old
    try:
        open(path).read()
        out["read"] = True
    except PermissionError:
        out["read"] = False
    for name, fn in (("setuid0", lambda: os.setuid(0)), ("setresuid0", lambda: os.setresuid(0, 0, 0)),
                     ("setgroups", lambda: os.setgroups([0]))):
        try:
            fn()
            out[name] = "succeeded"
        except PermissionError:
            out[name] = "refused"
    out["nnp"] = [ln for ln in open("/proc/self/status") if ln.startswith("NoNewPrivs")][0].split()[1]
    return out


@requires_root
def test_drop_privileges_to_nobody(world_tmp):
    secret = world_tmp / "secret"
    secret.write_text("root only")
    secret.chmod(0o600)

    def child():
        iso.drop_privileges(NOBODY, NOBODY, [NOBODY, 47123])
        return _dropped_view(str(secret))

    view = in_child(child)["result"]
    assert view["resuid"] == [NOBODY] * 3 and view["resgid"] == [NOBODY] * 3
    assert view["groups"] == [47123, NOBODY]  # exactly the requested ones: root's groups are replaced
    assert view["read"] is False
    assert view["setuid0"] == view["setresuid0"] == view["setgroups"] == "refused"
    assert view["umask"] == 0o077 and view["nnp"] == "1"
    assert view["caps"] == {"CapPrm": 0, "CapEff": 0, "CapAmb": 0}
    # an empty list really empties the supplementary groups
    assert in_child(lambda: (iso.drop_privileges(NOBODY, NOBODY, []), os.getgroups())[1])["result"] == []


@requires_root
def test_drop_privileges_refuses_root_ids():
    for args in ((0, NOBODY, []), (NOBODY, 0, []), (NOBODY, NOBODY, [0])):
        assert "refusing" in in_child(lambda a=args: iso.drop_privileges(*a))["error"]


# ------------------------------------------------------------------------------------ §4 exec, end to end

def _launch_in_child(name, argv, values, ids, paths, repo, *, leak: Path | None = None) -> dict:
    """Fork; the child runs iso.launch() (root -> component) with its stdout on a pipe; return the probe's report."""
    r, w = os.pipe()
    leaked = os.open(leak, os.O_RDONLY) if leak else None
    if leaked is not None:
        os.set_inheritable(leaked, True)  # an inheritable descriptor the operator's shell might have left open
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.dup2(w, 1)
            iso.launch(name, argv, values, ids, paths, repo)
        except BaseException as exc:  # noqa: BLE001
            os.write(1, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode())
        finally:
            os._exit(1)
    os.close(w)
    if leaked is not None:
        os.close(leaked)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    os.close(r)
    report = json.loads(data or b'{"error": "no output"}')
    report["_pid"] = pid
    if "--sleep" not in argv:
        os.waitpid(pid, 0)
    return report


def _values(checkout: Path, **extra) -> dict:
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "STRANGLER_STATE_DIR": str(checkout / ".state"),
            **SWITCH_SECRETS, "LD_LIBRARY_PATH": "/nonexistent", "GH_TOKEN": "gh-secret-token", **extra}


@requires_root
def test_launch_runs_the_proxy_as_its_user_with_only_what_it_needs(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    leak = checkout / ".state/switch.env"
    report = _launch_in_child("proxy", ["/root/x/.venv/bin/python", "-m", "targets.proxy.router", "--port", "9000",
                                        "--probe-path", str(leak), "--probe-path", str(paths.key("fuzzer"))],
                              _values(checkout), ids_for("proxy"), paths, checkout, leak=leak)
    assert "error" not in report, report
    uid = UIDS["proxy"]
    assert report["resuid"] == [uid] * 3 and report["resgid"] == [uid] * 3 and report["groups"] == []
    assert report["umask"] == 0o077
    assert report["sid"] == report["pid"] == report["_pid"]  # its own session, and the pid is the component itself
    assert report["fds"] == [0, 1, 2]  # the leaked descriptor and the launcher's pipes are gone
    assert report["cwd"] == str(paths.app)
    assert report["argv"][1:] == ["--port", "9000", "--probe-path", str(leak), "--probe-path",
                                  str(paths.key("fuzzer"))]
    assert report["cmdline"].split("\0")[:3] == [str(paths.python), "-m", "targets.proxy.router"]
    env = report["env"]
    assert env["ROUTER_ADMIN_TOKEN"] == ROUTER_TOKEN  # resolve_router_token() as root, from the checkout
    assert ROUTER_TOKEN not in report["cmdline"]
    for secret in (*SWITCH_SECRETS, "GH_TOKEN", "LD_LIBRARY_PATH", "SWITCH_URL"):
        assert secret not in env
    assert env["STRANGLER_STATE_DIR"] == str(paths.state) and env["HOME"] == str(paths.private("proxy"))
    assert report["reach"] == {str(leak): False, str(paths.key("fuzzer")): False}
    assert "NoNewPrivs:\t1" in report["status"]


@requires_root
def test_launch_gives_the_translator_its_pass_env_key_and_module(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    values = _values(checkout, STRANGLER_TRANSLATOR_PASS_ENV="LLM_API_KEY", LLM_API_KEY="llm-secret",
                     ROUTER_ADMIN_TOKEN="root-env-token")
    report = _launch_in_child("translator", ["python", "-m", "agents.translator_agent", "--probe-path",
                                             str(paths.key("translator"))], values, ids_for("translator"), paths,
                              checkout)
    assert "error" not in report, report
    env = report["env"]
    assert env["LLM_API_KEY"] == "llm-secret" and "ROUTER_ADMIN_TOKEN" not in env
    assert env["SWITCH_URL"] == "http://10.0.0.5:18000"  # connection.env, never switch.env
    assert env["MODERN_LISTS_PATH"] == str(paths.module_file)
    assert report["groups"] == [LOCK_GID]
    assert report["reach"] == {str(paths.key("translator")): True}


@requires_root
def test_launch_refuses_a_foreign_module_before_touching_anything(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    report = _launch_in_child("fuzzer", ["python", "-m", "agents.translator_agent"], _values(checkout),
                              ids_for("fuzzer"), paths, checkout)
    assert "may only run agents.fuzzer_agent" in report["error"]
    assert not paths.home.exists()


def test_exec_with_isolation_off_is_a_plain_exec():
    r = _cli("exec", "proxy", "--", "sh", "-c", 'echo "$SR_ISO_PROBE $(id -u)"',
             env={"STRANGLER_ISOLATION": "off", "SR_ISO_PROBE": "kept"})
    assert r.returncode == 0 and r.stdout == f"kept {os.geteuid()}\n", r.stderr


def test_exec_usage_and_unknown_component():
    assert _cli("exec", "proxy", "sh").returncode == 2  # no `--`
    r = _cli("exec", "nosuch", "--", "true", env={"STRANGLER_ISOLATION": "off"})
    assert r.returncode == 2 and "unknown component" in r.stderr


def test_exec_with_isolation_on_as_non_root_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(iso.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(iso, "_require_trusted", lambda st, what: None)
    monkeypatch.setattr(iso.os, "execvp", lambda *a: pytest.fail("ran unisolated"))
    monkeypatch.setattr(iso.os, "execve", lambda *a: pytest.fail("exec'd"))
    env = {**write_marker(tmp_path), "STRANGLER_ISOLATION": "on"}
    with pytest.raises(iso.IsolationError, match="not root"):
        iso.exec_component("proxy", ["python", "-m", "targets.proxy.router"], environ=env)


def test_exec_with_isolation_on_but_no_users_exits_2_without_running(tmp_path):
    if iso._lookup(iso.pwd.getpwnam, "sr-proxy") is not None:
        pytest.skip("the service users exist on this host")
    marker = write_marker(tmp_path, service_home=str(tmp_path / "srv"))
    flag = tmp_path / "ran"
    command = ["sh", "-c", f"touch {shlex.quote(str(flag))}"]
    # control: the very same command line does run (and write the flag) when isolation is off ...
    assert _cli("exec", "proxy", "--", *command, env={"STRANGLER_ISOLATION": "off"}).returncode == 0
    assert flag.exists()
    flag.unlink()
    # ... and with isolation on, without the service users, it is refused before anything runs or is created
    r = _cli("exec", "proxy", "--", *command,
             env={**marker, "STRANGLER_ISOLATION": "on", "STRANGLER_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 2 and "sr-proxy does not exist" in r.stderr, r.stderr
    assert not flag.exists() and not (tmp_path / "srv").exists() and not (tmp_path / "state").exists()


# ------------------------------------------------------------------------------------- §6 verify machinery

@requires_root
def test_access_matrix_as_each_synthetic_user(world_tmp, checkout, area):
    paths = area
    for comp in iso.COMPONENTS.values():
        probes = iso.access_probes(comp, paths, checkout / ".state", repo=checkout)
        rows = iso.run_probes(comp.name, ids_for(comp), probes)
        failed = [r for r in rows if r.status != "PASS"]
        assert not failed, failed
        checks = {r.check: r.actual for r in rows}
        assert checks["write own private dir"] == "yes" and checks["write app/"] == "no"
        assert checks["open production.lock read-write"] == ("yes" if comp.production_lock else "no")
        assert checks["create in module/"] == ("yes" if comp.module_writer else "no")
        assert checks["reach the checkout"] == "no" and checks["reach the operator's home"] == "no"
        if comp.agent:
            assert checks["read own API key copy"] == checks["read room.json"] == "yes"


@requires_root
def test_access_matrix_catches_an_exposed_key(world_tmp, checkout, area):
    paths = area
    paths.key("translator").chmod(0o644)
    rows = iso.run_probes("fuzzer", ids_for("fuzzer"), iso.access_probes("fuzzer", paths, checkout / ".state",
                                                                          repo=checkout))
    bad = [r for r in rows if r.status == "FAIL"]
    assert [r.check for r in bad] == ["read translator's API key copy"]
    rows = iso.run_probes("legacy", ids_for("legacy"), iso.access_probes(
        "legacy", paths, checkout / ".state", repo=checkout, pids={"proxy": os.getpid()}))
    assert [r for r in rows if r.check.startswith("signal proxy")][0].status == "PASS"  # cannot signal root's pid


@requires_root
def test_smoke_imports_each_module_as_its_user(world_tmp, checkout, area):
    paths = area
    for comp in iso.COMPONENTS.values():
        ok, output = iso.smoke(comp, ids_for(comp), paths)
        assert ok, (comp.name, output)
    (paths.app / "agents/fuzzer_agent.py").write_text("raise SystemExit('broken import')\n")
    ok, output = iso.smoke("fuzzer", ids_for("fuzzer"), paths)
    assert not ok and "broken import" in output
    (paths.app / "agents/fuzzer_agent.py").write_text("open('/etc/shadow')\n")
    ok, output = iso.smoke("fuzzer", ids_for("fuzzer"), paths)
    assert not ok and "Permission denied" in output


@requires_root
def test_secret_scan_finds_readable_secrets_except_the_own_key(world_tmp, checkout, area):
    paths = area
    scan = world_tmp / "scan"
    scan.mkdir()
    (scan / "leak.log").write_text(f"oops {SWITCH_SECRETS['GATEWAY_ADMIN_PASSWORD']} oops")
    (scan / "private.log").write_text(SWITCH_SECRETS["MATTERMOST_USER_PASSWORD"])
    (scan / "private.log").chmod(0o600)
    secrets = iso.secret_values(checkout / ".state", checkout, env={})
    secrets["short"] = "abc"
    subjects = {"fuzzer": ids_for("fuzzer"), "translator": ids_for("translator")}
    own = {n: str(paths.key(n)) for n in subjects}
    rows = iso.secret_scan(subjects, secrets, [paths.home, scan], own_keys=own)
    fails = [r for r in rows if r.status == "FAIL"]
    exposed = {(r.subject, r.check.split(" exposes ")[0]) for r in fails if " exposes " in r.check}
    assert exposed == {("fuzzer", str(scan / "leak.log")), ("translator", str(scan / "leak.log"))}
    assert any(r.status == "SKIP" and "short" in r.check for r in rows)
    (scan / "leak.log").unlink()
    rows = iso.secret_scan(subjects, secrets, [paths.home, scan], own_keys=own)
    assert all(r.status != "FAIL" for r in rows), rows  # own key copies are fine; the checkout is unreachable
    # without the own-key exception, the key copy is a hit
    rows = iso.secret_scan({"fuzzer": ids_for("fuzzer")}, secrets, [paths.home], own_keys={})
    assert any(r.status == "FAIL" and str(paths.key("fuzzer")) in r.check for r in rows)


@requires_root
def test_scan_lists_links_as_root_but_resolves_them_only_as_the_user(world_tmp):
    scan = world_tmp / "scan"
    (scan / "sub").mkdir(parents=True)
    outside = world_tmp / "elsewhere"  # outside the scan roots
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "backup.json").write_text("key needle-0123456789")
    (outside / "private.txt").write_text("needle-0123456789")
    (outside / "private.txt").chmod(0o600)
    (outside / "deep" / "loop").symlink_to(outside)  # a cycle: walked once
    (scan / "linked").symlink_to(outside)            # a readable directory alias
    (scan / "file-link").symlink_to(outside / "deep" / "backup.json")
    (scan / "private-link").symlink_to(outside / "private.txt")
    os.mkfifo(scan / "sub" / "fifo")                 # opening it for reading would block
    candidates = iso.scan_candidates([scan])
    listed = {os.path.join(d, f) for d, files in candidates for f in files}
    assert listed == {str(scan / "linked"), str(scan / "file-link"), str(scan / "private-link")}
    assert all(not d.startswith(str(scan / "linked")) for d, _ in candidates)  # root never walked the alias
    started = time.monotonic()
    result = iso.run_as(iso.Ids(NOBODY, NOBODY), iso.search_readable, candidates, {"n": b"needle-0123456789"})
    assert time.monotonic() - started < 10 and result["truncated"] is False
    assert sorted(h[0] for h in result["hits"]) == [str(scan / "file-link"),
                                                     str(scan / "linked" / "deep" / "backup.json")]
    # a candidate swapped for a FIFO after the listing is never opened for reading
    result = iso.run_as(iso.Ids(NOBODY, NOBODY), iso.search_readable, [(str(scan / "sub"), ["fifo"])], {})
    assert result == {"read": 0, "hits": [], "truncated": False}
    # the caps bound a walk through links; hitting them is reported
    result = iso.run_as(iso.Ids(NOBODY, NOBODY), lambda: iso.search_readable(
        [(str(scan), ["linked"])], {"n": b"needle-0123456789"}, link_max_files=0))
    assert result["truncated"] is True


@requires_root
def test_secret_scan_fails_on_a_symlink_alias_and_spares_the_own_key_alias(world_tmp, checkout, area):
    paths = area
    archive = world_tmp / "archive"
    archive.mkdir()
    (archive / "key.json").write_text(json.dumps({"api_key": AGENT_KEYS["cutover-gate"]}))
    scan = world_tmp / "scan"
    scan.mkdir()
    (scan / "exposed-key").symlink_to(archive / "key.json")
    (scan / "my-key").symlink_to(paths.key("fuzzer"))  # the fuzzer's own copy, reached through an alias
    secrets = {f"api-key:{n}": k for n, k in AGENT_KEYS.items()}
    rows = iso.secret_scan({"fuzzer": ids_for("fuzzer")}, secrets, [scan],
                           own_keys={"fuzzer": str(paths.key("fuzzer"))})
    fails = [r.check for r in rows if r.status == "FAIL"]
    assert fails == [f"{scan / 'exposed-key'} exposes api-key:cutover-gate",
                     "2 readable files searched for 3 secret values"], rows


@requires_root
def test_run_as_kills_a_child_that_never_finishes():
    started = time.monotonic()
    with pytest.raises(iso.IsolationError, match="did not finish within 1s"):
        iso.run_as(iso.Ids(NOBODY, NOBODY), time.sleep, 30, timeout=1.0)
    assert time.monotonic() - started < 10
    assert iso.run_as(iso.Ids(NOBODY, NOBODY), lambda: __import__("resource").getrlimit(
        __import__("resource").RLIMIT_CORE)) == [1, 1]  # no core dump of a child that holds secrets


def test_secret_values_collects_every_kind(world_tmp, checkout):
    infra = checkout / ".switch-infra"
    infra.mkdir()
    (infra / ".env").write_text("DB_PASSWORD=db-pass-000111\nSECRET_KEYS=k1:aaaaaaaabbbbbbbb,k2:ccccccccdddddddd\n"
                                "SWITCH_VERSION=0.31.0\nGATEWAY_ADMIN_EMAIL=admin@example.com\n")
    native = checkout / ".native"
    native.mkdir()
    (native / "mm-env.sh").write_text("export MM_SQLSETTINGS_DATASOURCE='postgres://mm:mm%40pass-xyz@127.0.0.1:5432/m'\n")
    values = iso.secret_values(checkout / ".state", checkout, env={})
    assert values["router.token"] == ROUTER_TOKEN
    assert {values[f"api-key:{n}"] for n in AGENT_KEYS} == set(AGENT_KEYS.values())
    assert values["switch.env:GATEWAY_ADMIN_PASSWORD"] == SWITCH_SECRETS["GATEWAY_ADMIN_PASSWORD"]
    assert values[".switch-infra/.env:DB_PASSWORD"] == "db-pass-000111"
    assert values[".switch-infra/.env:SECRET_KEYS[1]"] == "ccccccccdddddddd"
    assert values["mattermost-db-password"] == "mm@pass-xyz"
    assert not any("SWITCH_URL" in k or "VERSION" in k or "EMAIL" in k for k in values)


@requires_root
def test_process_checks_on_a_live_isolated_component(world_tmp, checkout):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    report = _launch_in_child("proxy", ["python", "-m", "targets.proxy.router", "--sleep"], _values(checkout),
                              ids_for("proxy"), paths, checkout)
    pid = report["_pid"]
    try:
        assert "error" not in report, report
        assert pid in iso.component_instances().get("proxy", [])  # found without any pid file
        # (this host may run its own proxy too: the inventory is passed explicitly to keep the test deterministic)
        rows = iso.process_checks({"proxy": ids_for("proxy"), "legacy": ids_for("legacy")},
                                  {"router.token": ROUTER_TOKEN}, {"proxy": pid}, instances={"proxy": [pid]})
        by_check = {r.check: r.status for r in rows if r.subject == "proxy"}
        assert all(s == "PASS" for s in by_check.values()), rows
        assert any("own session" in c for c in by_check) and any("ROUTER_ADMIN_TOKEN" in c for c in by_check)
        assert any("tracked instance" in c for c in by_check)
        assert [r.status for r in rows if r.subject == "legacy"] == ["SKIP"]
        # the same process judged as if it were the legacy server: it must not hold the token
        rows = iso.process_checks({"legacy": ids_for("proxy")}, {"router.token": ROUTER_TOKEN}, {}, instances={})
        assert any(r.status == "FAIL" and "ROUTER_ADMIN_TOKEN" in r.check for r in rows)
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def test_process_checks_fail_on_untracked_root_and_duplicate_instances():
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m",
                               "targets.legacy_openlibrary.server"]) for _ in range(2)]
    try:
        time.sleep(0.3)
        found = iso.component_instances().get("legacy", [])
        assert {p.pid for p in procs} <= set(found)  # no pid file needed to find them
        ids = ids_for("legacy")
        one = procs[0].pid
        rows = iso.process_checks({"legacy": ids}, {}, {}, instances={"legacy": [one]})  # untracked, runs as root
        bad = {r.check for r in rows if r.status == "FAIL"}
        assert f"pid {one} is the tracked instance (pid file: None)" in bad
        assert f"pid {one} runs as uid {ids.uid} (real/effective/saved/fs)" in bad
        assert all(r.status != "SKIP" for r in rows)
        rows = iso.process_checks({"legacy": ids}, {}, {"legacy": one},
                                  instances={"legacy": [p.pid for p in procs]})  # tracked, plus a duplicate
        assert any(r.status == "FAIL" and r.check.startswith("exactly one instance") for r in rows)
        assert any(r.status == "FAIL" and r.check.startswith(f"pid {procs[1].pid} is the tracked") for r in rows)
    finally:
        for p in procs:
            p.kill()
            p.wait()


# ------------------------------------------------------------------------------------- §5 setup / disable

def test_plan_accounts_creates_exactly_the_users_and_group():
    none = lambda name: (_ for _ in ()).throw(KeyError(name))  # noqa: E731
    cmds = iso.plan_accounts(getpwnam=none, getgrnam=none)
    assert cmds[0] == ["groupadd", "--system", "sr-prodlock"]
    users = [c for c in cmds if c[0] == "useradd"]
    assert [c[-1] for c in users] == [f"sr-{n}" for n in iso.COMPONENTS]
    assert all(c[1:-1] == ["--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell",
                           "/usr/sbin/nologin", "--user-group"] for c in users)
    assert ["gpasswd", "-M", "sr-translator,sr-cutover-gate", "sr-prodlock"] in cmds


def test_plan_accounts_refuses_to_adopt_a_foreign_account():
    class Pw:
        pw_uid, pw_gid, pw_shell, pw_dir = 1000, 1000, "/bin/bash", "/home/x"

    class Gr:
        gr_gid, gr_mem = 1000, []

    with pytest.raises(iso.IsolationError, match="refusing to adopt"):
        iso.plan_accounts(getpwnam=lambda n: Pw(), getgrnam=lambda n: Gr())


@requires_root
def test_setup_then_disable_on_a_temporary_host(tmp_path, world_tmp, checkout):
    env = {"STRANGLER_ISOLATION_MARKER": str(tmp_path / "etc" / "isolation.json"),
           "STRANGLER_STATE_DIR": str(checkout / ".state")}
    seen: list[tuple[list[str], str]] = []

    def fake_run(cmd):  # records the account commands; the marker must already say setting-up
        seen.append((list(cmd), json.loads(Path(env["STRANGLER_ISOLATION_MARKER"]).read_text())["state"]))

    def resolve(comp):
        return ids_for(comp)

    with pytest.raises(iso.IsolationError, match="refuses while"):
        iso.setup(env, service_home=world_tmp / "srv", run=fake_run, resolve=resolve, repo=checkout,
                  running=lambda: {4242: "agents.fuzzer_agent"})
    assert not (tmp_path / "etc").exists()
    with pytest.raises(iso.IsolationError, match="outside"):
        iso.setup(env, service_home=checkout / "srv", run=fake_run, resolve=resolve, repo=checkout,
                  running=lambda: {})
    actions = iso.setup(env, service_home=world_tmp / "srv", run=fake_run, resolve=resolve, repo=checkout,
                        running=lambda: {})
    marker = json.loads(Path(env["STRANGLER_ISOLATION_MARKER"]).read_text())
    assert marker["state"] == "ready" and marker["repo"] == str(checkout)
    assert marker["service_home"] == str(world_tmp / "srv")
    if iso._lookup(iso.pwd.getpwnam, "sr-proxy") is None:
        assert seen and all(state == "setting-up" for _, state in seen)
    assert sum("startup smoke" in a for a in actions) == len(iso.COMPONENTS)
    paths = iso.service_paths(world_tmp / "srv")
    assert st(paths.home) == (0o755, 0, 0) and st(paths.private("modern"))[1] == UIDS["modern"]
    # idempotent
    iso.setup(env, run=fake_run, resolve=resolve, repo=checkout, running=lambda: {})
    # a failing smoke import leaves the marker at setting-up (mode would exit 2)
    (checkout / "agents/fuzzer_agent.py").write_text("raise SystemExit('no')\n")
    with pytest.raises(iso.IsolationError, match="startup smoke failed for fuzzer"):
        iso.setup(env, run=fake_run, resolve=resolve, repo=checkout, running=lambda: {})
    assert json.loads(Path(env["STRANGLER_ISOLATION_MARKER"]).read_text())["state"] == "setting-up"
    with pytest.raises(iso.IsolationError, match="refuses while"):
        iso.disable(env, purge=True, run=fake_run, running=lambda: {1: "uid 47001"})
    with pytest.raises(iso.IsolationError, match="belongs to"):  # the marker names the fake checkout
        iso.disable(env, purge=True, run=fake_run, running=lambda: {})
    marker = json.loads(Path(env["STRANGLER_ISOLATION_MARKER"]).read_text())
    marker["repo"] = str(ROOT)
    Path(env["STRANGLER_ISOLATION_MARKER"]).write_text(json.dumps(marker))
    actions = iso.disable(env, purge=True, run=fake_run, running=lambda: {})
    assert not Path(env["STRANGLER_ISOLATION_MARKER"]).exists() and not paths.home.exists()
    assert any("removed" in a for a in actions)


@requires_root
def test_purge_refuses_a_service_home_with_unexpected_content(world_tmp):
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    (paths.home / "operator-notes").write_text("x")
    with pytest.raises(iso.IsolationError, match="unexpected entries"):
        iso._purge_home(paths)
    assert (paths.home / "operator-notes").exists()


def test_running_components_finds_an_untracked_unisolated_component():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m", "agents.fuzzer_agent"])
    try:
        time.sleep(0.2)
        assert iso.running_components().get(proc.pid) == "agents.fuzzer_agent"
    finally:
        proc.kill()
        proc.wait()


# ----------------------------------------------------------------------------------------------- the scripts

def _function(script: Path, *names: str) -> str:
    text = script.read_text()
    out = []
    for name in names:
        match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.MULTILINE | re.DOTALL)
        assert match, f"{name}() not found in {script}"
        out.append(match.group(0))
    return "\n".join(out)


def _kill_marked(mark: str) -> None:
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                if f"SR_ISO_TEST_MARK={mark}".encode() in (entry / "environ").read_bytes():
                    os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                pass


def test_isolate_prefix_keeps_a_quoted_value_with_spaces_as_one_word():
    script = ('a="/tmp/x y/py thon"; name="cutover gate"; show() { printf "<%s>" "$@"; echo; }\n'
              'show ${ISOLATE:+"$a" -m agents.isolation exec "$name" --} "$a" -m mod\n'
              'ISOLATE=1; show ${ISOLATE:+"$a" -m agents.isolation exec "$name" --} "$a" -m mod\n')
    out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert out == ["</tmp/x y/py thon><-m><mod>",
                   "</tmp/x y/py thon><-m><agents.isolation><exec><cutover gate><--></tmp/x y/py thon><-m><mod>"]


def test_scripts_launch_through_the_isolation_prefix_and_decide_the_mode_first():
    targets = (ROOT / "scripts/targets.sh").read_text()
    agents = (ROOT / "scripts/agents.sh").read_text()
    assert ('nohup ${ISOLATE:+"$PY" -m agents.isolation exec "$name" --} "$PY" -m "$module" --port "$port" '
            '>"$RUN/$name.log" 2>&1 </dev/null 9>&- &') in targets
    assert ('nohup ${ISOLATE:+"$PYTHON" -m agents.isolation exec "$name" --} "$PYTHON" -m "$module" '
            '>>"$log" 2>&1 </dev/null 9>&- &') in agents
    for text in (targets, agents):
        mode_at = text.index("-m agents.isolation mode --run-dir \"$RUN\"")
        assert mode_at < text.index('mkdir -p "$RUN"') and mode_at < text.index('exec 9>"$RUN/')
        assert 'ISOLATE=""' in text and "on) ISOLATE=1 ;;" in text
    assert 'RUN="$STATE/run"' in targets and 'RUN="${STRANGLER_RUN_DIR:-$STATE/run}"' in agents
    state_line = 'STATE="${STRANGLER_STATE_DIR:-$ROOT/.state}"'
    assert state_line in targets and state_line in agents
    fresh = targets[targets.index('if [ "${2:-}" = "--fresh" ]'):targets.index('echo "starting targets"')]
    for part in ('"${MODERN_LISTS_PATH:-$ROOT/targets/modern_fastapi/app/lists.py}"',
                 '"${ROUTER_STATE_FILE:-$STATE/proxy/router.json}"', '"$STATE/fuzzer/divergence_registry.json"',
                 '"$STATE/fuzzer/divergence_registry.json.staging"', '-m agents.isolation reset'):
        assert part in fresh


def test_make_targets_for_isolation():
    def dry(target):
        r = subprocess.run(["make", "-n", target], cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert "agents.isolation setup" in dry("isolate")
    assert "agents.isolation verify" in dry("isolate-verify")
    assert "agents.isolation disable" in dry("isolate-off")
    clean = dry("clean-state")
    assert clean.index("agents.sh stop") < clean.index("agents.isolation clean") < clean.index("find ")
    assert "${STRANGLER_STATE_DIR:-.state}" in clean


@pytest.fixture
def fake_module(tmp_path):
    (tmp_path / "fake_iso_target.py").write_text(FAKE_SERVER_SOURCE)
    (tmp_path / "run").mkdir()
    yield tmp_path
    _kill_marked(str(tmp_path))


FAKE_INTERPRETER = r'''#!/bin/bash
# Stands in for "$PY": `-m agents.isolation exec <name> -- <argv...>` records how the launcher was invoked (its
# STRANGLER_ISOLATION and argv) and execs <argv...>, keeping the pid; anything else runs the real interpreter.
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "agents.isolation" ]; then
  { printf 'STRANGLER_ISOLATION=%s\n' "${STRANGLER_ISOLATION:-}"; printf '%s\n' "$@"; } >>"$LAUNCH_RECORD"
  shift 4
  if [ "${1:-}" = "--" ]; then shift; fi
  exec "$@"
fi
exec @PYTHON@ "$@"
'''


def fake_interpreter(directory: Path) -> Path:
    path = directory / "fake-python"
    path.write_text(FAKE_INTERPRETER.replace("@PYTHON@", shlex.quote(sys.executable)))
    path.chmod(0o755)
    return path


def _svc(tmp: Path, port: int, isolated_pid_rc: int, *pre: str, listener_rc: int = 0,
         py: str | None = None) -> subprocess.CompletedProcess:
    harness = "\n".join([
        "set -euo pipefail",
        f"ROOT={shlex.quote(str(tmp))}", f"RUN={shlex.quote(str(tmp / 'run'))}",
        f"PY={shlex.quote(py or str(fake_interpreter(tmp)))}",
        f"export PYTHONPATH={shlex.quote(str(ROOT))} SR_ISO_TEST_MARK={shlex.quote(str(tmp))}",
        f"export LAUNCH_RECORD={shlex.quote(str(tmp / 'launch.record'))}",
        _function(ROOT / "scripts/targets.sh", "is_target_pid", "svc"),
        # stand-ins for "runs as sr-<name>" and "sr-<name> owns the listener" (no such user here)
        f"isolated_pid() {{ return {isolated_pid_rc}; }}", f"isolated_listener() {{ return {listener_rc}; }}",
        "ISOLATE=1", *pre,
        f"svc legacy fake_iso_target {port} /health", ""])
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60,
                          start_new_session=True)


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_svc_isolated_launch_line_execs_into_the_server(fake_module):
    port = free_port()
    proc = _svc(fake_module, port, 0)
    assert proc.returncode == 0 and f"legacy up on :{port}" in proc.stdout, proc.stdout + proc.stderr
    pid = int((fake_module / "run/legacy.pid").read_text())
    argv = (Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"))
    assert argv[1:5] == [b"-m", b"fake_iso_target", b"--port", str(port).encode()]  # exec'd: no launcher left
    assert b"agents.isolation" not in b" ".join(argv)
    assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5, trust_env=False).text == "ok"
    # the launcher was invoked with isolation forced on (a marker gone since the mode check is then an error)
    py = str(fake_module / "fake-python")
    assert (fake_module / "launch.record").read_text().splitlines() == [
        "STRANGLER_ISOLATION=on", "-m", "agents.isolation", "exec", "legacy", "--", py, "-m", "fake_iso_target",
        "--port", str(port)]
    # already up and tracked: accepted only because the stand-in says it runs as its user
    again = _svc(fake_module, port, 0)
    assert again.returncode == 0 and "already up" in again.stdout
    refused = _svc(fake_module, port, 1)
    assert refused.returncode == 1 and "not as a tracked legacy running as sr-legacy" in refused.stderr
    refused = _svc(fake_module, port, 0, listener_rc=1)  # tracked and isolated, but someone else owns the port
    assert refused.returncode == 1 and "not as a tracked legacy" in refused.stderr
    (fake_module / "run/legacy.pid").write_text("999999\n")  # healthy port, no tracked process
    refused = _svc(fake_module, port, 0)
    assert refused.returncode == 1 and "not as a tracked legacy" in refused.stderr
    assert _alive(pid)  # an error, but a process found already running is never killed by start


@pytest.mark.parametrize("pid_rc, listener_rc", [(1, 0), (0, 1)])
def test_svc_isolated_kills_its_server_if_it_is_not_isolated(fake_module, pid_rc, listener_rc):
    port = free_port()
    proc = _svc(fake_module, port, pid_rc, listener_rc=listener_rc)
    assert proc.returncode == 1 and "not from pid" in proc.stderr and "sr-legacy" in proc.stderr, proc.stderr
    assert not (fake_module / "run/legacy.pid").exists()
    for _ in range(50):
        if not port_in_use(port):
            break
        time.sleep(0.1)
    assert not port_in_use(port), "the unisolated server was left serving"


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_isolated_listener_reads_the_socket_owner(fake_module):
    port = free_port()
    assert _svc(fake_module, port, 0).returncode == 0  # the fake server runs as this (root) user
    for uid, expected in ((os.geteuid(), 0), (NOBODY, 1)):
        harness = "\n".join(["set -euo pipefail", f"id() {{ echo {uid}; }}",  # stands in for `id -u sr-legacy`
                             _function(ROOT / "scripts/targets.sh", "isolated_listener"),
                             f"isolated_listener {port} legacy"])
        assert subprocess.run(["bash", "-c", harness], timeout=30).returncode == expected
    harness = "\n".join(["set -euo pipefail", f"id() {{ echo {os.geteuid()}; }}",
                         _function(ROOT / "scripts/targets.sh", "isolated_listener"),
                         f"isolated_listener {free_port()} legacy"])
    assert subprocess.run(["bash", "-c", harness], timeout=30).returncode == 1  # no listener at all


def test_stop_one_isolated_also_kills_strays_of_the_service_user(tmp_path):
    calls = tmp_path / "calls"
    harness = "\n".join([
        "set -euo pipefail", f"RUN={shlex.quote(str(tmp_path))}", "ISOLATE=1",
        f"pkill() {{ echo \"$*\" >>{shlex.quote(str(calls))}; }}",
        _function(ROOT / "scripts/targets.sh", "is_target_pid", "stop_one"),
        "echo 999999 >\"$RUN/legacy.pid\"", "stop_one legacy targets.legacy_openlibrary.server 9001"])
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().splitlines() == ["-KILL -u sr-legacy"]
    agents = (ROOT / "scripts/agents.sh").read_text()
    assert 'pkill -KILL -u "sr-$name"' in _function(ROOT / "scripts/agents.sh", "stop_all")
    assert 'if [ -n "$ISOLATE" ]; then umask 077; fi' in agents
    assert 'if [ -n "$ISOLATE" ]; then umask 077; fi' in (ROOT / "scripts/targets.sh").read_text()


@pytest.mark.parametrize("marker_repo, error", [("/elsewhere", "belongs to /elsewhere"), (None, "no marker")])
def test_svc_isolated_launch_fails_loudly_when_the_launcher_refuses(fake_module, tmp_path, marker_repo, error):
    """The real launcher, as svc() runs it when isolated: a marker for another checkout, or a marker that vanished
    after the script's mode check, is an error (svc forces STRANGLER_ISOLATION=on), never an unisolated start."""
    port = free_port()
    if marker_repo:
        marker = write_marker(tmp_path, repo=marker_repo)["STRANGLER_ISOLATION_MARKER"]
    else:
        marker = str(tmp_path / "gone" / "isolation.json")
    proc = _svc(fake_module, port, 0, "export STRANGLER_ISOLATION=auto",
                f"export STRANGLER_ISOLATION_MARKER={shlex.quote(marker)}", py=sys.executable)
    assert proc.returncode == 1 and "exited during start-up" in proc.stderr
    assert error in proc.stderr  # the launcher's error, from the log
    assert not port_in_use(port)


def _start_one(run: Path, prefix: str, *pre: str) -> subprocess.CompletedProcess:
    harness = "\n".join([
        "set -euo pipefail",
        f"ROOT={shlex.quote(str(ROOT))}", f"RUN={shlex.quote(str(run))}",
        f"PYTHON={shlex.quote(str(fake_interpreter(run)))}", f"PREFIX={shlex.quote(prefix)}",
        f"export SR_ISO_TEST_MARK={shlex.quote(str(run))} LAUNCH_RECORD={shlex.quote(str(run / 'launch.record'))}",
        _function(ROOT / "scripts/agents.sh", "module_for", "pid_file", "log_file", "read_pid", "is_agent_pid",
                  "agent_pid", "start_one"),
        *pre, "start_one translator", ""])
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60,
                          start_new_session=True)


def test_agents_start_one_isolated_execs_into_the_agent(tmp_path):
    try:
        proc = _start_one(tmp_path, "tests.fake_agent_", "isolated_pid() { return 0; }", "ISOLATE=1")
        assert proc.returncode == 0 and "translator started pid=" in proc.stdout, proc.stdout + proc.stderr
        pid = int((tmp_path / "translator.pid").read_text())
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        assert argv[1:3] == [b"-m", b"tests.fake_agent_translator_agent"] and b"agents.isolation" not in argv
        record = (tmp_path / "launch.record").read_text().splitlines()
        assert record[:6] == ["STRANGLER_ISOLATION=on", "-m", "agents.isolation", "exec", "translator", "--"]
        # running but (per the stand-in) not as sr-translator: an error naming the fix, never "already running"
        proc = _start_one(tmp_path, "tests.fake_agent_", "isolated_pid() { return 1; }", "ISOLATE=1")
        assert proc.returncode == 1 and "agents.sh restart" in proc.stderr
    finally:
        _kill_marked(str(tmp_path))


def test_agents_start_one_isolated_fails_when_the_exec_never_happens(tmp_path):
    proc = _start_one(tmp_path, "tests.does_not_exist_", "isolated_pid() { return 1; }", "ISOLATE=1")
    assert proc.returncode == 1 and "did not start as sr-translator" in proc.stderr, proc.stdout + proc.stderr


# ------------------------------------------------------------------------------- review round 2 (Opus, codex)

@pytest.mark.parametrize("url, ok", [
    ("http://127.0.0.1:18000", True), ("http://[::1]:18000", True), ("http://10.2.3.4:18000/", True),
    ("http://localhost:18000", False), ("http://LOCALHOST:18000", False), ("http://switch.example:18000", False),
    ("http://ip6-localhost:1", False), ("not a url", False)])
@pytest.mark.parametrize("key", ["SWITCH_URL", "PROXY_URL", "LEGACY_URL", "MODERN_URL"])
def test_component_urls_must_name_an_ip_literal(key, url, ok):
    """Opus #1: `localhost` may resolve to ::1, where any service uid can listen and collect bearer keys."""
    parent = {**PARENT_ENV, key: url}
    switch_url = url if key == "SWITCH_URL" else "http://127.0.0.1:18000"
    comp = "cutover-gate"  # receives all four
    if ok:
        assert iso.child_env(comp, parent, paths=iso.service_paths("/srv/sr"), router_token="t",
                             switch_url=switch_url)[key] == url
    else:
        with pytest.raises(iso.IsolationError, match="IP literal"):
            iso.child_env(comp, parent, paths=iso.service_paths("/srv/sr"), router_token="t", switch_url=switch_url)
    assert iso.DEFAULT_SWITCH_URL == "http://127.0.0.1:18000"


def _listen_as(uid: int, host: str, port: int) -> subprocess.Popen:
    code = ("import socket, sys, time\n"
            f"s = socket.socket(socket.AF_INET6 if ':' in {host!r} else socket.AF_INET)\n"
            f"s.bind(({host!r}, {port})); s.listen(); print('up', flush=True); time.sleep(30)\n")
    proc = subprocess.Popen([BASE_PYTHON, "-I", "-c", code], user=uid, group=uid, extra_groups=[], cwd="/",
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "up"
    return proc


@requires_root
def test_listener_checks_cover_both_families_and_every_owner():
    """Opus #1c: every listener on an expected port, [::1] and [::] included, must belong to the expected uid."""
    port = free_port()
    procs = [_listen_as(NOBODY, "127.0.0.1", port), _listen_as(NOBODY, "::1", port)]
    try:
        found = iso.tcp_listeners().get(port, [])
        assert sorted(found) == sorted([("127.0.0.1", NOBODY), ("::1", NOBODY)])
        rows = iso.listener_checks({port: ("proxy", NOBODY)})
        assert [r.status for r in rows] == ["PASS", "PASS"]
        assert [r.status for r in iso.listener_checks({port: ("proxy", 4242)})] == ["FAIL", "FAIL"]
        squatter = _listen_as(0, "::1", free_port())  # nothing on a port at all: SKIP; a root squatter: FAIL
        try:
            other = squatter_port = iso.tcp_listeners()
            squatter_port = next(p for p, socks in other.items() if any(u == 0 and a == "::1" for a, u in socks)
                                 and p not in (port,))
            assert iso.listener_checks({squatter_port: ("proxy", NOBODY)})[0].status == "FAIL"
        finally:
            squatter.kill()
            squatter.wait()
        assert iso.listener_checks({free_port(): ("legacy", NOBODY)})[0].status == "SKIP"
    finally:
        for p in procs:
            p.kill()
            p.wait()


@requires_root
def test_llm_cli_check_as_the_translator(world_tmp):
    """Opus #3: in LLM mode the CLI must run for sr-translator and only root may be able to change it."""
    ids = ids_for("translator")
    assert iso.llm_cli_check(ids, {"PATH": "/usr/bin:/bin"}).status == "SKIP"  # deterministic by default
    llm = {"PATH": "/usr/bin:/bin", "STRANGLER_TRANSLATOR_MODE": "llm"}
    ok = iso.llm_cli_check(ids, {**llm, "STRANGLER_LLM_CMD": "true --flag"})
    assert ok.status == "PASS" and ok.actual.endswith("/true")
    missing = iso.llm_cli_check(ids, {**llm, "STRANGLER_LLM_CMD": "no-such-llm-cli-xyz -p"})
    assert missing.status == "FAIL" and "install the LLM CLI outside /root" in missing.actual
    unreachable = iso.llm_cli_check(ids, {**llm, "STRANGLER_LLM_CMD": "/root/.local/bin/claude -p"})
    assert unreachable.status == "FAIL"
    tools = world_tmp / "tools"
    tools.mkdir()
    cli = tools / "llm"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    os.chown(tools, ids.uid, ids.gid)  # the translator could swap its own CLI: refused
    mutable = iso.llm_cli_check(ids, {**llm, "STRANGLER_LLM_CMD": str(cli)})
    assert mutable.status == "FAIL" and "owned by uid" in mutable.actual


@requires_root
def test_drop_privileges_calls_prctl_and_joins_a_keyring_in_order(monkeypatch):
    """Opus #6/#7, and a test that would notice a missing prctl (NNP is inherited in this pod): observe the calls."""
    def child():
        calls = []
        real_prctl, real_join = iso._prctl, iso._join_session_keyring
        def prctl(option, arg):
            calls.append(["prctl", option, arg, os.geteuid()])
            return real_prctl(option, arg)
        iso._prctl = prctl
        iso._join_session_keyring = lambda: (calls.append(["keyring", os.geteuid()]), real_join())[1]
        iso.drop_privileges(NOBODY, NOBODY, [])
        return calls

    calls = in_child(child)["result"]
    assert calls == [["prctl", iso._PR_SET_DUMPABLE, 0, 0],            # before the uid changes
                     ["prctl", iso._PR_SET_DUMPABLE, 0, NOBODY],       # again: setresuid reset it
                     ["prctl", iso._PR_SET_NO_NEW_PRIVS, 1, NOBODY],
                     ["keyring", NOBODY]]

    def dumpable_and_keyring():
        lib = iso._libc()
        lib.syscall.restype = iso.ctypes.c_long
        get = lambda: lib.syscall(iso.ctypes.c_long(250), iso.ctypes.c_long(0), iso.ctypes.c_long(-3),  # noqa: E731
                                  iso.ctypes.c_long(0))
        before = get()
        iso.drop_privileges(NOBODY, NOBODY, [])
        return [lib.prctl(3, 0, 0, 0, 0), before, get()]

    dumpable, before, after = in_child(dumpable_and_keyring)["result"]
    assert dumpable == 0
    if os.uname().machine == "x86_64" and after > 0:
        assert after != before  # a fresh session keyring, not root's


@requires_root
def test_reset_warns_and_continues_on_a_planted_directory(world_tmp, checkout, area):
    """Opus #8: a directory under a known name never aborts the reset (which would leave the targets down)."""
    paths = area
    (paths.private("proxy") / "router.json").mkdir()
    (paths.private("fuzzer") / "divergence_registry.json").write_text("{}")
    paths.module_file.write_text("x = 1\n")
    owners = {"proxy": UIDS["proxy"], "fuzzer": UIDS["fuzzer"], "module": UIDS["translator"]}
    actions = iso.reset(paths, owners=owners)
    assert any(a.startswith("WARNING:") and "router.json" in a for a in actions)
    assert not (paths.private("fuzzer") / "divergence_registry.json").exists() and not paths.module_file.exists()


@requires_root
def test_disable_and_clean_remove_the_key_copies(tmp_path, world_tmp, checkout, area, monkeypatch):
    """Opus #9: API-key copies never outlive isolation."""
    paths = area
    marker = write_marker(tmp_path, service_home=str(paths.home))
    assert all(paths.key(n).exists() for n in AGENT_KEYS)
    actions = iso.disable(marker, running=lambda: {})
    assert not any(paths.key(n).exists() for n in AGENT_KEYS) and paths.home.exists()
    assert any("removed" in a and "translator.json" in a for a in actions)
    # clean runs whatever the mode, as long as the service area exists
    for comp in iso.COMPONENTS.values():
        iso.prepare(comp, ids_for(comp), paths, checkout / ".state")
    marker = write_marker(tmp_path, service_home=str(paths.home), state="setting-up")
    monkeypatch.setenv("STRANGLER_ISOLATION", "off")
    monkeypatch.setenv("STRANGLER_ISOLATION_MARKER", marker["STRANGLER_ISOLATION_MARKER"])
    monkeypatch.setattr(iso, "running_components", lambda uids=(): {})
    assert iso.clean_target() == paths
    assert iso.main(["clean"]) == 0
    assert os.listdir(paths.state) == []
    monkeypatch.setenv("STRANGLER_ISOLATION_MARKER", str(tmp_path / "none.json"))
    assert iso.clean_target() is None or iso.clean_target().home == iso.DEFAULT_SERVICE_HOME


def test_locale_allowlist_is_explicit():
    """Opus #10: the standard locale variables only, never an arbitrary LC_* name."""
    parent = {"LC_ALL": "C", "LC_CTYPE": "C.UTF-8", "LC_IDENTIFICATION": "C", "LC_SECRET_TOKEN": "x",
              "LC_ANYTHING": "y"}
    env = iso.child_env("legacy", parent, paths=iso.service_paths("/srv/sr"))
    assert {k for k in env if k.startswith("LC_")} == {"LC_ALL", "LC_CTYPE", "LC_IDENTIFICATION"}
    assert env["PYTHONSAFEPATH"] == "1"


@requires_root
def test_relative_mirror_links_are_resolved_not_normalised(world_tmp, checkout):
    """Opus #10: relative links are followed step by step; climbing above their tree is refused even when a
    lexical normalisation would have said otherwise, and an in-tree detour through a link is accepted."""
    venv = checkout / ".venv"
    (venv / "deep").symlink_to("lib/python3.12")  # an in-tree directory link
    (venv / "inside.py").symlink_to("deep/../../pyvenv.cfg")  # really venv/pyvenv.cfg (lexically: outside)
    paths = iso.service_paths(world_tmp / "srv")
    iso.ensure_service_home(paths)
    iso.sync_mirrors(checkout, paths)
    assert os.readlink(paths.venv / "inside.py") == "deep/../../pyvenv.cfg"
    (venv / "escape.py").symlink_to("deep/../../../outside.py")  # really above .venv (lexically: inside .venv?)
    with pytest.raises(iso.IsolationError, match="leaves"):
        iso.sync_mirrors(checkout, paths)


def _isolated_pid_harness(script: str, pid: int, name: str = "legacy") -> int:
    harness = "\n".join(["set -euo pipefail", f"id() {{ echo {NOBODY}; }}",  # stands in for `id -u sr-<name>`
                         _function(ROOT / script, "isolated_pid"), f"isolated_pid {pid} {name}"])
    return subprocess.run(["bash", "-c", harness], timeout=30).returncode


@requires_root
@pytest.mark.parametrize("script", ["scripts/targets.sh", "scripts/agents.sh"])
def test_the_scripts_real_isolated_pid_against_real_processes(script):
    """The real isolated_pid(): a process dropped to the user passes; root's, or the launcher's (whose argv still
    holds `-m agents.isolation`) does not."""
    sleep = [BASE_PYTHON, "-I", "-c", "import time; time.sleep(30)"]
    dropped = subprocess.Popen(sleep, user=NOBODY, group=NOBODY, extra_groups=[], cwd="/")
    launcher = subprocess.Popen([*sleep, "-m", "agents.isolation"], user=NOBODY, group=NOBODY, extra_groups=[], cwd="/")
    as_root = subprocess.Popen(sleep)
    try:
        time.sleep(0.3)
        assert _isolated_pid_harness(script, dropped.pid) == 0
        assert _isolated_pid_harness(script, launcher.pid) == 1
        assert _isolated_pid_harness(script, as_root.pid) == 1
        assert _isolated_pid_harness(script, 999999) == 1
    finally:
        for p in (dropped, launcher, as_root):
            p.kill()
            p.wait()


@requires_root
def test_config_checks_and_status_report_problems(tmp_path, world_tmp, checkout, area):
    paths = area
    env = {**write_marker(tmp_path, service_home=str(paths.home), state="setting-up"),
           "STRANGLER_STATE_DIR": str(checkout / ".state")}
    rows = iso.config_checks(paths, env)
    fails = {r.check for r in rows if r.status == "FAIL"}
    assert any("is ready and names this checkout" in c for c in fails)
    assert "isolation mode resolves to on" in fails
    if iso._lookup(iso.pwd.getpwnam, "sr-proxy") is None:
        assert any("system user" in c for c in fails)
    assert any(f"{paths.home}: trusted path" in r.check and r.status == "PASS" for r in rows)
    paths.state.chmod(0o777)
    rows = iso.config_checks(paths, env)
    assert any(r.status == "FAIL" and r.check.startswith(f"{paths.state} root:root 0755") for r in rows)
    paths.state.chmod(0o755)
    lines = "\n".join(iso.status_lines(env))
    assert "mode: error: isolation setup incomplete" in lines and "setting-up" in lines


def test_live_pid_trusts_only_a_live_process_running_the_module(tmp_path):
    env = {"STRANGLER_STATE_DIR": str(tmp_path)}
    run = tmp_path / "run"
    run.mkdir()
    assert iso.live_pid("proxy", env) is None
    for junk in ("", "garbage", "-1", "0", "999999"):
        (run / "proxy.pid").write_text(junk)
        assert iso.live_pid("proxy", env) is None
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    real = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m", "targets.proxy.router"])
    try:
        time.sleep(0.2)
        (run / "proxy.pid").write_text(f"{other.pid}\n")
        assert iso.live_pid("proxy", env) is None  # alive, but not the module
        (run / "proxy.pid").write_text(f"{real.pid}\n")
        assert iso.live_pid("proxy", env) == real.pid
    finally:
        for p in (other, real):
            p.kill()
            p.wait()


@requires_root
def test_verify_fails_cleanly_without_users_and_touches_nothing(tmp_path, world_tmp, checkout, area, monkeypatch):
    if iso._lookup(iso.pwd.getpwnam, "sr-proxy") is not None:
        pytest.skip("the service users exist on this host")
    paths = area
    env = {**write_marker(tmp_path, service_home=str(paths.home)), "STRANGLER_STATE_DIR": str(checkout / ".state")}
    before = {p: os.lstat(p).st_mtime_ns for p in (paths.state, paths.agents)}
    ok, rows = iso.verify(env, roots=[str(world_tmp / "nothing")])
    assert ok is False
    assert any(r.check == "resolve ids" and r.status == "FAIL" for r in rows)
    assert {p: os.lstat(p).st_mtime_ns for p in (paths.state, paths.agents)} == before
    assert "checks:" in iso.format_table(rows)


@requires_root
def test_isolated_svc_makes_an_older_log_private(fake_module):
    port = free_port()
    log = fake_module / "run" / "legacy.log"
    log.write_text("an older, unisolated run\n")
    log.chmod(0o644)
    assert _svc(fake_module, port, 0).returncode == 0
    assert stat.S_IMODE(log.stat().st_mode) == 0o600 and "older" not in log.read_text()


@pytest.mark.parametrize("script", ["targets.sh", "agents.sh"])
def test_scripts_stop_before_touching_the_run_dir_when_the_mode_is_misconfigured(tmp_path, script):
    """Behavioural companion of the structural test: with isolation on and no marker, the script exits 2 before it
    creates or locks anything."""
    state = tmp_path / "state"
    env = {**os.environ, "STRANGLER_ISOLATION": "on", "STRANGLER_ISOLATION_MARKER": str(tmp_path / "none.json"),
           "STRANGLER_STATE_DIR": str(state), "STRANGLER_RUN_DIR": str(state / "run")}
    proc = subprocess.run(["bash", str(ROOT / "scripts" / script), "status"], env=env, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 2 and "no marker" in proc.stderr, proc.stderr
    assert not state.exists()
