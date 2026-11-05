"""Helpers to launch target services as subprocesses for tests."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"{url} not up after {timeout}s: {last}")


@contextmanager
def service(module: str, port: int, health: str = "/health", env: dict | None = None, args: list[str] | None = None):
    e = dict(os.environ)
    if not (env and "STRANGLER_STATE_DIR" in env):
        # A spawned target resolves its default files (the legacy log, the proxy's router.json and token) under the
        # state dir: never the real deployment's .state (conftest.py makes this a per-session temp dir).
        e["STRANGLER_STATE_DIR"] = os.environ["STRANGLER_TEST_SERVICE_STATE_DIR"]
    e.update(env or {})
    e["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen([sys.executable, "-m", module, "--port", str(port), *(args or [])], cwd=ROOT, env=e,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{port}{health}")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
