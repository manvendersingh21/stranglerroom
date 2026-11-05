"""Contract checks for scripts/agents.sh and the Makefile (docs/AGENTS_SPEC.md §4).

scripts/agents.sh start|stop|status:
  * start: for each of translator, fuzzer, cutover-gate runs `$PYTHON -m agents.<module>` (modules: translator_agent,
    fuzzer_agent, cutover_agent) in the background with PYTHONPATH=$ROOT, logs to .state/run/<name>.log, pid in
    .state/run/<name>.pid; skips one that is already running (pid alive). PYTHON defaults to $ROOT/.venv/bin/python.
    AGENTS_MODULE_PREFIX (default "agents.") lets tests substitute modules.
  * stop: TERM each pid (then KILL after 5 s), remove pid files. Idempotent.
  * status: one line per agent "<name> running pid=<pid>" or "<name> stopped".
Makefile targets: install, switch-up, switch-down, targets, provision, agents, agents-stop, test, test-integration,
  e2e, demo, cinema, stop, clean-state. `make demo` = switch-up targets(fresh) provision agents e2e.
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, env=e, timeout=60)


def test_agents_sh_syntax():
    assert run("bash", "-n", "scripts/agents.sh").returncode == 0
    assert run("bash", "-n", "scripts/targets.sh").returncode == 0
    assert run("bash", "-n", "scripts/bootstrap_switch.sh").returncode == 0


def test_agents_sh_lifecycle(tmp_path):
    env = {"AGENTS_MODULE_PREFIX": "tests.fake_agent_", "STRANGLER_RUN_DIR": str(tmp_path)}
    r = run("bash", "scripts/agents.sh", "start", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        time.sleep(1.0)
        st = run("bash", "scripts/agents.sh", "status", env=env).stdout
        for name in ("translator", "fuzzer", "cutover-gate"):
            assert f"{name} running pid=" in st, st
            assert (tmp_path / f"{name}.pid").exists()
        r2 = run("bash", "scripts/agents.sh", "start", env=env)  # idempotent
        assert r2.returncode == 0
        assert len({(tmp_path / f"{n}.pid").read_text() for n in ("translator",)}) == 1
    finally:
        assert run("bash", "scripts/agents.sh", "stop", env=env).returncode == 0
    st = run("bash", "scripts/agents.sh", "status", env=env).stdout
    for name in ("translator", "fuzzer", "cutover-gate"):
        assert f"{name} stopped" in st, st
    assert run("bash", "scripts/agents.sh", "stop", env=env).returncode == 0


def test_makefile_targets():
    for target in ["install", "switch-up", "switch-down", "targets", "provision", "agents", "agents-stop", "test",
                   "test-integration", "e2e", "demo", "cinema", "stop", "clean-state"]:
        r = run("make", "-n", target)
        assert r.returncode == 0, (target, r.stderr)
    demo = run("make", "-n", "demo").stdout
    assert "bootstrap_switch.sh" in demo and "targets.sh start --fresh" in demo and "agents.provision" in demo
    assert "agents.sh start" in demo and "run_e2e_demo.py" in demo
    assert "--cinema" in run("make", "-n", "cinema").stdout
