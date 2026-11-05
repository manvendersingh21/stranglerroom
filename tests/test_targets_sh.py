"""Regression tests for the service launcher in ``scripts/targets.sh``.

``scripts/targets.sh`` starts each target with its shell function ``svc()``.  The
old implementation backgrounded a *list*::

    (cd "$ROOT" && nohup "$PY" -m "$module" --port "$port" >"$RUN/$name.log" 2>&1 &
     echo $! >"$RUN/$name.pid")

Bash runs that backgrounded ``cd && nohup ...`` list in a wrapper bash subshell.
The redirections apply only to ``nohup``, so the wrapper keeps the caller's
stdout/stderr pipes open while it waits for the server forever.  A caller that
captures our output (``scripts/run_e2e_demo.py`` does
``subprocess.run([...targets.sh start --fresh], capture_output=True, timeout=300)``)
therefore never sees EOF and hangs until its timeout.  ``$!`` also names the
wrapper bash rather than the server.

The fixed launcher backgrounds only the fully redirected simple command (``9>&-``: the server must not
inherit the script's flock descriptor, or the lock would live as long as the server)::

    (
      cd "$ROOT"
      nohup "$PY" -m "$module" --port "$port" >"$RUN/$name.log" 2>&1 </dev/null 9>&- &
      echo $! >"$RUN/$name.pid"
    )

and fails fast if that server dies during start-up (it never adopts another process's listener). Health checks are
bounded (``curl -m``), so a target that accepts connections but never answers cannot wedge start-up.

``stop_one <name> <module> <port>`` signals only the pid recorded in ``<name>.pid`` and only while
``/proc/<pid>/cmdline`` still holds the pairs ``-m <module>`` and ``--port <port>`` (a stale pid file whose pid was
reused — even by the same module serving another port — is reported and removed, never signalled); it sends TERM
(also to uvicorn's reload worker), then KILL after 5 s. ``start --fresh`` runs ``ports_free 9000 9001 9002`` after
stopping and fails loud instead of reusing a target it does not track. start/stop hold ``flock -w 60`` on fd 9.

The tests extract these functions verbatim from the script under test and run them against a tiny fake server on
a free loopback port (never 9000-9002, never the real ``.state``) while capturing output. Set ``TARGETS_SH`` to run
them against a different copy of the script (used to prove that they catch the old idioms).
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.procs import free_port

ROOT = Path(__file__).resolve().parents[1]
SVC_RE = re.compile(r"^svc\(\) \{.*?^\}$", re.MULTILINE | re.DOTALL)

FAKE_SERVER_SOURCE = '''"""Minimal stand-in for a target service.

It accepts the same command line shape that ``svc()`` uses for real targets,
``-m <module> --port <port>``, ignores the module name, and serves
``GET /health`` on loopback until it is killed.
"""

import argparse
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeTarget/1.0"

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", dest="module", default=None)  # accepted and ignored
    parser.add_argument("--port", type=int, required=True)
    args, _unknown = parser.parse_known_args()
    if os.environ.get("FAKE_IGNORE_TERM") == "1":  # a target that ignores TERM (stop must escalate to KILL)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("fake target serving on 127.0.0.1:%d" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
'''


@dataclass(frozen=True)
class FakeTarget:
    """Paths and port belonging to one test's throwaway fake target."""

    tmp_path: Path
    run_dir: Path
    server: Path
    port: int


def _targets_script() -> Path:
    """Return the script selected by ``TARGETS_SH`` or the repository copy."""
    return Path(os.environ.get("TARGETS_SH") or ROOT / "scripts/targets.sh")


def _svc_function(script: Path) -> str:
    """Extract the complete ``svc()`` function text from *script*."""
    try:
        text = script.read_text(encoding="utf-8")
    except OSError as exc:
        pytest.fail(f"cannot read targets script {script}: {exc}")
    match = SVC_RE.search(text)
    if match is None:
        pytest.fail(f"could not find a complete svc() function in {script}")
    return match.group(0)


def _write_fake_server(tmp_path: Path) -> Path:
    fake = tmp_path / "fake_target_server.py"
    fake.write_text("#!" + sys.executable + "\n" + FAKE_SERVER_SOURCE, encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _pids_referencing(marker: bytes) -> list[int]:
    """Return pids whose ``/proc/<pid>/cmdline`` contains *marker*."""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker in cmdline:
            pids.append(pid)
    return pids


def _cleanup(tmp_path: Path) -> list[int]:
    """SIGKILL every process referring to this test's tmp_path; poll to confirm.

    The unique tmp_path appears in the cmdlines of both the fake server (via its
    script path) and, under the old idiom, the orphaned wrapper bash (via the
    harness text).  Returns any pids still present after a short grace period.
    """
    marker = str(tmp_path).encode()
    deadline = time.monotonic() + 5.0
    while True:
        pids = _pids_referencing(marker)
        if not pids:
            return []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, FileNotFoundError):
                pass
        if time.monotonic() >= deadline:
            return _pids_referencing(marker)
        time.sleep(0.05)


def _cmdline(pid: int) -> list[str]:
    return [
        part.decode("utf-8", "replace")
        for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if part
    ]


@pytest.fixture
def fake_target(tmp_path: Path) -> Iterator[FakeTarget]:
    """Create the fake server and remove every process it leaves behind.

    Teardown runs after both passing and failing tests.  It is deliberately a
    fixture rather than a ``try/finally`` in the test body: a failure raised in
    teardown is reported as an error without hiding the test's own failure, and
    it cannot be skipped by inspecting only the currently propagating exception,
    which cannot see the in-flight failure there.
    """
    run_dir = tmp_path / "run"
    target = FakeTarget(
        tmp_path=tmp_path,
        run_dir=run_dir,
        server=_write_fake_server(tmp_path),
        port=free_port(),
    )
    try:
        yield target
    finally:
        leftovers = _cleanup(tmp_path)
        if leftovers:
            pytest.fail(
                f"processes still reference this test's tmp_path after cleanup: {leftovers}"
            )


def test_svc_returns_promptly_with_captured_output_and_records_the_server_pid(
    fake_target: FakeTarget,
) -> None:
    script = _targets_script()
    svc = _svc_function(script)
    port = fake_target.port

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"ROOT={shlex.quote(str(fake_target.tmp_path))}",
            f"RUN={shlex.quote(str(fake_target.run_dir))}",
            'mkdir -p "$RUN"',
            f"PY={shlex.quote(str(fake_target.server))}",
            svc,
            f"svc fake fake.module {port} /health",
            "",
        ]
    )

    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            timeout=12,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "svc() kept the caller's stdout/stderr pipes open (the backgrounded "
            "wrapper subshell bug): captured subprocess.run() never saw EOF"
        )
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"fake up on :{port}" in proc.stdout, proc.stdout + proc.stderr
    assert elapsed < 10.0, f"svc() took {elapsed:.1f}s with captured output"

    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0, trust_env=False)
    assert response.status_code == 200
    assert response.text == "ok"

    pid_file = fake_target.run_dir / "fake.pid"
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    argv = _cmdline(pid)

    # The pid must name the server itself, not a wrapper bash.
    assert any("fake_target_server.py" in arg for arg in argv), argv
    assert "--port" in argv, argv
    assert str(port) in argv, argv
    assert not any(
        Path(arg).name in {"bash", "sh", "dash", "zsh"} for arg in argv[:2]
    ), argv

    log = fake_target.run_dir / "fake.log"
    for fd in ("1", "2"):
        target = Path(os.readlink(f"/proc/{pid}/fd/{fd}"))
        assert target == log or target.resolve() == log.resolve(), (fd, target)
    assert Path(os.readlink(f"/proc/{pid}/fd/0")).resolve() == Path(os.devnull).resolve()


# --------------------------------------------------------------------------- stop / ports / start-up hardening
# targets.sh only ever signals a pid from its own pid file after /proc/<pid>/cmdline proves it is still the expected
# `python -m <module>`, escalates TERM -> KILL, refuses to reuse an untracked listener on --fresh (ports_free), fails
# fast when its server dies during start-up, and never lets a server inherit its flock descriptor (fd 9).

def _functions(*names: str) -> str:
    """The complete text of the named shell functions of the script under test, in the given order."""
    text = _targets_script().read_text(encoding="utf-8")
    out = []
    for name in names:
        match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.MULTILINE | re.DOTALL)
        if match is None:
            pytest.fail(f"could not find a complete {name}() function in {_targets_script()}")
        out.append(match.group(0))
    return "\n".join(out)


def _bash(target: FakeTarget, *lines: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run ``lines`` after the targets.sh helpers, with ROOT/RUN/PY pointing at the fake target."""
    harness = "\n".join([
        "set -euo pipefail",
        f"ROOT={shlex.quote(str(target.tmp_path))}",
        f"RUN={shlex.quote(str(target.run_dir))}",
        'mkdir -p "$RUN"',
        f"PY={shlex.quote(str(target.server))}",
        _functions("is_target_pid", "port_open", "svc", "stop_one", "ports_free"),
        *lines, ""])
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=timeout,
                          start_new_session=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}").exists() and "zombie" not in _proc_state(pid)


def _proc_state(pid: int) -> str:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return "gone"
    return next((ln.split(":", 1)[1].strip().lower() for ln in status.splitlines() if ln.startswith("State:")), "")


def _started_pid(target: FakeTarget) -> int:
    proc = _bash(target, f"svc fake fake.module {target.port} /health")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return int((target.run_dir / "fake.pid").read_text().strip())


def test_stop_one_terminates_the_recorded_server(fake_target: FakeTarget) -> None:
    pid = _started_pid(fake_target)
    proc = _bash(fake_target, f"stop_one fake fake.module {fake_target.port}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not _alive(pid), f"server {pid} survived stop_one"
    assert not (fake_target.run_dir / "fake.pid").exists()
    assert _bash(fake_target, f"ports_free {fake_target.port}").returncode == 0


def test_stop_one_never_signals_a_stale_or_foreign_pid(fake_target: FakeTarget) -> None:
    # A reused pid: the pid file names a live process that is NOT `python -m fake.module`.
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        fake_target.run_dir.mkdir(parents=True, exist_ok=True)
        (fake_target.run_dir / "fake.pid").write_text(f"{bystander.pid}\n")
        proc = _bash(fake_target, f"stop_one fake fake.module {fake_target.port}")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert bystander.poll() is None, "stop_one signalled a process it did not start"
        assert "stale pid file" in proc.stderr and str(bystander.pid) in proc.stderr, proc.stderr
        assert not (fake_target.run_dir / "fake.pid").exists()
        # garbage / empty pid files are dropped silently or with a note, never acted on
        for junk in ("", "not-a-pid", "-1", "0", "00", "0 1"):  # 0 / -1 would mean process groups to kill
            proc = None
            (fake_target.run_dir / "fake.pid").write_text(junk)
            proc = _bash(fake_target, f"stop_one fake fake.module {fake_target.port}")
            assert proc.returncode == 0 and "No such file" not in proc.stderr, proc.stderr
            assert not (fake_target.run_dir / "fake.pid").exists()
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=5)


def test_stop_one_escalates_to_kill_when_term_is_ignored(fake_target: FakeTarget) -> None:
    proc = _bash(fake_target, "export FAKE_IGNORE_TERM=1", f"svc fake fake.module {fake_target.port} /health")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pid = int((fake_target.run_dir / "fake.pid").read_text().strip())
    started = time.monotonic()
    proc = _bash(fake_target, f"stop_one fake fake.module {fake_target.port}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sending KILL" in proc.stderr, proc.stderr
    assert not _alive(pid), f"server {pid} survived TERM+KILL"
    assert time.monotonic() - started < 12


def test_ports_free_names_every_busy_port(fake_target: FakeTarget) -> None:
    _started_pid(fake_target)
    idle = free_port()
    assert _bash(fake_target, f"ports_free {idle}").returncode == 0
    proc = _bash(fake_target, f"ports_free {idle} {fake_target.port}")
    assert proc.returncode != 0
    assert str(fake_target.port) in proc.stderr and str(idle) not in proc.stderr.split("port(s)")[1].split("still")[0]


def test_svc_fails_fast_when_its_server_dies_during_start_up(fake_target: FakeTarget) -> None:
    dying = fake_target.tmp_path / "dying_server.sh"
    dying.write_text("#!/bin/sh\necho 'cannot bind' >&2\nexit 3\n")
    dying.chmod(0o755)
    started = time.monotonic()
    proc = _bash(fake_target, f"PY={shlex.quote(str(dying))}", f"svc fake fake.module {fake_target.port} /health")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "exited during start-up" in proc.stderr and "cannot bind" in proc.stderr, proc.stderr
    assert not (fake_target.run_dir / "fake.pid").exists()
    assert time.monotonic() - started < 10


def test_svc_server_does_not_inherit_the_lock_descriptor(fake_target: FakeTarget) -> None:
    proc = _bash(fake_target, 'exec 9>"$RUN/.targets.lock"', "flock 9",
                 f"svc fake fake.module {fake_target.port} /health")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pid = int((fake_target.run_dir / "fake.pid").read_text().strip())
    assert not Path(f"/proc/{pid}/fd/9").exists(), "the server inherited the flock fd: the lock would never be freed"
    # ...so a second locker gets the lock at once although the server keeps running
    locker = subprocess.run(["flock", "-n", str(fake_target.run_dir / ".targets.lock"), "true"], timeout=10)
    assert locker.returncode == 0 and _alive(pid)


def test_stop_one_spares_the_same_module_serving_another_port(fake_target: FakeTarget) -> None:
    """A reused pid that runs the SAME module for another port (e.g. a unit test's modern target) is not ours."""
    pid = _started_pid(fake_target)
    other = free_port()
    proc = _bash(fake_target, f"stop_one fake fake.module {other}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _alive(pid), "stop_one signalled a server for another port"
    assert "stale pid file" in proc.stderr and f"--port {other}" in proc.stderr, proc.stderr


def test_svc_is_not_wedged_by_a_listener_that_never_answers(fake_target: FakeTarget) -> None:
    """A port held by something that accepts TCP but never answers HTTP: the "already up" probe times out, the
    launched server cannot bind and svc() fails fast — it neither hangs nor adopts the foreign listener."""
    import socket

    with socket.socket() as mute:
        mute.bind(("127.0.0.1", fake_target.port))
        mute.listen(16)  # never accept(): connections complete in the kernel backlog and then hang
        started = time.monotonic()
        proc = _bash(fake_target, f"svc fake fake.module {fake_target.port} /health", timeout=60)
        elapsed = time.monotonic() - started
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "exited during start-up" in proc.stderr, proc.stderr
    assert elapsed < 20, f"svc() took {elapsed:.1f}s against a mute listener"

