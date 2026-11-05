"""The production lock: one cross-process lock shared by @translator and @cutover-gate.

@translator holds it for a whole ``migrate`` / ``preserve … deprecate …`` (check that no cutover is active →
write the module → wait for the hot reload → record the build). @cutover-gate holds it for a whole
``approve-cutover`` / ``rollback`` (re-check the modern rev → flip the proxy → verify → record). The two
sequences are therefore mutually exclusive: however a human's commands interleave, production can never be
flipped onto a build that is being replaced, nor a new build written under a flip in progress.

It is an ``flock`` on ``STRANGLER_PRODUCTION_LOCK`` (default ``<STRANGLER_STATE_DIR or <repo>/.state>/
production.lock``) — the agents share one host in this deployment. There is exactly one lock, so no deadlock;
a holder that dies releases it with its file descriptor.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def production_lock_path() -> Path:
    explicit = (os.environ.get("STRANGLER_PRODUCTION_LOCK") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_dir = (os.environ.get("STRANGLER_STATE_DIR") or "").strip() or str(ROOT / ".state")
    return Path(state_dir).expanduser() / "production.lock"


@contextlib.contextmanager
def production_lock(*, timeout: float, path: Path | None = None) -> Iterator[None]:
    """Hold the production lock for the block; ``TimeoutError`` if it cannot be taken within ``timeout`` s."""
    lock_path = Path(path) if path is not None else production_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another production change holds {lock_path} (waited {timeout:.0f}s)")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock
