"""Per-component OS users ("isolation"): docs/ISOLATION.md sections 3-7 (revision 2).

    python -m agents.isolation mode [--run-dir DIR]          # prints on|off; exit 2 on misconfiguration
    python -m agents.isolation exec <component> -- <python> -m <module> [args…]
    python -m agents.isolation setup [--service-home DIR]    # make isolate        (root, idempotent)
    python -m agents.isolation disable [--purge]             # make isolate-off    (root)
    python -m agents.isolation verify                        # make isolate-verify (root; exit 0 iff all pass)
    python -m agents.isolation reset | clean | status

With isolation off ``exec`` is a plain ``execvp``: nothing changes. With it on, the launcher (root) refreshes the
root-owned mirrors in the service area, prepares the component's part of it, rewrites argv to the service
interpreter, builds the component's environment from its own schema, starts a new session, closes every inherited
descriptor, drops to the component's user and ``execve``s it, so the recorded pid is the component itself.

Security model (docs/ISOLATION.md §2.2): the operator's checkout, its ``.state`` and ``/root`` are never touched and
stay unreachable. Root only writes into directories it owns and has checked (no symlink, root-owned, not writable by
any service), and never writes inside a service-owned directory: such a directory is created empty and handed over.
The one exception, ``reset``, unlinks known names through a checked ``O_DIRECTORY|O_NOFOLLOW`` descriptor. Nothing
ever falls back to running unisolated.
"""
from __future__ import annotations

import ctypes
import errno
import grp
import json
import os
import pwd
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, NoReturn

LOCK_GROUP = "sr-prodlock"
DEFAULT_SERVICE_HOME = Path("/srv/stranglerroom")
DEFAULT_MARKER = Path("/etc/stranglerroom/isolation.json")
DEFAULT_SWITCH_URL = "http://127.0.0.1:18000"
NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")
NO_HOME = "/nonexistent"
MARKER_VERSION = 2
APP_TREES = ("agents", "targets", "docs")
#: never mirrored into app/: byte code, caches, and the checkout's own generated module
APP_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".hypothesis"})
APP_EXCLUDE_FILES = frozenset({"targets/modern_fastapi/app/lists.py"})
SCAN_ROOTS = ("/srv", "/tmp", "/var/tmp", "/dev/shm", "/etc", "/home", "/opt", "/var/log",
              "/var/lib/stranglerroom", "/root")
SCAN_MAX_BYTES = 8 * 1024 * 1024
#: shorter values would match all over the file system; they are reported as "not scanned", not searched
MIN_SECRET_LEN = 8


class IsolationError(RuntimeError):
    """Isolation is misconfigured or a safety check failed. Callers must never fall back to running unisolated."""


@dataclass(frozen=True)
class Component:
    name: str                  # translator | fuzzer | cutover-gate | proxy | legacy | modern
    user: str                  # "sr-" + name
    module: str                # the one module it may run, e.g. "agents.translator_agent"
    agent: bool                # API key + room.json copies, SWITCH_URL
    router_token: bool         # ROUTER_ADMIN_TOKEN
    production_lock: bool      # member of LOCK_GROUP
    module_writer: bool        # owns SERVICE_HOME/state/module
    env_keys: tuple[str, ...]  # component-specific pass-through names (section 4)

    @property
    def groups(self) -> tuple[str, ...]:
        """Supplementary groups."""
        return (LOCK_GROUP,) if self.production_lock else ()

    @property
    def uses_module(self) -> bool:
        """Receives MODERN_LISTS_PATH (the translator writes the module, the modern server serves it)."""
        return self.module_writer or self.name == "modern"


_AGENT_KEYS = ("STRANGLER_AGENT_PACE", "LEGACY_URL", "MODERN_URL", "PROXY_URL")

COMPONENTS: dict[str, Component] = {c.name: c for c in (
    Component("translator", "sr-translator", "agents.translator_agent", agent=True, router_token=False,
              production_lock=True, module_writer=True,
              env_keys=_AGENT_KEYS + ("STRANGLER_TRANSLATOR_MODE", "STRANGLER_LLM_CMD")),
    Component("fuzzer", "sr-fuzzer", "agents.fuzzer_agent", agent=True, router_token=False, production_lock=False,
              module_writer=False, env_keys=_AGENT_KEYS),
    Component("cutover-gate", "sr-cutover-gate", "agents.cutover_agent", agent=True, router_token=True,
              production_lock=True, module_writer=False, env_keys=_AGENT_KEYS),
    Component("proxy", "sr-proxy", "targets.proxy.router", agent=False, router_token=True, production_lock=False,
              module_writer=False, env_keys=("LEGACY_URL", "MODERN_URL")),
    Component("legacy", "sr-legacy", "targets.legacy_openlibrary.server", agent=False, router_token=False,
              production_lock=False, module_writer=False, env_keys=()),
    Component("modern", "sr-modern", "targets.modern_fastapi.server", agent=False, router_token=False,
              production_lock=False, module_writer=False, env_keys=("MODERN_NO_RELOAD",)),
)}


@dataclass(frozen=True)
class Ids:
    """Resolved numeric ids for one component."""

    uid: int
    gid: int                                   # primary group (named after the user)
    groups: tuple[int, ...] = ()               # supplementary gids (LOCK_GROUP for translator and gate)
    lock_gid: int | None = None                # LOCK_GROUP, when the component takes the production lock
    module_owner: tuple[int, int] | None = None  # the translator's (uid, gid), for translator and modern


@dataclass(frozen=True)
class ServicePaths:
    home: Path

    @property
    def app(self) -> Path:
        return self.home / "app"

    @property
    def venv(self) -> Path:
        return self.home / "venv"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def state(self) -> Path:
        return self.home / "state"

    @property
    def agents(self) -> Path:
        return self.state / "agents"

    @property
    def room(self) -> Path:
        return self.state / "room.json"

    @property
    def module(self) -> Path:
        return self.state / "module"

    @property
    def module_file(self) -> Path:
        return self.module / "lists.py"

    @property
    def lock(self) -> Path:
        return self.state / "production.lock"

    @property
    def sync_lock(self) -> Path:
        return self.home / ".sync.lock"

    def private(self, component: Component | str) -> Path:
        return self.state / _component(component).name

    def key(self, component: Component | str) -> Path:
        return self.agents / f"{_component(component).name}.json"


def service_paths(service_home: Path | str) -> ServicePaths:
    home = Path(service_home)
    if not home.is_absolute() or home == Path("/"):
        raise IsolationError(f"the service home must be an absolute directory below /, not {service_home!r}")
    return ServicePaths(Path(os.path.normpath(home)))


# ----------------------------------------------------------------------------------------------------- paths

def _values(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _component(component: Component | str) -> Component:
    if isinstance(component, Component):
        return component
    try:
        return COMPONENTS[component]
    except KeyError:
        raise IsolationError(f"unknown component {component!r} (expected one of {', '.join(COMPONENTS)})") from None


def _abs(raw: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(raw)))


def repo_root() -> Path:
    """The resolved root of this checkout."""
    return Path(__file__).resolve().parents[1]


def repo_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """The checkout's state directory: ``STRANGLER_STATE_DIR`` (of the launcher; relative to the checkout, where the
    launchers run everything) or ``<repo>/.state``."""
    raw = (_values(env).get("STRANGLER_STATE_DIR") or "").strip()
    return Path(os.path.normpath(repo_root() / os.path.expanduser(raw))) if raw else repo_root() / ".state"


def router_token_path(env: Mapping[str, str] | None = None) -> Path:
    """Same rule as targets/common/router_token.py (``ROUTER_TOKEN_FILE`` or <state>/router.token)."""
    raw = (_values(env).get("ROUTER_TOKEN_FILE") or "").strip()
    return _abs(raw) if raw else repo_state_dir(env) / "router.token"


def marker_path(env: Mapping[str, str] | None = None) -> Path:
    raw = (_values(env).get("STRANGLER_ISOLATION_MARKER") or "").strip()
    return _abs(raw) if raw else DEFAULT_MARKER


def run_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    """Where the launchers keep pid files and logs: ``<state>/run`` (targets.sh, and agents.sh by default) and
    ``STRANGLER_RUN_DIR`` (agents.sh) if set."""
    dirs = [repo_state_dir(env) / "run"]
    raw = (_values(env).get("STRANGLER_RUN_DIR") or "").strip()
    if raw:
        extra = Path(os.path.normpath(raw if os.path.isabs(raw) else repo_root() / raw))
        if extra not in dirs:
            dirs.append(extra)
    return dirs


def run_dir_for(component: Component | str, env: Mapping[str, str] | None = None) -> Path:
    dirs = run_dirs(env)
    return dirs[-1] if _component(component).agent else dirs[0]


# --------------------------------------------------------------------------------------------- trusted paths

def _check_dir(st: os.stat_result, part: Path, path: Path, *, last: bool, owners: tuple[int, ...],
               traversable: bool) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise IsolationError(f"{part} is a symlink or not a directory (on the path to {path})")
    if st.st_uid not in owners:
        raise IsolationError(f"{part} is owned by uid {st.st_uid}, not root (on the path to {path})")
    if st.st_mode & 0o022 and not (st.st_mode & stat.S_ISVTX and not last):
        raise IsolationError(f"{part} is group/other-writable (mode {stat.S_IMODE(st.st_mode):04o}; on the path "
                             f"to {path})")
    if traversable and not st.st_mode & 0o001:
        raise IsolationError(f"{part} is not searchable by others (mode {stat.S_IMODE(st.st_mode):04o}), so the "
                             f"service users cannot reach {path}")


def open_trusted_dir(path: Path, *, create_mode: int | None = None, owners: tuple[int, ...] = (0,),
                     traversable: bool = False) -> int | None:
    """Open ``path`` one component at a time from /, each with O_DIRECTORY|O_NOFOLLOW relative to its parent's
    descriptor, and fstat-check each before descending: a real directory owned by root (``owners``) and not writable
    by group or others, except a sticky directory such as /tmp that is not the last component (its next component
    must then be root's too, so nobody else can rename it away). No part of the path can be swapped by a service.
    ``traversable``: others may also search every component. A missing component is created (``create_mode``,
    relative to the checked parent) or, without ``create_mode``, makes this return None. Returns the descriptor."""
    path = Path(os.path.normpath(path))
    if not path.is_absolute():
        raise IsolationError(f"{path} is not an absolute path")
    fd = os.open("/", _DIR_FLAGS)
    try:
        parts = path.parts[1:]
        _check_dir(os.fstat(fd), Path("/"), path, last=not parts, owners=owners, traversable=traversable)
        current = Path("/")
        for i, name in enumerate(parts):
            current = current / name
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if create_mode is None:
                    os.close(fd)
                    return None
                try:
                    os.mkdir(name, create_mode, dir_fd=fd)
                except FileExistsError:
                    pass
                child = _openat_dir(fd, name, current)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise IsolationError(f"{current} is a symlink or not a directory (on the path to {path})") from exc
                raise
            os.close(fd)
            fd = child
            _check_dir(os.fstat(fd), current, path, last=i == len(parts) - 1, owners=owners,
                       traversable=traversable)
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_trusted_path(path: Path, *, traversable: bool = False, allow_missing: bool = False) -> None:
    """``open_trusted_dir`` without keeping the descriptor. ``allow_missing``: a missing tail is fine."""
    fd = open_trusted_dir(path, traversable=traversable)
    if fd is None:
        if allow_missing:
            return
        raise IsolationError(f"{path} does not exist")
    os.close(fd)


_RUN_DIR_ENTRY = re.compile(r"^(?:[A-Za-z0-9_-]+\.(?:pid|log)|\.lock|\.targets\.lock)$")


def ensure_run_dir(path: Path) -> list[str]:
    """The launchers' pid/log directory when isolated: on a trusted path, root-owned, 0700 (created if missing). An
    existing directory is tightened to 0700 only if it holds nothing but launcher files (pid files, logs, locks), so
    a mistaken ``STRANGLER_RUN_DIR=/var/log`` is refused instead of locked down."""
    path = Path(os.path.normpath(path))
    actions = []
    if not os.path.lexists(path):
        actions.append(f"mkdir {path} (and any missing parent, 0700)")
    fd = open_trusted_dir(path, create_mode=0o700)  # created and checked descriptor by descriptor
    assert fd is not None
    try:
        st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != 0o700:
            foreign = sorted(n for n in os.listdir(fd) if not _RUN_DIR_ENTRY.match(n))
            if foreign:
                raise IsolationError(f"run directory {path} is {stat.S_IMODE(st.st_mode):04o} and holds more than "
                                     f"launcher files ({', '.join(foreign[:5])}): not making it 0700; point "
                                     "STRANGLER_RUN_DIR at a directory of its own")
            os.fchmod(fd, 0o700)
            actions.append(f"chmod {path} {stat.S_IMODE(st.st_mode):04o} -> 0700")
    finally:
        os.close(fd)
    return actions


# ------------------------------------------------------------------------------------------------ mode (§3)

def _require_trusted(st: os.stat_result, what: str) -> None:
    """A file that decides whether isolation is on must be writable only by root (or by the caller itself)."""
    if st.st_uid not in (0, os.geteuid()):
        raise IsolationError(f"{what} is owned by uid {st.st_uid}; only root may own it")
    if st.st_mode & 0o022:
        raise IsolationError(f"{what} is group/other-writable (mode {stat.S_IMODE(st.st_mode):04o}); refusing to "
                             "trust it")


def _read_small(fd: int, limit: int = 1 << 20) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise IsolationError(f"file larger than {limit} bytes")


def load_marker(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The marker written by ``setup``, or None if there is none. A marker that exists but is not a trusted,
    well-formed JSON object raises: it may be ours, so it is never silently ignored. The marker's directory chain
    is checked component by component first (``open_trusted_dir``; root or the caller itself may own each part):
    an ancestor a service could rename is an error even when the marker is missing, so "no marker" (off) can never
    be produced by a service moving the marker's directory away."""
    path = marker_path(env)
    dfd = open_trusted_dir(path.parent, owners=(0, os.geteuid()))
    if dfd is None:
        return None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IsolationError(f"cannot open the isolation marker {path} (symlinks are refused): {exc}") from exc
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IsolationError(f"isolation marker {path} is not a regular file")
        _require_trusted(st, f"isolation marker {path}")
        data = _read_small(fd)
    finally:
        os.close(fd)
    try:
        marker = json.loads(data)
    except ValueError as exc:
        raise IsolationError(f"isolation marker {path} is not valid JSON: {exc}") from exc
    if not isinstance(marker, dict) or not isinstance(marker.get("repo"), str):
        raise IsolationError(f"isolation marker {path} has no \"repo\" string")
    if marker.get("state") not in ("setting-up", "ready"):
        raise IsolationError(f"isolation marker {path} has an unknown state {marker.get('state')!r}")
    if not isinstance(marker.get("service_home"), str) or not os.path.isabs(marker["service_home"]):
        raise IsolationError(f"isolation marker {path} has no absolute \"service_home\"")
    return marker


def _same_repo(repo: str) -> bool:
    return os.path.realpath(repo) == str(repo_root())


INCOMPLETE = "isolation setup incomplete: re-run `make isolate` or `make isolate-off`"


def isolation_on(env: Mapping[str, str] | None = None) -> bool:
    """Mode resolution (§3): ``STRANGLER_ISOLATION`` = off | on | auto (default auto)."""
    values = _values(env)
    mode = (values.get("STRANGLER_ISOLATION") or "auto").strip().lower()
    if mode == "off":
        return False
    if mode not in ("on", "auto"):
        raise IsolationError(f"STRANGLER_ISOLATION must be off, on or auto, not {mode!r}")
    marker = load_marker(values)
    where = marker_path(values)
    if marker is None or not _same_repo(marker["repo"]):
        if mode == "on":
            whose = "there is no marker" if marker is None else f"the marker belongs to {marker['repo']}"
            raise IsolationError(f"STRANGLER_ISOLATION=on but {whose} at {where} for this checkout ({repo_root()}): "
                                 "run `make isolate` (as root) first")
        return False
    if marker["state"] != "ready":
        raise IsolationError(f"{INCOMPLETE} (marker {where} is {marker['state']!r})")
    return True


def _ready_paths(values: Mapping[str, str]) -> ServicePaths:
    marker = load_marker(values)
    if marker is None:
        raise IsolationError(f"no isolation marker at {marker_path(values)}")
    return service_paths(marker["service_home"])


# ------------------------------------------------------------------------------------------- environment (§4.4)

_LOCALE_KEYS = ("LC_ALL", "LC_CTYPE", "LC_NUMERIC", "LC_TIME", "LC_COLLATE", "LC_MONETARY", "LC_MESSAGES",
                "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE", "LC_MEASUREMENT", "LC_IDENTIFICATION")
_COMMON_KEYS = frozenset({"PATH", "LANG", "LANGUAGE", "TZ", "PYTHONUNBUFFERED", "STRANGLER_LOG_LEVEL",
                          *_LOCALE_KEYS})
_LAUNCHER_SET = frozenset({"STRANGLER_STATE_DIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH", "HOME",
                           "USER", "LOGNAME", "MODERN_LISTS_PATH", "ROUTER_ADMIN_TOKEN", "SWITCH_URL"})
#: URLs a component dials: a host name such as localhost may resolve to ::1 first, where any service user can listen
#: if the real server binds 127.0.0.1 only (and collect bearer keys). Only IP literals are accepted.
_URL_KEYS = ("SWITCH_URL", "PROXY_URL", "LEGACY_URL", "MODERN_URL")
#: never forwarded by STRANGLER_TRANSLATOR_PASS_ENV: the router token and the operator's switch.env / Switch .env
#: secrets (pass-through is for LLM credentials), loader and interpreter knobs
_FORBIDDEN = frozenset({
    "ROUTER_ADMIN_TOKEN", "ROUTER_TOKEN_FILE", "AGENT_REGISTRATION_TOKEN", "GATEWAY_ADMIN_EMAIL",
    "GATEWAY_ADMIN_PASSWORD", "MATTERMOST_USER_PASSWORD", "MATTERMOST_ADMIN_PASSWORD", "DB_PASSWORD",
    "DB_OWNER_PASSWORD", "SECRET_KEYS", "JWT_SECRET_KEY", "CONTROLLER_TOKEN_SECRET", "MM_SQLSETTINGS_DATASOURCE",
    "PGPASSWORD", "STRANGLER_TRANSLATOR_PASS_ENV"})
_FORBIDDEN_PREFIXES = ("LD_", "PYTHON", "STRANGLER_ISOLATION")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pass_names(parent_env: Mapping[str, str]) -> list[str]:
    names = []
    for name in (parent_env.get("STRANGLER_TRANSLATOR_PASS_ENV") or "").split(","):
        name = name.strip()
        if not name:
            continue
        if (not _ENV_NAME.match(name) or name in _LAUNCHER_SET or name in _FORBIDDEN
                or name.startswith(_FORBIDDEN_PREFIXES)):
            raise IsolationError(f"STRANGLER_TRANSLATOR_PASS_ENV may not forward {name!r}")
        names.append(name)
    return names


def child_env(component: Component | str, parent_env: Mapping[str, str], *, paths: ServicePaths,
              router_token: str | None = None, switch_url: str | None = None) -> dict[str, str]:
    """The component's whole environment, from its own schema (§4.4); launcher-set keys are applied last."""
    comp = _component(component)
    env: dict[str, str] = {}
    for key, value in parent_env.items():
        if key in _COMMON_KEYS or key in comp.env_keys:
            env[key] = value
    if comp.name == "translator":
        for name in _pass_names(parent_env):
            if name in parent_env:
                env[name] = parent_env[name]
    env["STRANGLER_STATE_DIR"] = str(paths.state)
    env["PYTHONPATH"] = str(paths.app)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONSAFEPATH"] = "1"  # no implicit cwd / script directory on sys.path: only app/ (PYTHONPATH)
    env["HOME"] = str(paths.private(comp))
    env["USER"] = env["LOGNAME"] = comp.user
    if comp.uses_module:
        env["MODERN_LISTS_PATH"] = str(paths.module_file)
    if comp.agent:
        url = (switch_url or "").strip()
        if not url:
            raise IsolationError(f"{comp.name} is an agent and needs SWITCH_URL")
        env["SWITCH_URL"] = url
    if comp.router_token:
        token = (router_token or "").strip()
        if not token:
            raise IsolationError(f"{comp.name} needs the router token")
        env["ROUTER_ADMIN_TOKEN"] = token
    else:
        env.pop("ROUTER_ADMIN_TOKEN", None)
    env.pop("ROUTER_TOKEN_FILE", None)
    for key in _URL_KEYS:
        if key in env:
            check_ip_url(key, env[key])
    return env


def check_ip_url(key: str, url: str) -> None:
    """``url`` must name its host as an IP literal (loopback or remote), never a name such as ``localhost``: a name
    can resolve to an address (e.g. ::1) where the real server does not listen but any service user may."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url.strip()).hostname or ""
        ipaddress.ip_address(host)
    except ValueError:
        raise IsolationError(f"{key}={url!r}: use an IP literal host (e.g. http://127.0.0.1:18000), not a name: "
                             "localhost may resolve to ::1, where any service user could listen and collect "
                             "credentials") from None


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file (``export`` prefixes allowed); missing file -> {}; a symlink is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise IsolationError(f"cannot read {path} (symlinks are refused): {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IsolationError(f"{path} is not a regular file")
        text = _read_small(fd).decode("utf-8", "replace")
    finally:
        os.close(fd)
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def resolve_switch_url(env: Mapping[str, str] | None = None) -> str:
    """``SWITCH_URL`` from the environment, else from the checkout's public connection.env (never switch.env), else
    the default: the rule of ``Settings.load_agent()``."""
    values = _values(env)
    url = (values.get("SWITCH_URL") or "").strip()
    if not url:
        url = (_read_env_file(repo_state_dir(values) / "connection.env").get("SWITCH_URL") or "").strip()
    return (url or DEFAULT_SWITCH_URL).rstrip("/")


def service_argv(component: Component | str, argv: Sequence[str], paths: ServicePaths) -> list[str]:
    """``[<checkout python>, "-m", <module>, …]`` -> ``[SERVICE_HOME/venv/bin/python, "-m", <module>, …]``. Any other
    shape, or another module than the component's own, is refused. The checkout interpreter is discarded."""
    comp = _component(component)
    argv = list(argv)
    if len(argv) < 3 or argv[1] != "-m":
        raise IsolationError(f"exec {comp.name}: expected `<python> -m {comp.module} [args…]`, got {argv!r}")
    if argv[2] != comp.module:
        raise IsolationError(f"exec {comp.name}: may only run {comp.module}, not {argv[2]!r}")
    return [str(paths.python), "-m", comp.module, *argv[3:]]


# ------------------------------------------------------------------------------------------ file helpers

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_DUMPABLE = 4


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _openat_dir(dir_fd: int | None, name: str | Path, label: str | Path) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise IsolationError(f"{label} is a symlink or not a directory: refusing to follow it") from exc
        raise


def _root_dir_at(parent_fd: int, name: str, label: Path, mode: int, actions: list[str]) -> int:
    """Open (creating if missing) a root-owned directory that only root may write; returns its descriptor.
    Refused if it is a symlink or owned by anyone but root; its mode is set to ``mode``."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        actions.append(f"mkdir {label}")
    except FileExistsError:
        pass
    fd = _openat_dir(parent_fd, name, label)
    try:
        st = os.fstat(fd)
        if st.st_uid != 0:
            raise IsolationError(f"{label} is owned by uid {st.st_uid}, not root: refusing to write into it")
        if st.st_gid != 0:
            os.fchown(fd, 0, 0)
        if stat.S_IMODE(st.st_mode) != mode:
            os.fchmod(fd, mode)
            if not created:
                actions.append(f"chmod {label} {stat.S_IMODE(st.st_mode):04o} -> {mode:04o}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _service_dir_at(parent_fd: int, name: str, label: Path, uid: int, gid: int, mode: int,
                    actions: list[str]) -> None:
    """A service-owned directory: created empty by root inside a root-only directory and handed over at once. An
    existing one must already belong to ``uid`` (or be an empty root-owned leftover of an interrupted hand-over):
    root never repairs anything inside it. Its own mode is restored through the no-follow descriptor (a service that
    opened it to others, e.g. ``chmod 0777 module``, must not keep it open across launches)."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    fd = _openat_dir(parent_fd, name, label)
    try:
        st = os.fstat(fd)
        if st.st_uid == uid:
            if stat.S_IMODE(st.st_mode) != mode:
                os.fchmod(fd, mode)
                actions.append(f"chmod {label} {stat.S_IMODE(st.st_mode):04o} -> {mode:04o}")
            return
        if st.st_uid == 0 and not os.listdir(fd):
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            actions.append(f"{'created' if created else 'handed over'} {label} ({uid}:{gid} {mode:04o})")
            return
        raise IsolationError(f"{label} is owned by uid {st.st_uid}, not {uid}: refusing (no ownership repair; "
                             "`python -m agents.isolation clean` empties the service state)")
    finally:
        os.close(fd)


def _read_source(path: Path) -> bytes | None:
    """A root-owned regular file of the checkout (read as root), or None if it does not exist."""
    try:
        fd = os.open(path, _FILE_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IsolationError(f"cannot read {path} (symlinks are refused): {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IsolationError(f"{path} is not a regular file")
        return _read_small(fd)
    finally:
        os.close(fd)


def _same_file(dir_fd: int, name: str, data: bytes, uid: int, gid: int, mode: int) -> bool:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or (st.st_uid, st.st_gid) != (uid, gid)
                or stat.S_IMODE(st.st_mode) != mode or st.st_size != len(data)):
            return False
        return _read_small(fd, limit=len(data) + 1) == data
    finally:
        os.close(fd)


def _install_copy(dir_fd: int, name: str, label: Path, data: bytes, uid: int, gid: int, mode: int,
                  actions: list[str]) -> None:
    """Publish ``data`` as ``name`` in a root-only directory: mkstemp-style exclusive temp file, fchown, fchmod,
    fsync, rename. Skipped if the file is already identical."""
    if _same_file(dir_fd, name, data, uid, gid, mode):
        return
    tmp = f".{name}.{os.urandom(6).hex()}.tmp"
    fd = os.open(tmp, _NEW_FILE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    actions.append(f"installed {label} ({uid}:{gid} {mode:04o})")


def _unlink_at(dir_fd: int, name: str, label: Path, actions: list[str]) -> None:
    """unlink never follows the final component: a symlink is removed itself."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        raise IsolationError(f"{label} is a directory, expected a file")
    os.unlink(name, dir_fd=dir_fd)
    actions.append(f"removed {label}")


@contextmanager
def _open_home(paths: ServicePaths, *, create: bool = False) -> Iterator[int]:
    """SERVICE_HOME's descriptor, after checking that no service can swap any part of its path."""
    check_trusted_path(paths.home, traversable=True, allow_missing=create)
    if create and not os.path.lexists(paths.home):
        check_trusted_path(paths.home.parent, traversable=True)
        os.mkdir(paths.home, 0o755)
    fd = _openat_dir(None, paths.home, paths.home)
    try:
        st = os.fstat(fd)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise IsolationError(f"{paths.home} must be owned by root and not group/other-writable")
        if stat.S_IMODE(st.st_mode) != 0o755 or st.st_gid != 0:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o755)
        yield fd
    finally:
        os.close(fd)


def ensure_service_home(paths: ServicePaths) -> list[str]:
    """Create SERVICE_HOME (root:root 0755) and its root-owned state/ and state/agents/ if missing."""
    actions: list[str] = []
    existed = os.path.lexists(paths.home)
    with _open_home(paths, create=True) as home_fd:
        if not existed:
            actions.append(f"mkdir {paths.home}")
        state_fd = _root_dir_at(home_fd, "state", paths.state, 0o755, actions)
        try:
            os.close(_root_dir_at(state_fd, "agents", paths.agents, 0o711, actions))
        finally:
            os.close(state_fd)
    return actions


@contextmanager
def _sync_locked(home_fd: int) -> Iterator[None]:
    """Serialise mirror refreshes and preparation (SERVICE_HOME/.sync.lock, root 0600)."""
    import fcntl

    fd = os.open(".sync.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=home_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
            raise IsolationError("SERVICE_HOME/.sync.lock is not a root-owned regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------- mirrors (§2.2)

def _mirror_mode(st: os.stat_result) -> int:
    """Root-owned copies: keep the execute bits, drop every write bit but the owner's and all set-id/sticky bits."""
    return stat.S_IMODE(st.st_mode) & 0o755 | 0o600 if stat.S_ISREG(st.st_mode) else 0o755


def _copy_file(src: Path, src_st: os.stat_result, dir_fd: int, name: str) -> None:
    mode = _mirror_mode(src_st)
    sfd = os.open(src, _FILE_FLAGS)
    try:
        if (os.fstat(sfd).st_ino, os.fstat(sfd).st_dev) != (src_st.st_ino, src_st.st_dev):
            raise IsolationError(f"{src} changed while mirroring it")
        tmp = f".{name}.{os.urandom(6).hex()}.sync"
        dfd = os.open(tmp, _NEW_FILE_FLAGS, 0o600, dir_fd=dir_fd)
        try:
            try:
                offset = 0
                while offset < src_st.st_size:
                    sent = os.sendfile(dfd, sfd, offset, src_st.st_size - offset)
                    if sent == 0:
                        break
                    offset += sent
                os.fchmod(dfd, mode)
                os.utime(dfd, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
            finally:
                os.close(dfd)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(sfd)


def _remove_any(dir_fd: int, name: str) -> None:
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)  # fd-based, never follows symlinks
    else:
        os.unlink(name, dir_fd=dir_fd)


def _check_mirrored_link(top: Path, src_path: Path, target: str) -> None:
    """A symlink copied into a mirror must not let a service influence code: a relative one must stay inside its
    tree (it is copied verbatim, so it resolves to the mirrored copy), an absolute one must lead to a path that only
    root can modify (e.g. bin/python -> /usr/bin/python3.12)."""
    if not os.path.isabs(target):
        _check_relative_link(top, src_path, target)
        return
    try:
        check_link_target(target, Path("/"))
    except IsolationError as exc:
        raise IsolationError(f"{src_path} -> {target}: {exc}; refusing to mirror it") from exc


MAX_LINK_DEPTH = 40


def _check_relative_link(top: Path, src_path: Path, target: str) -> None:
    """A relative link is copied verbatim, so it must resolve inside its tree: resolved step by step (following
    the links it meets, not lexically), it may never climb above ``top``. Once it meets an absolute link, the rest
    of the resolution is outside the mirror and must pass ``check_link_target``."""
    inside = Path(os.path.realpath(top))
    hops = [0]

    def refuse(why: str) -> IsolationError:
        return IsolationError(f"{src_path} -> {target}: {why}; refusing to mirror it")

    def resolve(text: str, current: Path) -> Path | None:
        parts = [p for p in text.split("/") if p not in ("", ".")]
        for i, name in enumerate(parts):
            last = i == len(parts) - 1
            if name == "..":
                if current == inside:
                    raise refuse(f"it leaves {top}")
                current = current.parent
                continue
            path = current / name
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                return None  # dangling inside the tree: the mirrored copy dangles the same way
            if stat.S_ISLNK(st.st_mode):
                hops[0] += 1
                if hops[0] > MAX_LINK_DEPTH:
                    raise refuse(f"more than {MAX_LINK_DEPTH} symlinks")
                inner = os.readlink(path)
                if os.path.isabs(inner):
                    try:
                        check_link_target("/".join([inner.rstrip("/"), *parts[i + 1:]]), Path("/"))
                    except IsolationError as exc:
                        raise refuse(str(exc)) from exc
                    return None
                nxt = resolve(inner, current)
                if nxt is None:
                    return None
                current = nxt
            elif stat.S_ISDIR(st.st_mode):
                current = path
            elif last:
                return path
            else:
                raise refuse(f"{path} is not a directory")
        return current

    start = Path(os.path.realpath(src_path.parent))
    if start != inside and inside not in start.parents:
        raise refuse(f"it is not inside {top}")
    resolve(target, start)


def check_link_target(target: str, base: Path) -> Path:
    """Resolve a symlink's ``target`` (relative ones from ``base``, the directory holding the link) as the kernel
    would, and require that only root can change any step of that resolution: every directory traversed is root's
    and not writable by others (a sticky one only as a pass-through to a root-owned entry), every symlink followed is
    root's and is itself resolved the same way (up to MAX_LINK_DEPTH links), the final file, if present, is root's
    and not writable by others, and a missing component is accepted only in a directory nobody else can write.
    Mutable indirection is refused even if it currently points somewhere trustworthy. Returns the resolved path."""
    check_trusted_path(base)
    hops = [0]

    def trusted(st: os.stat_result, path: Path, *, last: bool) -> None:
        if st.st_uid != 0:
            raise IsolationError(f"{path} is owned by uid {st.st_uid}, not root")
        if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022 and not (
                stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX and not last):
            raise IsolationError(f"{path} is group/other-writable (mode {stat.S_IMODE(st.st_mode):04o})")

    def resolve(text: str, current: Path) -> Path:
        if text.startswith("/"):
            current = Path("/")
        parts = [p for p in text.split("/") if p not in ("", ".")]
        for i, name in enumerate(parts):
            last = i == len(parts) - 1
            if name == "..":
                current = current.parent  # an ancestor of a checked directory: checked on the way down
                continue
            path = current / name
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                if os.lstat(current).st_mode & 0o022:
                    raise IsolationError(f"{path} is missing in {current}, which others can write") from None
                return path  # dangling: only root could create it
            trusted(st, path, last=last)
            if stat.S_ISLNK(st.st_mode):
                hops[0] += 1
                if hops[0] > MAX_LINK_DEPTH:
                    raise IsolationError(f"more than {MAX_LINK_DEPTH} symlinks resolving {target}")
                current = resolve(os.readlink(path), current)
                if not last and not os.path.isdir(current):
                    raise IsolationError(f"{path} does not lead to a directory")
            elif stat.S_ISDIR(st.st_mode):
                current = path
            elif last:
                return path
            else:
                raise IsolationError(f"{path} is not a directory")
        return current

    return resolve(target, Path(os.path.normpath(base)))


def _mirror_dir(src: Path, dst_fd: int, label: Path, rel: str, actions: list[str], *, app: bool,
                top: Path | None = None) -> None:
    """Make the root-owned directory ``dst_fd`` an exact mirror of ``src`` (only changed entries are written)."""
    top = top or src
    try:
        names = sorted(os.listdir(src))
    except OSError as exc:
        raise IsolationError(f"cannot list {src}: {exc}") from exc
    wanted = set()
    for name in names:
        src_path = src / name
        rel_path = f"{rel}/{name}" if rel else name
        try:
            st = os.lstat(src_path)
        except FileNotFoundError:
            continue
        if app and ((stat.S_ISDIR(st.st_mode) and name in APP_EXCLUDE_DIRS) or name.endswith(".pyc")
                    or rel_path in APP_EXCLUDE_FILES):
            continue
        try:
            dst = os.stat(name, dir_fd=dst_fd, follow_symlinks=False)
        except FileNotFoundError:
            dst = None
        if stat.S_ISDIR(st.st_mode):
            wanted.add(name)
            if dst is not None and not stat.S_ISDIR(dst.st_mode):
                _remove_any(dst_fd, name)
            sub = _root_dir_at(dst_fd, name, label / name, 0o755, actions)
            try:
                _mirror_dir(src_path, sub, label / name, rel_path, actions, app=app, top=top)
            finally:
                os.close(sub)
        elif stat.S_ISLNK(st.st_mode):
            wanted.add(name)
            target = os.readlink(src_path)
            _check_mirrored_link(top, src_path, target)
            if dst is not None and stat.S_ISLNK(dst.st_mode) and os.readlink(name, dir_fd=dst_fd) == target:
                continue
            tmp = f".{name}.{os.urandom(6).hex()}.sync"
            os.symlink(target, tmp, dir_fd=dst_fd)
            if dst is not None and stat.S_ISDIR(dst.st_mode):
                _remove_any(dst_fd, name)
            os.rename(tmp, name, src_dir_fd=dst_fd, dst_dir_fd=dst_fd)
            actions.append(f"link {label / name} -> {target}")
        elif stat.S_ISREG(st.st_mode):
            wanted.add(name)
            if (dst is not None and stat.S_ISREG(dst.st_mode) and dst.st_size == st.st_size
                    and dst.st_mtime_ns == st.st_mtime_ns and (dst.st_uid, dst.st_gid) == (0, 0)
                    and stat.S_IMODE(dst.st_mode) == _mirror_mode(st)):
                continue
            if dst is not None and stat.S_ISDIR(dst.st_mode):
                _remove_any(dst_fd, name)
            _copy_file(src_path, st, dst_fd, name)
            actions.append(f"copied {label / name}")
        # sockets, FIFOs, devices: never mirrored
    for name in sorted(set(os.listdir(dst_fd)) - wanted):
        _remove_any(dst_fd, name)
        actions.append(f"removed stale {label / name}")


def sync_mirrors(repo: Path, paths: ServicePaths) -> list[str]:
    """Refresh ``app/`` (agents/, targets/, docs/) and ``venv/`` (the checkout's .venv) in the service area. Only
    changed files are written (temp file plus rename); every directory written is root-owned and checked first.
    Returns the actions taken."""
    actions: list[str] = []
    venv_src = Path(os.path.realpath(repo / ".venv"))
    if not (venv_src / "bin" / "python").exists():
        raise IsolationError(f"{repo / '.venv'} has no bin/python: run `make install` first")
    with _open_home(paths) as home_fd, _sync_locked(home_fd):
        _sync_locked_mirrors(repo, venv_src, paths, home_fd, actions)
    return actions


def _sync_locked_mirrors(repo: Path, venv_src: Path, paths: ServicePaths, home_fd: int, actions: list[str]) -> None:
    app_fd = _root_dir_at(home_fd, "app", paths.app, 0o755, actions)
    try:
        for name in APP_TREES:
            if not os.path.isdir(repo / name):
                raise IsolationError(f"{repo / name} is missing")
            sub = _root_dir_at(app_fd, name, paths.app / name, 0o755, actions)
            try:
                _mirror_dir(repo / name, sub, paths.app / name, name, actions, app=True)
            finally:
                os.close(sub)
        for name in sorted(set(os.listdir(app_fd)) - set(APP_TREES)):
            _remove_any(app_fd, name)
            actions.append(f"removed stale {paths.app / name}")
    finally:
        os.close(app_fd)
    venv_fd = _root_dir_at(home_fd, "venv", paths.venv, 0o755, actions)
    try:
        _mirror_dir(venv_src, venv_fd, paths.venv, "", actions, app=False)
    finally:
        os.close(venv_fd)


# ----------------------------------------------------------------------------------------------- prepare (§4.2)

def prepare(component: Component | str, ids: Ids, paths: ServicePaths, repo_state: Path) -> list[str]:
    """Prepare ``component``'s part of the service area: its private dir; API key and room.json copies (agents);
    the production lock (translator, gate); the module dir (translator; the modern server needs it to exist).
    Root writes only into root-owned, checked directories. Returns the actions taken."""
    comp = _component(component)
    if ids.uid == 0 or ids.gid == 0 or 0 in ids.groups:
        raise IsolationError(f"refusing to prepare {comp.name} for root ids {ids}")
    actions: list[str] = []
    with _open_home(paths) as home_fd, _sync_locked(home_fd):
        _prepare_locked(comp, ids, paths, Path(repo_state), home_fd, actions)
    return actions


def _prepare_locked(comp: Component, ids: Ids, paths: ServicePaths, repo_state: Path, home_fd: int,
                    actions: list[str]) -> None:
    state_fd = _root_dir_at(home_fd, "state", paths.state, 0o755, actions)
    try:
        _service_dir_at(state_fd, comp.name, paths.private(comp), ids.uid, ids.gid, 0o700, actions)
        if comp.agent:
            agents_fd = _root_dir_at(state_fd, "agents", paths.agents, 0o711, actions)
            try:
                key = _read_source(repo_state / "agents" / f"{comp.name}.json")
                if key is None:
                    _unlink_at(agents_fd, f"{comp.name}.json", paths.key(comp), actions)
                    actions.append(f"no API key for {comp.name} in {repo_state / 'agents'} (run provision)")
                else:
                    _install_copy(agents_fd, f"{comp.name}.json", paths.key(comp), key, ids.uid, ids.gid, 0o600,
                                  actions)
            finally:
                os.close(agents_fd)
            room = _read_source(repo_state / "room.json")
            if room is None:
                _unlink_at(state_fd, "room.json", paths.room, actions)
                actions.append(f"no room.json in {repo_state} (run provision)")
            else:
                _install_copy(state_fd, "room.json", paths.room, room, 0, 0, 0o644, actions)
        if comp.production_lock:
            if not ids.lock_gid:
                raise IsolationError(f"{comp.name} needs the {LOCK_GROUP} gid")
            _prepare_lock(state_fd, paths.lock, ids.lock_gid, actions)
        if comp.uses_module:
            if ids.module_owner is None or 0 in ids.module_owner:
                raise IsolationError(f"{comp.name} needs the translator's ids for {paths.module}")
            _service_dir_at(state_fd, "module", paths.module, *ids.module_owner, 0o755, actions)
    finally:
        os.close(state_fd)


def _prepare_lock(state_fd: int, label: Path, lock_gid: int, actions: list[str]) -> None:
    """root:sr-prodlock 0660, created if missing, in the root-only state directory (so it cannot be swapped)."""
    fd = os.open("production.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
                 | os.O_CLOEXEC, 0o600, dir_fd=state_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
            raise IsolationError(f"{label} must be a regular file owned by root")
        if (st.st_gid, stat.S_IMODE(st.st_mode)) != (lock_gid, 0o660):
            os.fchown(fd, 0, lock_gid)
            os.fchmod(fd, 0o660)
            actions.append(f"{label} -> root:{lock_gid} 0660")
    finally:
        os.close(fd)


def reset(paths: ServicePaths, *, owners: Mapping[str, int] | None = None) -> list[str]:
    """``targets.sh start --fresh``: unlink the proxy's routing state, the fuzzer's registry (and its staging copy)
    and the generated module in the service area. Each through an O_DIRECTORY|O_NOFOLLOW descriptor whose owner
    is checked first; unlink never follows the final component. ``owners``: directory -> expected uid."""
    if owners is None:
        owners = {}
        for dirname, comp in (("proxy", "proxy"), ("fuzzer", "fuzzer"), ("module", "translator")):
            pw = _lookup(pwd.getpwnam, COMPONENTS[comp].user)
            if pw is None:
                raise IsolationError(f"user {COMPONENTS[comp].user} does not exist")
            owners[dirname] = pw.pw_uid
    actions: list[str] = []
    targets = {"proxy": ("router.json",), "fuzzer": ("divergence_registry.json", "divergence_registry.json.staging"),
               "module": ("lists.py",)}
    with _open_home(paths) as home_fd:
        state_fd = _openat_dir(home_fd, "state", paths.state)
        try:
            st = os.fstat(state_fd)
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise IsolationError(f"{paths.state} must be owned by root and not group/other-writable")
            for dirname, names in targets.items():
                try:
                    dfd = _openat_dir(state_fd, dirname, paths.state / dirname)
                except FileNotFoundError:
                    continue
                try:
                    owner = os.fstat(dfd).st_uid
                    if owner != owners[dirname]:
                        raise IsolationError(f"{paths.state / dirname} is owned by uid {owner}, expected "
                                             f"{owners[dirname]}: refusing to reset it")
                    for name in names:
                        try:
                            _unlink_at(dfd, name, paths.state / dirname / name, actions)
                        except IsolationError as exc:  # e.g. a directory planted under that name: never abort
                            actions.append(f"WARNING: {exc}; left in place")
                finally:
                    os.close(dfd)
        finally:
            os.close(state_fd)
    return actions


def clean_target(env: Mapping[str, str] | None = None) -> ServicePaths | None:
    """The service area ``clean`` empties, whatever the mode: the marker's (for this checkout) or the default one, if
    its state directory exists."""
    marker = load_marker(env)
    home = marker["service_home"] if marker and _same_repo(marker["repo"]) else DEFAULT_SERVICE_HOME
    paths = service_paths(home)
    return paths if os.path.lexists(paths.state) else None


def clean(paths: ServicePaths) -> list[str]:
    """``make clean-state``: empty SERVICE_HOME/state. Every entry is first renamed (inside the root-only state
    directory) out of the way, then removed with the fd-based, symlink-safe ``shutil.rmtree``."""
    actions: list[str] = []
    with _open_home(paths) as home_fd, _sync_locked(home_fd):
        try:
            state_fd = _openat_dir(home_fd, "state", paths.state)
        except FileNotFoundError:
            return actions
        try:
            st = os.fstat(state_fd)
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise IsolationError(f"{paths.state} must be owned by root and not group/other-writable")
            for name in sorted(os.listdir(state_fd)):
                trash = f".trash-{os.urandom(6).hex()}"
                os.rename(name, trash, src_dir_fd=state_fd, dst_dir_fd=state_fd)
                _remove_any(state_fd, trash)
                actions.append(f"removed {paths.state / name}")
        finally:
            os.close(state_fd)
    return actions


# ----------------------------------------------------------------------------------------- privileges (§4.5)

def _cap_sets() -> dict[str, int]:
    caps: dict[str, int] = {}
    with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
        for line in fh:
            if line.startswith(("CapPrm:", "CapEff:", "CapAmb:")):
                key, value = line.split(":", 1)
                caps[key] = int(value.strip(), 16)
    return caps


def drop_privileges(uid: int, gid: int, groups: list[int]) -> None:
    """Become ``uid``/``gid`` with exactly ``groups`` as supplementary groups (replacing root's), for real,
    effective and saved ids; prove that root cannot be regained; set no_new_privs, join a fresh session keyring and
    set umask 077. The process is made non-dumpable first, so the target uid can never ptrace it or read its
    memory while it still holds root's data (execve of the component resets this)."""
    groups = list(groups)
    if uid == 0 or gid == 0 or 0 in groups:
        raise IsolationError("refusing to 'drop' privileges to root")
    if _prctl(_PR_SET_DUMPABLE, 0) != 0:
        raise IsolationError("cannot make the process non-dumpable")
    os.setgroups(groups)
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
        raise IsolationError(f"privilege drop incomplete: uids {os.getresuid()}, gids {os.getresgid()}")
    if sorted(set(os.getgroups())) != sorted(set(groups)):
        raise IsolationError(f"supplementary groups are {os.getgroups()}, expected {groups}")
    for regain in (lambda: os.setuid(0), lambda: os.setresuid(-1, 0, -1), lambda: os.setgid(0)):
        try:
            regain()
        except PermissionError:
            continue
        raise IsolationError("privileges could be regained after dropping them")
    caps = _cap_sets()
    if any(caps.get(k, 0) for k in ("CapPrm", "CapEff", "CapAmb")):
        raise IsolationError(f"capabilities survived the privilege drop: {caps}")
    # the credential change reset "dumpable" to fs.suid_dumpable (2 on some hosts): make it 0 again
    if _prctl(_PR_SET_DUMPABLE, 0) != 0 or _prctl(_PR_SET_NO_NEW_PRIVS, 1) != 0:
        raise IsolationError("cannot set non-dumpable / no_new_privs")
    _join_session_keyring()
    os.umask(0o077)


def _prctl(option: int, arg: int) -> int:
    return _libc().prctl(option, arg, 0, 0, 0)


_KEYCTL_SYSCALL = {"x86_64": 250, "aarch64": 219}
_KEYCTL_JOIN_SESSION_KEYRING = 1


def _join_session_keyring() -> None:
    """Leave the launcher's (root's) session keyring for a new anonymous one, so no key root holds is reachable.
    Tolerated where keyrings are unavailable or refused (ENOSYS, EPERM, EKEYREVOKED); any other error is fatal."""
    import platform

    number = _KEYCTL_SYSCALL.get(platform.machine())
    if number is None:
        return
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(ctypes.c_long(number), ctypes.c_long(_KEYCTL_JOIN_SESSION_KEYRING), ctypes.c_void_p(None)) < 0:
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EPERM, errno.EKEYREVOKED):
            raise IsolationError(f"cannot join a new session keyring: {os.strerror(err)}")


def close_inherited_fds() -> None:
    """Close every descriptor but 0-2 (listed from /proc/self/fd, not trusting close-on-exec defaults), then prove
    that none is left."""
    for name in os.listdir("/proc/self/fd"):
        if int(name) > 2:
            try:
                os.close(int(name))
            except OSError:
                pass  # the descriptor of the listing itself, already closed
    left = [int(n) for n in os.listdir("/proc/self/fd") if int(n) > 2]
    for fd in left:  # only the listing's own descriptor may show up, and it is closed by now
        try:
            os.fstat(fd)
        except OSError:
            continue
        raise IsolationError(f"descriptor {fd} is still open before exec")


def _lookup(fn: Callable[[str], Any], name: str) -> Any:
    try:
        return fn(name)
    except KeyError:
        return None


def resolve_ids(component: Component | str) -> Ids:
    """Look up the component's ids. Supplementary groups come from the component definition, not from /etc/group,
    so a tampered membership list cannot widen them."""
    comp = _component(component)

    def user(name: str) -> tuple[int, int]:
        pw = _lookup(pwd.getpwnam, name)
        if pw is None:
            raise IsolationError(f"user {name} does not exist: run `make isolate` (as root)")
        own = _lookup(grp.getgrnam, name)
        if pw.pw_uid == 0 or pw.pw_gid == 0 or own is None or own.gr_gid != pw.pw_gid:
            raise IsolationError(f"{name} is not a StranglerRoom service user (uid {pw.pw_uid}, gid {pw.pw_gid})")
        return pw.pw_uid, pw.pw_gid

    uid, gid = user(comp.user)
    lock = _lookup(grp.getgrnam, LOCK_GROUP) if comp.production_lock else None
    if comp.production_lock and (lock is None or lock.gr_gid == 0):
        raise IsolationError(f"group {LOCK_GROUP} is missing: run `make isolate` (as root)")
    return Ids(uid, gid, (lock.gr_gid,) if lock else (), lock.gr_gid if lock else None,
               user(COMPONENTS["translator"].user) if comp.uses_module else None)


def exec_component(name: str, argv: Sequence[str], *, environ: Mapping[str, str] | None = None) -> NoReturn:
    """``exec <component> -- argv…`` (§4). Never returns; raises IsolationError instead of running unisolated."""
    values = _values(environ)
    comp = _component(name)
    argv = list(argv)
    if not argv:
        raise IsolationError("exec: no command given after --")
    if not isolation_on(values):
        os.execvp(argv[0], argv)
    if os.geteuid() != 0:
        raise IsolationError(f"isolation is on but exec runs as uid {os.geteuid()}, not root: refusing to start "
                             f"{comp.name} unisolated")
    launch(comp, argv, values, resolve_ids(comp), _ready_paths(values), repo_root())


def launch(component: Component | str, argv: Sequence[str], values: Mapping[str, str], ids: Ids,
           paths: ServicePaths, repo: Path) -> NoReturn:
    """Steps 2-5 of §4, as root: mirrors, preparation, argv, environment, session, descriptors, ids, execve."""
    comp = _component(component)
    new_argv = service_argv(comp, argv, paths)
    report = [f"mirror: {a}" for a in sync_mirrors(repo, paths)]
    report += prepare(comp, ids, paths, repo_state_dir(values))
    token = None
    if comp.router_token:
        from targets.common.router_token import resolve_router_token

        token = resolve_router_token(values)  # as root: the rule the proxy and the gate apply unisolated
    url = resolve_switch_url(values) if comp.agent else None
    env = child_env(comp, values, paths=paths, router_token=token, switch_url=url)
    for line in report[:50]:
        print(f"isolation[{comp.name}]: {line}", file=sys.stderr)
    if len(report) > 50:
        print(f"isolation[{comp.name}]: … {len(report) - 50} more", file=sys.stderr)
    print(f"isolation[{comp.name}]: exec as {comp.user} (uid {ids.uid}, gid {ids.gid}, groups {list(ids.groups)}): "
          f"{' '.join(new_argv)}", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.setsid()
    except OSError as exc:
        raise IsolationError(f"cannot start a new session for {comp.name}: {exc}") from exc
    close_inherited_fds()
    drop_privileges(ids.uid, ids.gid, list(ids.groups))
    os.chdir(paths.app)
    os.execve(new_argv[0], new_argv, env)
    raise AssertionError("unreachable")  # pragma: no cover


# ------------------------------------------------------------------------------------------ setup / disable (§5)

def _run(cmd: Sequence[str]) -> None:
    proc = subprocess.run(list(cmd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise IsolationError(f"`{' '.join(cmd)}` failed ({proc.returncode}): {proc.stderr.strip()}")


def group_members() -> dict[str, list[str]]:
    """The exact /etc/group member lists setup enforces."""
    members = {LOCK_GROUP: [c.user for c in COMPONENTS.values() if c.production_lock]}
    for comp in COMPONENTS.values():
        members[comp.user] = []
    return members


def plan_accounts(*, getpwnam: Callable[[str], Any] = pwd.getpwnam,
                  getgrnam: Callable[[str], Any] = grp.getgrnam) -> list[list[str]]:
    """Commands that create the group and the system users and set group membership exactly (§5). Pure apart from
    the two lookups. A same-named account that does not look like ours makes setup refuse instead of adopting it."""
    cmds: list[list[str]] = []
    lock = _lookup(getgrnam, LOCK_GROUP)
    if lock is None:
        cmds.append(["groupadd", "--system", LOCK_GROUP])
    elif lock.gr_gid == 0:
        raise IsolationError(f"group {LOCK_GROUP} has gid 0")
    for comp in COMPONENTS.values():
        pw = _lookup(getpwnam, comp.user)
        own = _lookup(getgrnam, comp.user)
        if pw is None:
            if own is not None:
                raise IsolationError(f"group {comp.user} exists without user {comp.user}: remove it first")
            cmds.append(["useradd", "--system", "--no-create-home", "--home-dir", NO_HOME, "--shell",
                         NOLOGIN_SHELLS[0], "--user-group", comp.user])
            continue
        if pw.pw_uid == 0 or pw.pw_gid == 0 or own is None or own.gr_gid != pw.pw_gid:
            raise IsolationError(f"existing user {comp.user} (uid {pw.pw_uid}, gid {pw.pw_gid}) does not look like a "
                                 "StranglerRoom service user: refusing to adopt it")
        if pw.pw_shell not in NOLOGIN_SHELLS or pw.pw_dir != NO_HOME:
            raise IsolationError(f"existing user {comp.user} has shell {pw.pw_shell} and home {pw.pw_dir}: refusing "
                                 "to adopt it")
    for group, users in group_members().items():
        gr = _lookup(getgrnam, group)
        if gr is None or sorted(gr.gr_mem) != sorted(users):
            cmds.append(["gpasswd", "-M", ",".join(users), group])
    return cmds


def _require_root(what: str) -> None:
    if os.geteuid() != 0:
        raise IsolationError(f"{what} must run as root")


def _proc_bytes(pid: int, name: str) -> bytes | None:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _proc_status(pid: int) -> dict[str, list[int]] | None:
    raw = _proc_bytes(pid, "status")
    if raw is None:
        return None
    out: dict[str, list[int]] = {}
    for line in raw.decode("ascii", "replace").splitlines():
        key, _, value = line.partition(":")
        if key in ("Uid", "Gid", "Groups"):
            out[key] = [int(x) for x in value.split()]
    return out if "Uid" in out else None


def _proc_session(pid: int) -> int | None:
    raw = _proc_bytes(pid, "stat")
    if not raw:
        return None
    try:
        return int(raw[raw.rindex(b")") + 2:].split()[3])
    except (ValueError, IndexError):
        return None


def _module_pairs(cmdline: bytes) -> list[str]:
    args = cmdline.split(b"\0")
    return [b.decode("utf-8", "replace") for a, b in zip(args, args[1:]) if a == b"-m"]


def running_components(*, uids: Iterable[int] = ()) -> dict[int, str]:
    """pid -> description of every process that runs a component module (``-m <module>``, isolated or not, tracked
    or not) or runs as one of ``uids``."""
    modules = {c.module for c in COMPONENTS.values()}
    wanted = set(uids)
    found: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        pid = int(entry)
        hit = [m for m in _module_pairs(_proc_bytes(pid, "cmdline") or b"") if m in modules]
        if hit:
            found[pid] = hit[0]
            continue
        status = _proc_status(pid)
        if status and set(status["Uid"][:2]) & wanted:
            found[pid] = f"uid {status['Uid'][1]}"
    return found


def _service_uids() -> list[int]:
    return [pw.pw_uid for pw in (_lookup(pwd.getpwnam, c.user) for c in COMPONENTS.values()) if pw]


def _refuse_while_running(what: str, running: Callable[[], Mapping[int, str]] | None) -> None:
    live = (running or (lambda: running_components(uids=_service_uids())))()
    if live:
        listed = ", ".join(f"{pid} ({desc})" for pid, desc in sorted(live.items()))
        raise IsolationError(f"{what} refuses while StranglerRoom components run: {listed}. Stop them first "
                             "(scripts/agents.sh stop; scripts/targets.sh stop)")


def write_marker(data: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Path:
    """Write the marker atomically (exclusive temp file + rename) in a directory whose whole chain is checked (and
    created if missing) descriptor by descriptor; mode 0644."""
    path = marker_path(env)
    dfd = open_trusted_dir(path.parent, create_mode=0o755, owners=(0, os.geteuid()))
    assert dfd is not None
    tmp = f".{path.name}.{os.urandom(6).hex()}.tmp"
    try:
        fd = os.open(tmp, _NEW_FILE_FLAGS, 0o644, dir_fd=dfd)
        try:
            os.write(fd, (json.dumps(dict(data), indent=2, sort_keys=True) + "\n").encode())
            os.fchmod(fd, 0o644)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)
    return path


def remove_marker(env: Mapping[str, str] | None = None) -> bool:
    path = marker_path(env)
    dfd = open_trusted_dir(path.parent, owners=(0, os.geteuid()))
    if dfd is None:
        return False
    try:
        os.unlink(path.name, dir_fd=dfd)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(dfd)


def setup(env: Mapping[str, str] | None = None, *, service_home: Path | str | None = None,
          run: Callable[[Sequence[str]], None] = _run, resolve: Callable[[Component], Ids] = resolve_ids,
          repo: Path | None = None, running: Callable[[], Mapping[int, str]] | None = None,
          smoke_check: bool = True) -> list[str]:
    """``make isolate`` (§5): marker ``setting-up`` first; group and users; SERVICE_HOME with its mirrors; every
    component's part of the service area; a startup smoke import as every user; marker ``ready`` last."""
    _require_root("setup")
    values = _values(env)
    repo = repo or repo_root()
    _refuse_while_running("setup", running)
    existing = load_marker(values)
    if existing is not None and os.path.realpath(existing["repo"]) != os.path.realpath(repo):
        raise IsolationError(f"{marker_path(values)} belongs to {existing['repo']}: run `disable` there first")
    home = service_home or (existing or {}).get("service_home") or DEFAULT_SERVICE_HOME
    paths = service_paths(home)
    for protected in (repo, Path("/root")):
        if paths.home == protected or protected in paths.home.parents or paths.home in protected.parents:
            raise IsolationError(f"the service home {paths.home} must lie outside {protected}")
    check_trusted_path(paths.home, traversable=True, allow_missing=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    marker = {"version": MARKER_VERSION, "state": "setting-up", "repo": str(repo), "service_home": str(paths.home),
              "created": (existing or {}).get("created", now), "updated": now,
              "users": {c.name: c.user for c in COMPONENTS.values()}, "group": LOCK_GROUP}
    actions = [f"marker {write_marker(marker, values)}: setting-up"]
    for cmd in plan_accounts():
        run(cmd)
        actions.append(" ".join(cmd))
    actions += ensure_service_home(paths)
    actions += sync_mirrors(repo, paths)
    for comp in COMPONENTS.values():
        ids = resolve(comp)
        actions += [f"{comp.name}: {a}" for a in prepare(comp, ids, paths, repo_state_dir(values))]
        if smoke_check:
            ok, output = smoke(comp, ids, paths)
            if not ok:
                raise IsolationError(f"startup smoke failed for {comp.name} (marker left at setting-up): {output}")
            actions.append(f"{comp.name}: startup smoke import of {comp.module} as {comp.user} passed")
    llm = llm_cli_check(resolve(COMPONENTS["translator"]), values)
    if llm.status == "FAIL":
        raise IsolationError(f"STRANGLER_TRANSLATOR_MODE=llm: {llm.actual} (marker left at setting-up)")
    if llm.status == "PASS":
        actions.append(f"translator: LLM CLI {llm.actual} runs as sr-translator")
    marker.update(state="ready", updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    actions.append(f"marker {write_marker(marker, values)}: ready")
    return actions


def disable(env: Mapping[str, str] | None = None, *, purge: bool = False,
            run: Callable[[Sequence[str]], None] = _run,
            running: Callable[[], Mapping[int, str]] | None = None) -> list[str]:
    """``make isolate-off`` (§5): remove the marker; ``purge`` also removes SERVICE_HOME, the users and the group."""
    _require_root("disable")
    values = _values(env)
    _refuse_while_running("disable", running)
    marker = load_marker(values)
    if marker is not None and not _same_repo(marker["repo"]):
        raise IsolationError(f"{marker_path(values)} belongs to {marker['repo']}: run `disable` there")
    actions = []
    if remove_marker(values):
        actions.append(f"removed {marker_path(values)}")
    else:
        actions.append(f"no marker at {marker_path(values)}")
    paths = service_paths((marker or {}).get("service_home") or DEFAULT_SERVICE_HOME)
    actions += remove_key_copies(paths)  # API keys never outlive isolation, purge or not
    if purge:
        actions += _purge_home(paths)
        for comp in COMPONENTS.values():
            if _lookup(pwd.getpwnam, comp.user) is not None:
                run(["userdel", comp.user])
                actions.append(f"userdel {comp.user}")
            if _lookup(grp.getgrnam, comp.user) is not None:
                run(["groupdel", comp.user])
                actions.append(f"groupdel {comp.user}")
        if _lookup(grp.getgrnam, LOCK_GROUP) is not None:
            run(["groupdel", LOCK_GROUP])
            actions.append(f"groupdel {LOCK_GROUP}")
    return actions


def remove_key_copies(paths: ServicePaths) -> list[str]:
    """Unlink the agents' API-key copies from the root-only SERVICE_HOME/state/agents (checked descriptors)."""
    actions: list[str] = []
    if not os.path.lexists(paths.agents):
        return actions
    with _open_home(paths) as home_fd:
        state_fd = _openat_dir(home_fd, "state", paths.state)
        try:
            agents_fd = _openat_dir(state_fd, "agents", paths.agents)
            try:
                if os.fstat(agents_fd).st_uid != 0:
                    raise IsolationError(f"{paths.agents} is not owned by root")
                for name in sorted(os.listdir(agents_fd)):
                    _unlink_at(agents_fd, name, paths.agents / name, actions)
            finally:
                os.close(agents_fd)
        finally:
            os.close(state_fd)
    return actions


def _purge_home(paths: ServicePaths) -> list[str]:
    if not os.path.lexists(paths.home):
        return [f"{paths.home} does not exist"]
    check_trusted_path(paths.home)
    extra = sorted(set(os.listdir(paths.home)) - {"app", "venv", "state", ".sync.lock"})
    if extra:
        raise IsolationError(f"{paths.home} holds unexpected entries {extra}: not removing it")
    pfd = _openat_dir(None, paths.home.parent, paths.home.parent)
    try:
        trash = f".{paths.home.name}.purge-{os.urandom(6).hex()}"
        os.rename(paths.home.name, trash, src_dir_fd=pfd, dst_dir_fd=pfd)
        shutil.rmtree(trash, dir_fd=pfd)
    finally:
        os.close(pfd)
    return [f"removed {paths.home}"]


# ----------------------------------------------------------------------------------------------- verify (§6)

@dataclass(frozen=True)
class Probe:
    """One operation a service user attempts in a forked child; the kernel decides."""

    section: str
    check: str
    op: str          # read | list | create | rw | reach | signal
    path: str
    expected: bool


@dataclass(frozen=True)
class Row:
    section: str
    subject: str
    check: str
    expected: str
    actual: str
    status: str      # PASS | FAIL | SKIP


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _row(section: str, subject: str, check: str, expected: bool, actual: bool) -> Row:
    return Row(section, subject, check, _yes(expected), _yes(actual), "PASS" if expected == actual else "FAIL")


def attempt(op: str, path: str) -> bool:
    """Try one operation as the current (dropped) user. True if the kernel allowed it. Side effects are undone."""
    try:
        if op == "read":
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
            try:
                os.read(fd, 1)
            finally:
                os.close(fd)
            return True
        if op == "list":
            os.listdir(path)
            return True
        if op == "create":
            name = os.path.join(path, f".sr-verify-{os.getpid()}-{os.urandom(6).hex()}.probe")
            fd = os.open(name, _NEW_FILE_FLAGS, 0o600)
            os.close(fd)
            os.unlink(name)
            return True
        if op == "rw":
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
            os.close(fd)
            return True
        if op == "reach":  # read a file, or list or traverse a directory, or even stat it
            st = os.stat(path)
            if stat.S_ISDIR(st.st_mode):
                try:
                    os.listdir(path)
                    return True
                except OSError:
                    pass
                try:
                    os.stat(os.path.join(path, "."))
                    return True
                except OSError:
                    return False
            return attempt("read", path)
        if op == "signal":
            os.kill(int(path), 0)
            return True
    except OSError:
        return False
    raise ValueError(f"unknown probe operation {op!r}")


def run_as(ids: Ids, fn: Callable[..., Any], *args: Any, timeout: float = 300.0) -> Any:
    """Run ``fn(*args)`` in a forked child that first drops to ``ids``; return its JSON-serialisable result.

    The child inherits this process's memory (verify's children search for secret values), but it is not dumpable
    after the drop (no ptrace, no /proc/<pid>/mem or environ for that uid), it cannot dump core (RLIMIT_CORE hard
    limit 1, which also disables piped core handlers), and a child the service's uid stops or stalls is killed
    after ``timeout`` seconds."""
    import resource

    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never return into the caller's stack
        code = 0
        try:
            os.close(rfd)
            try:
                resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
                drop_privileges(ids.uid, ids.gid, list(ids.groups))
                payload = {"ok": True, "result": fn(*args)}
            except BaseException as exc:  # noqa: BLE001 - reported to the parent
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            data = memoryview(json.dumps(payload).encode())
            while data:
                data = data[os.write(wfd, data):]
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(wfd)
    chunks = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([rfd], [], [], remaining)[0]:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise IsolationError(f"probe child as uid {ids.uid} did not finish within {timeout:.0f}s")
            chunk = os.read(rfd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(rfd)
    os.waitpid(pid, 0)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        payload = {}
    if not payload.get("ok"):
        raise IsolationError(f"probe child as uid {ids.uid} failed: {payload.get('error', 'no result')}")
    return payload["result"]


def _attempt_all(probes: list[tuple[str, str]]) -> list[bool]:
    return [attempt(op, path) for op, path in probes]


def run_probes(subject: str, ids: Ids, probes: Sequence[Probe]) -> list[Row]:
    results = run_as(ids, _attempt_all, [(p.op, p.path) for p in probes])
    return [_row(p.section, subject, p.check, p.expected, bool(r)) for p, r in zip(probes, results)]


def access_probes(component: Component | str, paths: ServicePaths, repo_state: Path, *,
                  repo: Path | None = None, pids: Mapping[str, int] | None = None,
                  operator_home: Path = Path("/root")) -> list[Probe]:
    """The §6.2 access matrix for one component (planned as root; executed by ``run_probes``)."""
    comp = _component(component)
    repo = repo or repo_root()
    probes: list[Probe] = []

    def add(check: str, op: str, path: Path | str, expected: bool, section: str = "access") -> None:
        probes.append(Probe(section, check, op, str(path), expected))

    for other in (c for c in COMPONENTS.values() if c.agent):
        if other is comp:
            add("read own API key copy", "read", paths.key(other), True)
        elif os.path.lexists(paths.key(other)):
            add(f"read {other.name}'s API key copy", "read", paths.key(other), False)
    if comp.agent:
        add("read room.json", "read", paths.room, True)
    add("reach the checkout", "reach", repo, False)
    add("reach the operator's home", "reach", operator_home, False)
    for path in (repo_state / "switch.env", repo_state / "router.token", repo_state / "agents"):
        add(f"reach {path}", "reach", path, False)
    for other in (c for c in COMPONENTS.values() if c.agent):
        add(f"reach {repo_state / 'agents' / (other.name + '.json')}", "reach",
            repo_state / "agents" / f"{other.name}.json", False)
    add(f"reach {operator_home / '.env'}", "reach", operator_home / ".env", False)
    add("write own private dir", "create", paths.private(comp), True)
    for other in COMPONENTS.values():
        if other is not comp and os.path.lexists(paths.private(other)):
            add(f"write {other.name}'s private dir", "create", paths.private(other), False)
            add(f"list {other.name}'s private dir", "list", paths.private(other), False)
    add("write state/", "create", paths.state, False)
    add("write state/agents/", "create", paths.agents, False)
    add("write app/", "create", paths.app, False)
    add("write app/agents/", "create", paths.app / "agents", False)
    add("write venv/", "create", paths.venv, False)
    add("write venv/bin/", "create", paths.venv / "bin", False)
    add("write the service home", "create", paths.home, False)
    add("open production.lock read-write", "rw", paths.lock, comp.production_lock)
    add("create in module/", "create", paths.module, comp.module_writer)
    for other, pid in (pids or {}).items():
        if other != comp.name:
            add(f"signal {other} (pid {pid})", "signal", str(pid), False, section="process")
    return probes


def smoke(component: Component | str, ids: Ids, paths: ServicePaths, *, timeout: float = 120.0) -> tuple[bool, str]:
    """§6.3: as the component's user (new session, no inherited descriptors), the service interpreter imports the
    component's module from app/. Returns (ok, output tail)."""
    comp = _component(component)
    env = child_env(comp, {"PATH": os.environ.get("PATH") or "/usr/bin:/bin"}, paths=paths,
                    router_token="smoke-test-placeholder" if comp.router_token else None,
                    switch_url=DEFAULT_SWITCH_URL if comp.agent else None)
    argv = [str(paths.python), "-c", "import importlib, sys; importlib.import_module(sys.argv[1])", comp.module]
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(rfd)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(wfd, 1)
            os.dup2(wfd, 2)
            os.setsid()
            close_inherited_fds()
            drop_privileges(ids.uid, ids.gid, list(ids.groups))
            os.chdir(paths.app)
            os.execve(argv[0], argv, env)
        except BaseException as exc:  # noqa: BLE001
            try:
                os.write(2, f"smoke: {type(exc).__name__}: {exc}\n".encode())
            finally:
                os._exit(127)
    os.close(wfd)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                output += b"\n(timed out)"
                break
            ready, _, _ = select.select([rfd], [], [], remaining)
            if ready:
                chunk = os.read(rfd, 65536)
                if not chunk:
                    break
                output += chunk
    finally:
        os.close(rfd)
    _, status = os.waitpid(pid, 0)
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 and b"(timed out)" not in output
    return ok, output.decode("utf-8", "replace").strip()[-800:]


def scan_candidates(roots: Iterable[str | Path], *, max_bytes: int = SCAN_MAX_BYTES) -> list[tuple[str, list[str]]]:
    """(directory, [regular files <= max_bytes and symlinks]) for every directory under ``roots``, listed as root
    by descriptor (not ``os.fwalk``, which opens a symlinked directory before checking it): each subdirectory is
    opened O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK relative to its parent's descriptor, so a directory a service swaps for
    a symlink (or a device) mid-walk is neither followed nor opened. Root only lists names here; it never opens a
    candidate nor resolves a link (only the dropped child does, ``search_readable``)."""
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    def walk(fd: int, path: str, depth: int) -> None:
        if path in seen or depth > 256:
            return
        seen.add(path)
        files, subdirs = [], []
        try:
            names = sorted(os.listdir(fd))
        except OSError:
            return
        for name in names:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                subdirs.append(name)
            elif (stat.S_ISREG(st.st_mode) and st.st_size <= max_bytes) or stat.S_ISLNK(st.st_mode):
                files.append(name)
        out.append((path, files))
        for name in subdirs:
            try:
                child = os.open(name, _DIR_FLAGS | os.O_NONBLOCK, dir_fd=fd)
            except OSError:
                continue
            try:
                walk(child, os.path.join(path, name), depth + 1)
            finally:
                os.close(child)

    for root in roots:
        root = os.path.normpath(str(root))
        try:
            fd = os.open(root, _DIR_FLAGS | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            walk(fd, root, 0)
        finally:
            os.close(fd)
    return out


#: caps for the directories a dropped child reaches only through symlinks (they may lead anywhere, even in loops)
LINK_WALK_MAX_FILES = 20000
LINK_WALK_MAX_BYTES = 512 * 1024 * 1024


def search_readable(candidates: Sequence[tuple[str, Sequence[str]]], secrets: Mapping[str, bytes], *,
                    own_key: str | None = None, max_bytes: int = SCAN_MAX_BYTES,
                    link_max_files: int = LINK_WALK_MAX_FILES,
                    link_max_bytes: int = LINK_WALK_MAX_BYTES) -> dict[str, Any]:
    """As the current (dropped) user: read every candidate it can (the kernel decides) and search it for every
    secret value. Each candidate is opened ``O_PATH`` (following a symlink as this user only; no side effects even
    on a device node), checked with fstat to be a regular file, and only then reopened for reading through that
    descriptor. A candidate that leads to a directory (a directory symlink) is walked here, as this user, with a
    (st_dev, st_ino) visited set and the ``link_max_*`` caps. Returns the number of files read, [path, [labels],
    is-own-key-copy] for each hit (never a value) and whether a cap cut a walk short."""
    by_value: dict[bytes, list[str]] = {}
    for label, value in secrets.items():
        by_value.setdefault(value, []).append(label)
    own_id = None
    if own_key:
        try:
            own_st = os.stat(own_key)
            own_id = (own_st.st_dev, own_st.st_ino)
        except OSError:
            pass
    visited: set[tuple[int, int]] = set()
    for dirpath, _files in candidates:  # directories the listing covers are not walked again through links
        try:
            dst = os.stat(dirpath)
            visited.add((dst.st_dev, dst.st_ino))
        except OSError:
            pass
    state: dict[str, Any] = {"read": 0, "hits": [], "truncated": False, "link_files": 0, "link_bytes": 0}

    def read_one(path: str, pfd: int, st: os.stat_result, via_link_walk: bool) -> None:
        if st.st_size > max_bytes:
            return
        if via_link_walk:
            if state["link_files"] >= link_max_files or state["link_bytes"] + st.st_size > link_max_bytes:
                state["truncated"] = True
                return
            state["link_files"] += 1
            state["link_bytes"] += st.st_size
        try:
            fd = os.open(f"/proc/self/fd/{pfd}", os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            return
        try:
            data = os.read(fd, max_bytes + 1)[:max_bytes]
        except OSError:
            return
        finally:
            os.close(fd)
        state["read"] += 1
        found = [label for value, labels in by_value.items() if value in data for label in labels]
        if found:
            state["hits"].append([path, found, (st.st_dev, st.st_ino) == own_id])

    def visit(path: str, stack: list[tuple[str, tuple[int, int]]] | None) -> None:
        try:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)  # follows a symlink, as this user
        except OSError:
            return
        try:
            st = os.fstat(pfd)
            if stat.S_ISREG(st.st_mode):
                read_one(path, pfd, st, stack is not None)
            elif stat.S_ISDIR(st.st_mode):
                key = (st.st_dev, st.st_ino)
                if stack is not None:
                    stack.append((path, key))
                else:
                    walk(path, key)
        finally:
            os.close(pfd)

    def walk(top: str, key: tuple[int, int]) -> None:
        stack = [(top, key)]
        while stack and not state["truncated"]:
            dirpath, key = stack.pop()
            if key in visited:
                continue
            visited.add(key)
            try:
                names = sorted(os.listdir(dirpath))
            except OSError:
                continue
            for name in names:
                visit(os.path.join(dirpath, name), stack)

    for dirpath, files in candidates:
        if not os.access(dirpath, os.X_OK):
            continue
        for name in files:
            visit(os.path.join(dirpath, name), None)
    return {"read": state["read"], "hits": state["hits"], "truncated": state["truncated"]}


def secret_scan(subjects: Mapping[str, Ids], secrets: Mapping[str, str], roots: Iterable[str | Path], *,
                own_keys: Mapping[str, str] | None = None) -> list[Row]:
    """§6.4: as each subject, a forked child reads every candidate it can (the kernel decides), following symlinks
    only as that user, and searches it for every secret value. A hit fails unless it is the subject's own API key
    in its own key copy (``own_keys``: subject -> path; matched by inode, so an alias of the copy is no hit). A
    symlinked-directory walk cut short by its caps fails: the scan is then incomplete."""
    rows: list[Row] = []
    usable = {k: v.encode() for k, v in secrets.items() if len(v) >= MIN_SECRET_LEN}
    for label in sorted(set(secrets) - set(usable)):
        rows.append(Row("secrets", "-", f"{label}: shorter than {MIN_SECRET_LEN} characters, not scanned", "-", "-",
                        "SKIP"))
    candidates = scan_candidates(roots)
    for subject, ids in subjects.items():
        own_key = (own_keys or {}).get(subject)
        result = run_as(ids, lambda key=own_key: search_readable(candidates, usable, own_key=key), timeout=1800.0)
        bad = []
        for path, labels, own in result["hits"]:
            labels = [lb for lb in labels if not (own and lb == f"api-key:{subject}")]
            if labels:
                bad.append((path, labels))
        for path, labels in bad:
            rows.append(Row("secrets", subject, f"{path} exposes {', '.join(sorted(set(labels)))}", "no", "yes",
                            "FAIL"))
        rows.append(Row("secrets", subject, f"{result['read']} readable files searched for {len(usable)} secret "
                        "values", "0 hits", f"{len(bad)} hits", "PASS" if not bad else "FAIL"))
        if result["truncated"]:
            rows.append(Row("secrets", subject, f"symlinked directories walked within {LINK_WALK_MAX_FILES} files / "
                            f"{LINK_WALK_MAX_BYTES >> 20} MiB", "complete", "cut short", "FAIL"))
    return rows


def _secret_keys(entries: Mapping[str, str], source: str) -> dict[str, str]:
    out = {}
    for key, value in entries.items():
        if value and re.search(r"PASSWORD|TOKEN|SECRET|_KEY|KEYS$|DATASOURCE", key):
            out[f"{source}:{key}"] = value
            if key == "SECRET_KEYS":  # "<id>:<secret>,…": each secret on its own as well
                for i, part in enumerate(value.split(",")):
                    secret = part.split(":", 1)[-1].strip()
                    if secret and secret != value:
                        out[f"{source}:{key}[{i}]"] = secret
    return out


def _datasource_password(datasource: str) -> str | None:
    from urllib.parse import unquote, urlsplit

    try:
        password = urlsplit(datasource).password
    except ValueError:
        return None
    return unquote(password) if password else None


def secret_values(repo_state: Path, repo: Path, *, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """label -> value of every secret verify searches for (read as root)."""
    values = _values(env)
    out: dict[str, str] = {}
    out.update(_secret_keys(_read_env_file(repo_state / "switch.env"), "switch.env"))
    out.update(_secret_keys(_read_env_file(repo / ".switch-infra" / ".env"), ".switch-infra/.env"))
    token = (values.get("ROUTER_ADMIN_TOKEN") or "").strip()
    if token:
        out["ROUTER_ADMIN_TOKEN"] = token
    token_files = {"router.token": repo_state / "router.token"}
    if (values.get("ROUTER_TOKEN_FILE") or "").strip():
        token_files["ROUTER_TOKEN_FILE"] = router_token_path(values)
    for label, path in token_files.items():
        data = _read_source(path) if os.path.lexists(path) else None
        if data and data.strip():
            out[label] = data.decode("utf-8", "replace").strip()
    for comp in (c for c in COMPONENTS.values() if c.agent):
        try:
            data = json.loads(_read_source(repo_state / "agents" / f"{comp.name}.json") or b"null")
        except (ValueError, IsolationError):
            continue
        if isinstance(data, dict) and data.get("api_key"):
            out[f"api-key:{comp.name}"] = str(data["api_key"])
    datasources = [values.get("MM_SQLSETTINGS_DATASOURCE") or "",
                   _read_env_file(repo / ".native" / "mm-env.sh").get("MM_SQLSETTINGS_DATASOURCE", "")]
    try:
        config = json.loads(_read_source(repo / ".native" / "mattermost" / "config" / "config.json") or b"{}")
        datasources.append(str((config.get("SqlSettings") or {}).get("DataSource") or ""))
    except (ValueError, AttributeError, IsolationError):
        pass
    for ds in datasources:
        password = _datasource_password(ds) if ds else None
        if password:
            out["mattermost-db-password"] = password
    return {k: v for k, v in out.items() if v}


def live_pid(component: Component | str, env: Mapping[str, str] | None = None) -> int | None:
    """The component's pid from its pid file, if that process is alive and runs the component's module."""
    comp = _component(component)
    try:
        raw = (run_dir_for(comp, env) / f"{comp.name}.pid").read_text().strip()
    except OSError:
        return None
    if not raw.isdigit() or int(raw) <= 0:
        return None
    pid = int(raw)
    if comp.module not in _module_pairs(_proc_bytes(pid, "cmdline") or b""):
        return None
    return pid


def component_instances() -> dict[str, list[int]]:
    """component -> pids of every process on the host whose argv holds ``-m <its module>``, independent of pid
    files (an untracked or root-run instance is found too)."""
    found: dict[str, list[int]] = {}
    for entry in sorted((e for e in os.listdir("/proc") if e.isdigit()), key=int):
        if int(entry) == os.getpid():
            continue
        pairs = _module_pairs(_proc_bytes(int(entry), "cmdline") or b"")
        for comp in COMPONENTS.values():
            if comp.module in pairs:
                found.setdefault(comp.name, []).append(int(entry))
    return found


def process_checks(subjects: Mapping[str, Ids], secrets: Mapping[str, str], pids: Mapping[str, int], *,
                   instances: Mapping[str, Sequence[int]] | None = None) -> list[Row]:
    """§6.5: every process running a component module (``instances``, default: the whole host) must be that
    component's single tracked pid (``pids``, from the pid files), run as its user with its groups, lead its own
    session and have finished the isolated exec. SKIP only when no instance runs. ROUTER_ADMIN_TOKEN only in the
    proxy's and the gate's environment; no secret value in any service process's environment (but the token
    there) or in any process's command line."""
    rows: list[Row] = []
    uid_owner = {ids.uid: name for name, ids in subjects.items()}
    instances = component_instances() if instances is None else instances
    checked: list[tuple[str, int]] = []
    for name in subjects:
        found = sorted(instances.get(name, []))
        if not found:
            rows.append(Row("process", name, "running", "yes", "no", "SKIP"))
            continue
        if len(found) > 1:
            rows.append(Row("process", name, f"exactly one instance of {COMPONENTS[name].module}", "1",
                            f"{len(found)}: pids {found}", "FAIL"))
        for pid in found:
            rows.append(_row("process", name, f"pid {pid} is the tracked instance (pid file: {pids.get(name)})", True,
                             pid == pids.get(name)))
            checked.append((name, pid))
    for name, pid in checked:
        ids = subjects[name]
        status = _proc_status(pid) or {}
        rows.append(_row("process", name, f"pid {pid} runs as uid {ids.uid} (real/effective/saved/fs)", True,
                         status.get("Uid") == [ids.uid] * 4))
        rows.append(_row("process", name, f"pid {pid} has gid {ids.gid} and groups {list(ids.groups)}", True,
                         status.get("Gid") == [ids.gid] * 4
                         and sorted(status.get("Groups", [])) == sorted(ids.groups)))
        rows.append(_row("process", name, f"pid {pid} leads its own session", True, _proc_session(pid) == pid))
        rows.append(_row("process", name, f"pid {pid} finished the isolated exec", True,
                         "agents.isolation" not in _module_pairs(_proc_bytes(pid, "cmdline") or b"")))
    usable = {k: v.encode() for k, v in secrets.items() if len(v) >= MIN_SECRET_LEN}
    owned: dict[str, list[int]] = {}
    for entry in sorted((e for e in os.listdir("/proc") if e.isdigit()), key=int):
        pid = int(entry)
        cmdline = _proc_bytes(pid, "cmdline") or b""
        leaked = sorted(label for label, value in usable.items() if value in cmdline)
        if leaked:
            rows.append(Row("process", f"pid {pid}", f"command line exposes {', '.join(leaked)}", "no", "yes", "FAIL"))
        status = _proc_status(pid)
        if status and status["Uid"][1] in uid_owner:
            owned.setdefault(uid_owner[status["Uid"][1]], []).append(pid)
    for name, owned_pids in owned.items():
        # /proc/<pid>/environ of another uid needs CAP_SYS_PTRACE, which root may lack (it does in this pod): read it
        # as the process's own user, in a forked child. Only labels and flags come back, never values.
        findings = run_as(subjects[name], _environ_findings, owned_pids, usable)
        allowed = COMPONENTS[name].router_token
        for pid, (readable, has_token, exposed) in zip(owned_pids, findings):
            if not readable:
                if _proc_bytes(pid, "status") is not None:  # still alive: an unreadable environment is a failure
                    rows.append(Row("process", name, f"pid {pid} environment readable by its own user", "yes", "no",
                                    "FAIL"))
                continue
            rows.append(_row("process", name, f"pid {pid} has ROUTER_ADMIN_TOKEN in its environment", allowed,
                             has_token))
            exposed = [lb for lb in exposed if not (allowed and lb in _TOKEN_LABELS)]
            if exposed:
                rows.append(Row("process", name, f"pid {pid} environment exposes {', '.join(exposed)}", "no", "yes",
                                "FAIL"))
    return rows


_TOKEN_LABELS = frozenset({"ROUTER_ADMIN_TOKEN", "router.token", "ROUTER_TOKEN_FILE"})


def _environ_findings(pids: Sequence[int], usable: Mapping[str, bytes]) -> list[tuple[bool, bool, list[str]]]:
    """In a child dropped to the processes' user: (readable, has ROUTER_ADMIN_TOKEN, labels of secrets found)."""
    out = []
    for pid in pids:
        environ = _proc_bytes(pid, "environ")
        if environ is None:
            out.append((False, False, []))
            continue
        has_token = any(v.startswith(b"ROUTER_ADMIN_TOKEN=") for v in environ.split(b"\0"))
        out.append((True, has_token, sorted(label for label, value in usable.items() if value in environ)))
    return out


def _mode_row(rows: list[Row], what: str, path: Path, want: tuple[int, int, int | None], *,
              kind: str = "dir") -> None:
    try:
        st = os.lstat(path)
    except OSError:
        rows.append(Row("config", "-", f"{path} {what}", "present", "missing", "FAIL"))
        return
    is_kind = stat.S_ISDIR(st.st_mode) if kind == "dir" else stat.S_ISREG(st.st_mode)
    actual = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
    ok = is_kind and actual[0] == want[0] and actual[1] == want[1] and (want[2] is None or actual[2] == want[2])
    rows.append(Row("config", "-", f"{path} {what}", f"{want[0]:04o} {want[1]}:{'*' if want[2] is None else want[2]}",
                    f"{actual[0]:04o} {actual[1]}:{actual[2]}" + ("" if is_kind else f" (not a {kind})"),
                    "PASS" if ok else "FAIL"))


def config_checks(paths: ServicePaths, env: Mapping[str, str] | None = None) -> list[Row]:
    """§6.1: marker, users, group and memberships, the service area's ownership and modes, run directories."""
    values = _values(env)
    rows: list[Row] = []

    def check(subject: str, what: str, ok: bool, actual: str = "") -> None:
        rows.append(Row("config", subject, what, "yes", actual or _yes(ok), "PASS" if ok else "FAIL"))

    try:
        marker = load_marker(values)
        error = ""
    except IsolationError as exc:
        marker, error = None, str(exc)
    check("-", f"marker {marker_path(values)} is ready and names this checkout",
          marker is not None and marker["state"] == "ready" and _same_repo(marker["repo"]),
          actual=error or ("missing" if marker is None else f"{marker['state']}, {marker['repo']}"))
    try:
        on = isolation_on(values)
        error = ""
    except IsolationError as exc:
        on, error = False, str(exc)
    check("-", "isolation mode resolves to on", on, actual=error)
    lock = _lookup(grp.getgrnam, LOCK_GROUP)
    check(LOCK_GROUP, "group exists", lock is not None and lock.gr_gid != 0)
    ids: dict[str, tuple[int, int]] = {}
    for comp in COMPONENTS.values():
        pw = _lookup(pwd.getpwnam, comp.user)
        own = _lookup(grp.getgrnam, comp.user)
        ok = (pw is not None and own is not None and pw.pw_uid != 0 and pw.pw_gid == own.gr_gid != 0
              and pw.pw_shell in NOLOGIN_SHELLS and pw.pw_dir == NO_HOME)
        check(comp.user, "system user: own primary group, no login shell, home /nonexistent", ok,
              actual="" if pw else "missing")
        if pw is not None:
            ids[comp.name] = (pw.pw_uid, pw.pw_gid)
    service_gids: dict[int, str] = {}
    for group, users in group_members().items():
        gr = _lookup(grp.getgrnam, group)
        if gr is None:
            continue
        service_gids[gr.gr_gid] = group
        check(group, f"members exactly: {', '.join(users) or 'none'}", sorted(gr.gr_mem) == sorted(users),
              actual=", ".join(sorted(gr.gr_mem)) or "none")
    foreign = sorted(pw.pw_name for pw in pwd.getpwall()
                     if pw.pw_gid in service_gids and pw.pw_name != service_gids[pw.pw_gid])
    check("-", "no other user has a service group as its primary group", not foreign, actual=", ".join(foreign))
    try:
        check_trusted_path(paths.home, traversable=True)
        error = ""
    except IsolationError as exc:
        error = str(exc)
    check("-", f"{paths.home}: trusted path (root-owned, not writable by others), searchable", not error, error)
    _mode_row(rows, "root:root 0755", paths.home, (0o755, 0, 0))
    for tree in (paths.app, paths.venv):
        _mode_row(rows, "root:root 0755", tree, (0o755, 0, 0))
        bad = _not_root_only(tree)
        check("-", f"everything in {tree} is root-owned and writable by root only", not bad,
              actual=", ".join(bad[:3]) + (" …" if len(bad) > 3 else ""))
    _mode_row(rows, "root:root 0755", paths.state, (0o755, 0, 0))
    _mode_row(rows, "root 0711", paths.agents, (0o711, 0, None))
    _mode_row(rows, "root 0644", paths.room, (0o644, 0, None), kind="file")
    if lock is not None:
        _mode_row(rows, f"root:{LOCK_GROUP} 0660", paths.lock, (0o660, 0, lock.gr_gid), kind="file")
    for comp in COMPONENTS.values():
        if comp.name not in ids:
            continue
        uid, gid = ids[comp.name]
        _mode_row(rows, f"{comp.user} 0700", paths.private(comp), (0o700, uid, gid))
        if comp.agent:
            _mode_row(rows, f"{comp.user} 0600", paths.key(comp), (0o600, uid, gid), kind="file")
    if "translator" in ids:
        uid, gid = ids["translator"]
        _mode_row(rows, "sr-translator 0755", paths.module, (0o755, uid, gid))
        if os.path.lexists(paths.module_file):
            _mode_row(rows, "sr-translator 0644", paths.module_file, (0o644, uid, None), kind="file")
    for rd in run_dirs(values):
        if not os.path.lexists(rd):
            continue
        try:
            check_trusted_path(rd)
            error = ""
        except IsolationError as exc:
            error = str(exc)
        check("-", f"run directory {rd}: trusted path, root-owned, not writable by others", not error, error)
    return rows


def _not_root_only(tree: Path) -> list[str]:
    """Entries of a mirror that are not root's alone, including symlinks whose resolution passes through anything
    a service could change (``check_link_target``)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(tree, onerror=lambda _e: None):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if st.st_uid != 0 or (not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o7022):
                out.append(path)
            elif stat.S_ISLNK(st.st_mode):
                try:
                    check_link_target(os.readlink(path), Path(dirpath))
                except (IsolationError, OSError) as exc:
                    out.append(f"{path} ({exc})")
    return out


def tcp_listeners() -> dict[int, list[tuple[str, int]]]:
    """port -> [(address, uid)] of every listening TCP socket, IPv4 and IPv6 (/proc/net/tcp{,6}: root here may lack
    CAP_SYS_PTRACE, so sockets are attributed by uid, not by pid)."""
    import ipaddress

    out: dict[int, list[tuple[str, int]]] = {}
    for table, width in (("/proc/net/tcp", 4), ("/proc/net/tcp6", 16)):
        try:
            with open(table, encoding="ascii") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8 or fields[3] != "0A":
                continue
            hex_addr, hex_port = fields[1].split(":")
            raw = bytes.fromhex(hex_addr)
            raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))  # each 32-bit word is host order
            addr = str(ipaddress.ip_address(raw)) if len(raw) == width else hex_addr
            out.setdefault(int(hex_port, 16), []).append((addr, int(fields[7])))
    return out


def listener_checks(expected: Mapping[int, tuple[str, int | None]],
                    listeners: Mapping[int, Sequence[tuple[str, int]]] | None = None) -> list[Row]:
    """Every listener, in either family and on any address ([::1] and [::] included), on each expected port must
    belong to the expected uid: a service listening where a client dials a name (e.g. localhost -> ::1) could
    collect credentials. SKIP when nothing listens on a port."""
    listeners = tcp_listeners() if listeners is None else listeners
    rows = []
    for port, (owner, uid) in sorted(expected.items()):
        found = listeners.get(port, [])
        if not found:
            rows.append(Row("process", owner, f"something listens on :{port}", "yes", "no", "SKIP"))
        for addr, actual in found:
            rows.append(_row("process", owner, f"listener {addr}:{port} belongs to uid {uid}", True, actual == uid))
    return rows


def _expected_listeners(values: Mapping[str, str]) -> dict[int, tuple[str, int | None]]:
    from urllib.parse import urlsplit

    switch_port = urlsplit(resolve_switch_url(values)).port or 80
    expected: dict[int, tuple[str, int | None]] = {switch_port: ("switch", 0)}
    for name, port in (("proxy", 9000), ("legacy", 9001), ("modern", 9002)):
        pw = _lookup(pwd.getpwnam, COMPONENTS[name].user)
        expected[port] = (name, pw.pw_uid if pw else None)
    return expected


LLM_CLI_FIX = ("install the LLM CLI outside /root (root-owned, e.g. /usr/local/bin) and pass its key with "
               "STRANGLER_TRANSLATOR_PASS_ENV")


def _find_executable(word: str, search: str) -> str | None:
    """As the current (dropped) user: where ``word`` resolves on ``search`` (a PATH), if executable for it."""
    candidates = [word] if "/" in word else [os.path.join(d or ".", word) for d in search.split(":")]
    for path in candidates:
        if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def llm_cli_check(ids: Ids, env: Mapping[str, str] | None = None) -> Row:
    """In LLM mode the translator runs ``STRANGLER_LLM_CMD``: its first word must resolve, as the translator's user
    and on the translator's PATH, to an executable whose whole resolution chain only root can modify. SKIP in the
    deterministic mode."""
    import shlex

    values = _values(env)
    mode = (values.get("STRANGLER_TRANSLATOR_MODE") or "deterministic").strip().lower()
    check = "LLM CLI (first word of STRANGLER_LLM_CMD) runs for sr-translator"
    if mode != "llm":
        return Row("smoke", "translator", check, "-", f"mode {mode}", "SKIP")
    from agents.llm_synth import DEFAULT_LLM_CMD

    words = shlex.split(values.get("STRANGLER_LLM_CMD") or DEFAULT_LLM_CMD)
    if not words:
        return Row("smoke", "translator", check, "found", f"empty command; {LLM_CLI_FIX}", "FAIL")
    found = run_as(ids, _find_executable, words[0], values.get("PATH") or "/usr/bin:/bin")
    if found is None:
        return Row("smoke", "translator", check, "found", f"{words[0]} not found or not executable for "
                   f"sr-translator: {LLM_CLI_FIX}", "FAIL")
    try:
        check_link_target(found, Path("/"))
    except IsolationError as exc:
        return Row("smoke", "translator", check, "root-only", f"{found}: {exc}; {LLM_CLI_FIX}", "FAIL")
    return Row("smoke", "translator", check, "found", found, "PASS")


def verify(env: Mapping[str, str] | None = None, *, roots: Iterable[str] = SCAN_ROOTS) -> tuple[bool, list[Row]]:
    """``make isolate-verify`` (§6). Returns (all passed, rows)."""
    _require_root("verify")
    values = _values(env)
    marker = load_marker(values)
    paths = service_paths((marker or {}).get("service_home") or DEFAULT_SERVICE_HOME)
    repo_state = repo_state_dir(values)
    rows = config_checks(paths, values)
    subjects: dict[str, Ids] = {}
    for comp in COMPONENTS.values():
        try:
            subjects[comp.name] = resolve_ids(comp)
        except IsolationError as exc:
            rows.append(Row("access", comp.name, "resolve ids", "ok", str(exc), "FAIL"))
    pids = {name: pid for name in subjects if (pid := live_pid(name, values)) is not None}
    for name, ids in subjects.items():
        rows += run_probes(name, ids, access_probes(name, paths, repo_state, pids=pids))
        if os.path.lexists(paths.python):
            ok, output = smoke(name, ids, paths)
            comp = COMPONENTS[name]
            rows.append(Row("smoke", name, f"{paths.python} imports {comp.module} as {comp.user}", "ok",
                            "ok" if ok else (output.splitlines() or ["failed"])[-1][:60], "PASS" if ok else "FAIL"))
    if "translator" in subjects:
        rows.append(llm_cli_check(subjects["translator"], values))
    secrets = secret_values(repo_state, repo_root(), env=values)
    if subjects:
        rows += secret_scan(subjects, secrets, [str(paths.home), *roots],
                            own_keys={c.name: str(paths.key(c)) for c in COMPONENTS.values() if c.agent})
    rows += process_checks(subjects, secrets, pids)
    try:
        rows += listener_checks(_expected_listeners(values))
    except IsolationError as exc:
        rows.append(Row("process", "-", "listeners on the Switch and target ports", "checked", str(exc), "FAIL"))
    return all(r.status != "FAIL" for r in rows), rows


def format_table(rows: Sequence[Row]) -> str:
    header = Row("SECTION", "SUBJECT", "CHECK", "EXPECTED", "ACTUAL", "RESULT")
    w = {f: min(40, max(len(getattr(r, f)) for r in [header, *rows])) for f in ("section", "subject", "status",
                                                                              "expected", "actual")}
    lines = [f"{r.section:<{w['section']}}  {r.subject:<{w['subject']}}  {r.status:<{w['status']}}  "
             f"{r.expected[:40]:<{w['expected']}}  {r.actual[:40]:<{w['actual']}}  {r.check}" for r in [header, *rows]]
    failed = sum(r.status == "FAIL" for r in rows)
    skipped = sum(r.status == "SKIP" for r in rows)
    lines.append(f"{len(rows)} checks: {len(rows) - failed - skipped} passed, {failed} failed, {skipped} skipped")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------- status

def status_lines(env: Mapping[str, str] | None = None) -> list[str]:
    values = _values(env)
    lines = []
    try:
        lines.append(f"mode: {'on' if isolation_on(values) else 'off'}")
    except IsolationError as exc:
        lines.append(f"mode: error: {exc}")
    try:
        marker = load_marker(values)
        lines.append(f"marker: {marker_path(values)}" + (
            f" ({marker['state']}, repo {marker['repo']}, service home {marker['service_home']})" if marker
            else " (none)"))
    except IsolationError as exc:
        lines.append(f"marker: error: {exc}")
    lines.append("components:")
    for comp in COMPONENTS.values():
        pw = _lookup(pwd.getpwnam, comp.user)
        user = f"{comp.user} uid {pw.pw_uid}" if pw else f"{comp.user} (no such user)"
        pid = live_pid(comp, values)
        if pid is None:
            proc = "not running"
        else:
            uids = (_proc_status(pid) or {}).get("Uid", [])
            isolated = pw is not None and uids == [pw.pw_uid] * 4
            proc = f"pid {pid} uid {uids[1] if uids else '?'}" + (" (isolated)" if isolated else " (NOT isolated)")
        lines.append(f"  {comp.name:<13} {user:<28} {proc}")
    return lines


# ---------------------------------------------------------------------------------------------------------- CLI

USAGE = ("usage: python -m agents.isolation mode [--run-dir DIR] | exec <component> -- <python> -m <module> [args…] | "
         "setup [--service-home DIR] | disable [--purge] | reset | clean | verify | status")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "mode" and (not rest or (len(rest) == 2 and rest[0] == "--run-dir")):
            on = isolation_on()
            if on and os.geteuid() != 0:
                raise IsolationError(f"isolation is on ({marker_path()}) but this launcher runs as uid "
                                     f"{os.geteuid()}, not root")
            if on and rest:
                ensure_run_dir(Path(os.path.abspath(rest[1])))
            print("on" if on else "off")
            return 0
        if cmd == "exec":
            if len(rest) < 3 or rest[1] != "--":
                print(USAGE, file=sys.stderr)
                return 2
            exec_component(rest[0], rest[2:])
        if cmd == "setup" and (not rest or (len(rest) == 2 and rest[0] == "--service-home")):
            for line in setup(service_home=rest[1] if rest else None):
                print(line)
            print("isolation is ready: restart the agents and targets to run them as their users")
            return 0
        if cmd == "disable" and rest in ([], ["--purge"]):
            for line in disable(purge=bool(rest)):
                print(line)
            return 0
        if cmd == "reset" and not rest:
            if not isolation_on():
                print("isolation is off: nothing to reset")
                return 0
            _require_root(cmd)
            for line in reset(_ready_paths(os.environ)):
                print(line)
            return 0
        if cmd == "clean" and not rest:
            # whatever the mode: a service area that exists is emptied (no key copy outlives isolation)
            paths = clean_target(os.environ)
            if paths is None:
                print("no service area: nothing to clean")
                return 0
            _require_root(cmd)
            _refuse_while_running("clean", None)
            for line in clean(paths):
                print(line)
            return 0
        if cmd == "verify" and not rest:
            ok, rows = verify()
            print(format_table(rows))
            return 0 if ok else 1
        if cmd == "status" and not rest:
            print("\n".join(status_lines()))
            return 0
    except IsolationError as exc:
        print(f"isolation: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"isolation: error: {exc}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
