"""Extra contract checks for scripts/agents.sh and the Makefile (docs/AGENTS_SPEC.md §4).

Covers: restart, stale/crashed pid files, never signalling a foreign pid, log append + timestamped header,
usage errors, and `make -n <target>` for every target (plus the exact CLIs of targets.sh / bootstrap_switch.sh).
All agent processes are the tests.fake_agent_* stand-ins, run in a tmp STRANGLER_RUN_DIR.
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("translator", "fuzzer", "cutover-gate")
TARGETS = ["help", "install", "switch-up", "switch-down", "targets", "provision", "agents", "agents-stop", "test",
           "test-integration", "e2e", "demo", "cinema", "stop", "clean-state",
           "isolate", "isolate-verify", "isolate-off"]


def run(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, env=e, timeout=60)


def agents(cmd, env):
    return run("bash", "scripts/agents.sh", cmd, env=env)


def pid_of(run_dir, name):
    return int((run_dir / f"{name}.pid").read_text())


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill -0; treat it as dead
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def wait_dead(pid, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def env(tmp_path):
    e = {"AGENTS_MODULE_PREFIX": "tests.fake_agent_", "STRANGLER_RUN_DIR": str(tmp_path)}
    yield e
    agents("stop", e)


def test_restart_replaces_every_process(env, tmp_path):
    assert agents("start", env).returncode == 0
    time.sleep(0.5)
    old = {n: pid_of(tmp_path, n) for n in NAMES}
    r = agents("restart", env)
    assert r.returncode == 0, r.stdout + r.stderr
    time.sleep(0.5)
    st = agents("status", env).stdout
    for n in NAMES:
        new = pid_of(tmp_path, n)
        assert new != old[n]
        assert f"{n} running pid={new}" in st, st
        assert wait_dead(old[n]), f"old {n} pid {old[n]} survived restart"
        assert alive(new)


def test_restart_when_nothing_running_just_starts(env, tmp_path):
    assert agents("restart", env).returncode == 0
    time.sleep(0.3)
    assert all(f"{n} running pid=" in agents("status", env).stdout for n in NAMES)


def test_status_after_crash_is_stopped(env, tmp_path):
    assert agents("start", env).returncode == 0
    time.sleep(0.5)
    victim = pid_of(tmp_path, "fuzzer")
    os.kill(victim, signal.SIGKILL)
    assert wait_dead(victim)
    st = agents("status", env).stdout
    assert "fuzzer stopped" in st, st
    assert "translator running pid=" in st and "cutover-gate running pid=" in st, st
    # start heals only the crashed one: the survivors keep their pids
    keep = {n: pid_of(tmp_path, n) for n in ("translator", "cutover-gate")}
    assert agents("start", env).returncode == 0
    time.sleep(0.4)
    assert "fuzzer running pid=" in agents("status", env).stdout
    assert pid_of(tmp_path, "fuzzer") != victim
    assert {n: pid_of(tmp_path, n) for n in keep} == keep


def test_stale_pid_file_is_stopped_and_stop_removes_it(env, tmp_path):
    (tmp_path / "translator.pid").write_text("999999\n")
    assert "translator stopped" in agents("status", env).stdout
    assert agents("stop", env).returncode == 0
    assert not (tmp_path / "translator.pid").exists()


def test_never_signals_a_foreign_pid(env, tmp_path):
    """A live process that is not our agent (pid file points at it) must survive stop/restart/start."""
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (tmp_path / "translator.pid").write_text(f"{foreign.pid}\n")
        assert "translator stopped" in agents("status", env).stdout
        assert agents("stop", env).returncode == 0
        assert foreign.poll() is None, "stop killed a pid it did not start"
        (tmp_path / "translator.pid").write_text(f"{foreign.pid}\n")
        assert agents("start", env).returncode == 0
        time.sleep(0.4)
        assert foreign.poll() is None
        assert pid_of(tmp_path, "translator") != foreign.pid  # replaced by a freshly started agent
        assert "translator running pid=" in agents("status", env).stdout
        assert agents("restart", env).returncode == 0
        assert foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait()


def test_logs_append_with_timestamped_header_per_start(env, tmp_path):
    assert agents("start", env).returncode == 0
    time.sleep(0.3)
    assert agents("restart", env).returncode == 0
    time.sleep(0.3)
    for n in NAMES:
        text = (tmp_path / f"{n}.log").read_text()
        headers = [ln for ln in text.splitlines() if ln.startswith("===")]
        assert len(headers) == 2, text
        assert all(re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", h) and n in h for h in headers), headers


def test_usage_error_exit_2(env):
    for args in (["bogus"], []):
        r = run("bash", "scripts/agents.sh", *args, env=env)
        assert r.returncode == 2 and "usage" in r.stderr, (args, r.returncode, r.stderr)


def test_start_fails_loud_when_module_missing(env, tmp_path):
    e = dict(env, AGENTS_MODULE_PREFIX="tests.does_not_exist_")
    r = agents("start", e)
    assert r.returncode != 0
    assert "translator" in r.stderr and "log" in r.stderr.lower(), r.stderr
    assert "translator running" not in agents("status", e).stdout


@pytest.mark.parametrize("target", TARGETS)
def test_make_dry_run_every_target(target):
    r = run("make", "-n", target)
    assert r.returncode == 0, (target, r.stderr)
    assert r.stdout.strip() or target == "help", target


def test_default_goal_is_help_and_lists_targets():
    r = run("make")
    assert r.returncode == 0, r.stderr
    for t in TARGETS:
        assert t in r.stdout, (t, r.stdout)


def test_makefile_recipes_use_real_clis():
    assert "bootstrap_switch.sh up" in run("make", "-n", "switch-up").stdout
    assert "bootstrap_switch.sh stop" in run("make", "-n", "switch-down").stdout
    tg = run("make", "-n", "targets").stdout
    assert "targets.sh start" in tg and "--fresh" not in tg
    assert "agents.provision" in run("make", "-n", "provision").stdout
    assert "agents.sh start" in run("make", "-n", "agents").stdout
    assert "agents.sh stop" in run("make", "-n", "agents-stop").stdout
    stop = run("make", "-n", "stop").stdout
    assert stop.index("agents.sh stop") < stop.index("targets.sh stop") < stop.index("bootstrap_switch.sh stop")
    assert "-m 'not integration'" in run("make", "-n", "test").stdout
    assert "agents.isolation setup" in run("make", "-n", "isolate").stdout
    assert "agents.isolation verify" in run("make", "-n", "isolate-verify").stdout
    assert "agents.isolation disable" in run("make", "-n", "isolate-off").stdout
    assert "-m integration" in run("make", "-n", "test-integration").stdout
    e2e = run("make", "-n", "e2e").stdout
    assert "run_e2e_demo.py" in e2e and "--fresh" in e2e and "--cinema" not in e2e
    cin = run("make", "-n", "cinema").stdout
    assert "--cinema" in cin and "--fresh" in cin


def test_demo_order():
    out = run("make", "-n", "demo").stdout
    marks = ["bootstrap_switch.sh up", "targets.sh start --fresh", "agents.provision", "agents.sh start",
             "run_e2e_demo.py"]
    pos = [out.find(m) for m in marks]
    assert all(p >= 0 for p in pos), out
    assert pos == sorted(pos), out


def test_requirements_pinned():
    lines = [ln.strip() for ln in (ROOT / "requirements.txt").read_text().splitlines() if ln.strip()]
    assert lines and all("==" in ln for ln in lines), lines
    names = {re.split(r"[=\[]", ln)[0].lower() for ln in lines}
    assert {"fastapi", "uvicorn", "httpx", "hypothesis", "pytest", "web.py", "pydantic", "pyyaml",
            "httpx-sse"} <= names


def test_stop_escalates_to_kill_after_grace(tmp_path):
    e = {"AGENTS_MODULE_PREFIX": "tests.ops_stubborn_", "STRANGLER_RUN_DIR": str(tmp_path)}
    try:
        assert agents("start", e).returncode == 0
        time.sleep(0.5)
        pids = [pid_of(tmp_path, n) for n in NAMES]
        t0 = time.time()
        assert agents("stop", e).returncode == 0
        assert 4.0 < time.time() - t0 < 15.0  # TERM ignored -> one shared ~5 s grace, then KILL
        assert all(wait_dead(p) for p in pids)
        assert not list(tmp_path.glob("*.pid"))
    finally:
        agents("stop", e)


def test_foreign_process_mentioning_module_is_not_ours(env, tmp_path):
    """Only argv containing `-m <module>` counts; the module name merely appearing in other args does not."""
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--note",
                                "tests.fake_agent_translator_agent"])
    try:
        (tmp_path / "translator.pid").write_text(f"{foreign.pid}\n")
        assert "translator stopped" in agents("status", env).stdout
        assert agents("stop", env).returncode == 0
        assert foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait()


def test_concurrent_starts_do_not_duplicate_agents(env, tmp_path):
    procs = [subprocess.Popen(["bash", "scripts/agents.sh", "start"], cwd=ROOT, env={**os.environ, **env},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for p in procs:
        p.communicate(timeout=30)
        assert p.returncode == 0
    time.sleep(0.3)
    pids = {pid_of(tmp_path, n) for n in NAMES}
    assert len(pids) == 3 and all(alive(p) for p in pids)
    assert agents("stop", env).returncode == 0
    assert all(wait_dead(p) for p in pids)
    assert not [p for p in Path("/proc").glob("[0-9]*")
                if f"STRANGLER_RUN_DIR={tmp_path}".encode() in _environ(p)], "untracked duplicate agent left running"


def _environ(proc_dir):
    try:
        return (proc_dir / "environ").read_bytes()
    except OSError:
        return b""
