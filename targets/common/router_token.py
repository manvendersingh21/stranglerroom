"""The strangler proxy's admin token, shared by the proxy (server side) and @cutover-gate (client side).

ONE rule, applied identically by both processes (they must never disagree, or every flip and rollback is
refused with 403):

1. ``ROUTER_ADMIN_TOKEN`` in the environment (whitespace-trimmed) — tests, CI, explicit deployments;
2. otherwise the per-deployment random token in ``ROUTER_TOKEN_FILE``, or by default
   ``<STRANGLER_STATE_DIR or <repo>/.state>/router.token`` (mode 0600), generated on first use.

There is no public default secret, and creation is race-safe (a temp file hard-linked into place), so the proxy
and the agent may start in either order, and a crash mid-write leaves no empty token behind. The location does not
depend on ``ROUTER_STATE_FILE``. A token file is only used if it is a regular, non-symlinked file owned by this
user with no group/other access — so to use a mounted secret (often a symlink, 0644), pass it via
``ROUTER_ADMIN_TOKEN`` instead.
"""
from __future__ import annotations

import os
import secrets
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE_NAME = "router.token"


def token_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the per-deployment token lives (when no explicit token is configured)."""
    values = os.environ if env is None else env
    explicit = (values.get("ROUTER_TOKEN_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_dir = (values.get("STRANGLER_STATE_DIR") or "").strip() or str(ROOT / ".state")
    return Path(state_dir).expanduser() / TOKEN_FILE_NAME


def resolve_router_token(env: Mapping[str, str] | None = None) -> str:
    """Return the configured token, or the per-deployment token (creating it on first use)."""
    values = os.environ if env is None else env
    configured = (values.get("ROUTER_ADMIN_TOKEN") or "").strip()
    if configured:
        return configured
    path = token_path(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Publish atomically: write a private temp file, then hard-link it into place. link() fails if the
        # token already exists (another process won the race), and nobody ever sees a half-written file.
        fd, tmp = tempfile.mkstemp(prefix=".router.token.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(24) + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # durable before it becomes visible: no empty token after a power loss
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)
    return _read_private_token(path)


def _read_private_token(path: Path) -> str:
    """Read the token only from a regular, non-symlinked file owned by this user with no group/other access:
    a token anyone else can read would let them flip production routing."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # NONBLOCK: a FIFO must not hang us
    except OSError as exc:
        raise RuntimeError(f"cannot open router token {path} (symlinks are refused): {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"router token {path} is not a regular file")
        if st.st_uid != os.getuid():
            raise RuntimeError(f"router token {path} is owned by uid {st.st_uid}, not by this user")
        if st.st_mode & 0o077:
            raise RuntimeError(f"router token {path} is accessible to group/others (mode "
                               f"{oct(stat.S_IMODE(st.st_mode))}); `chmod 600` it or delete it to regenerate")
        token = os.read(fd, 4096).decode().strip()
    finally:
        os.close(fd)
    if not token:
        raise RuntimeError(f"{path} is empty — delete it to generate a new router token")
    return token
