"""Strangler-pattern reverse proxy for the Open Library migration demo.

Run with::

    python -m targets.proxy.router --host 127.0.0.1 --port 9000

The router forwards every non-admin request to either the legacy web.py
service or the modern FastAPI service.  The selected target is held in memory
and persisted to an atomically replaced JSON state file (``ROUTER_STATE_FILE``, default
``<STRANGLER_STATE_DIR or .state>/proxy/router.json``), so a restart keeps the last cutover
decision.

Admin endpoints (never forwarded)::

    GET  /_router/status
    POST /_router/flip     (requires ``X-Router-Token``)
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Sequence, cast

import httpx
import uvicorn

from targets.common.router_token import resolve_router_token

logger = logging.getLogger(__name__)

Target = Literal["legacy", "modern"]
TARGETS: frozenset[str] = frozenset({"legacy", "modern"})

DEFAULT_LEGACY_URL = "http://127.0.0.1:9001"
DEFAULT_MODERN_URL = "http://127.0.0.1:9002"
DEFAULT_PORT = 9000
DEFAULT_HOST = "127.0.0.1"

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_state_file(env: Mapping[str, str] | None = None) -> Path:
    """The routing state file when ``ROUTER_STATE_FILE`` is unset:
    ``<STRANGLER_STATE_DIR or <repo>/.state>/proxy/router.json`` (the proxy's private state directory)."""
    values = os.environ if env is None else env
    state_dir = (values.get("STRANGLER_STATE_DIR") or "").strip() or str(REPO_ROOT / ".state")
    return Path(state_dir).expanduser() / "proxy" / "router.json"


UPSTREAM_TIMEOUT = httpx.Timeout(30.0)

STATUS_PATH = "/_router/status"
FLIP_PATH = "/_router/flip"

# RFC 7230 section 6.1 hop-by-hop headers, plus commonly seen legacy aliases.
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Request headers that must not be replayed verbatim.  ``host`` belongs to the
# hop between client and router; ``content-length`` is recomputed by httpx for
# the buffered request body.
REQUEST_EXCLUDED_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {"host", "content-length"}

# Response headers that must not be replayed verbatim.  The body is forwarded as the raw upstream
# bytes (still content-encoded), so Content-Encoding is kept and Content-Length is recomputed.
RESPONSE_EXCLUDED_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {
    "content-length",
    "x-strangler-upstream",
}

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class ClientDisconnected(Exception):
    """The client went away before its request body was fully received."""


@dataclass(slots=True)
class RouterState:
    """Mutable cutover state shared by the admin API and the forwarding path."""

    target: Target = "legacy"
    flipped_at: str | None = None
    approved_by: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON representation documented in SPEC §6."""
        return {
            "target": self.target,
            "flipped_at": self.flipped_at,
            "approved_by": self.approved_by,
            "evidence": self.evidence,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RouterState":
        """Build state from a persisted JSON object, tolerating bad fields."""
        target = data.get("target")
        if not isinstance(target, str) or target not in TARGETS:
            logger.warning("Ignoring invalid persisted target %r; using legacy", target)
            target = "legacy"

        def optional_str(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        return cls(
            target=cast(Target, target),
            flipped_at=optional_str("flipped_at"),
            approved_by=optional_str("approved_by"),
            evidence=optional_str("evidence"),
        )


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Runtime configuration, read once from the environment."""

    legacy_url: str
    modern_url: str
    state_file: Path
    admin_token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RouterConfig":
        """Read configuration from ``env`` (defaults to ``os.environ``)."""
        values = os.environ if env is None else env
        legacy_url = (values.get("LEGACY_URL") or DEFAULT_LEGACY_URL).rstrip("/")
        modern_url = (values.get("MODERN_URL") or DEFAULT_MODERN_URL).rstrip("/")
        state_file = Path(values.get("ROUTER_STATE_FILE") or default_state_file(values)).expanduser()
        # No public default secret, and the SAME rule as @cutover-gate (targets/common/router_token.py).
        admin_token = resolve_router_token(values)
        return cls(
            legacy_url=legacy_url,
            modern_url=modern_url,
            state_file=state_file,
            admin_token=admin_token,
        )

    def upstream_url(self, target: Target) -> str:
        """Return the base URL for ``target``."""
        return self.legacy_url if target == "legacy" else self.modern_url


class StateStore:
    """Load and atomically persist :class:`RouterState`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RouterState:
        """Load state from disk, falling back to the legacy default."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("Router state file %s not found; defaulting to legacy", self.path)
            return RouterState()
        except OSError:
            logger.warning("Could not read router state file %s; defaulting to legacy", self.path)
            return RouterState()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Router state file %s is not valid JSON; defaulting to legacy",
                self.path,
            )
            return RouterState()

        if not isinstance(data, dict):
            logger.warning(
                "Router state file %s is not a JSON object; defaulting to legacy",
                self.path,
            )
            return RouterState()

        return RouterState.from_mapping(data)

    def save(self, state: RouterState) -> None:
        """Write ``state`` atomically (same-directory temp file + ``os.replace``)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state.as_dict(), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def _decode_headers(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    """Decode raw ASGI headers using the HTTP latin-1 convention."""
    return [(name.decode("latin-1"), value.decode("latin-1")) for name, value in headers]


def _connection_tokens(headers: Iterable[tuple[str, str]]) -> set[str]:
    """Return connection-nominated hop-by-hop header names."""
    tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            tokens.update(part.strip().lower() for part in value.split(",") if part.strip())
    return tokens


def _forward_request_headers(raw_headers: Sequence[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Strip hop-by-hop and host headers from an incoming request, keeping the raw value bytes
    (a valid latin-1 byte such as 0xE9 must not turn into an ASCII encoding error)."""
    decoded = [(name.decode("latin-1").lower(), value.decode("latin-1")) for name, value in raw_headers]
    excluded = set(REQUEST_EXCLUDED_HEADERS) | _connection_tokens(decoded)
    return [(name.lower(), value) for name, value in raw_headers
            if name.decode("latin-1").lower() not in excluded]


def _forward_response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """Strip hop-by-hop/content headers from an upstream response."""
    decoded = [(name.lower(), value) for name, value in headers]
    excluded = set(RESPONSE_EXCLUDED_HEADERS) | _connection_tokens(decoded)
    return [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in decoded
        if name not in excluded
    ]


def _request_header(scope: Scope, wanted: str) -> str | None:
    """Return the first value of ``wanted`` from an ASGI scope."""
    needle = wanted.lower().encode("latin-1")
    for name, value in scope.get("headers", []):
        if name.lower() == needle:
            return value.decode("latin-1")
    return None


def _target_url(base: str, scope: Scope) -> str:
    """Build the upstream URL, preserving the raw path and query string."""
    raw_path = scope.get("raw_path")
    if raw_path:
        path = raw_path.decode("latin-1")
    else:
        path = scope.get("path") or "/"

    query = scope.get("query_string", b"")
    url = f"{base}{path}"
    if query:
        url = f"{url}?{query.decode('latin-1')}"
    return url


async def _read_request_body(receive: Receive) -> bytes:
    """Buffer the complete request body from the ASGI server."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ClientDisconnected
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _send_json(
    send: Send,
    status: int,
    payload: Mapping[str, Any],
    *,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
    omit_body: bool = False,
) -> None:
    """Send a JSON response with an accurate content length."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": b"" if omit_body else body})


class StranglerRouter:
    """ASGI application implementing the strangler cutover proxy."""

    def __init__(self, config: RouterConfig | None = None) -> None:
        self.config = config or RouterConfig.from_env()
        self._store = StateStore(self.config.state_file)
        self._state = self._store.load()
        self._client: httpx.AsyncClient | None = None

    @property
    def state(self) -> RouterState:
        """The current in-memory cutover state."""
        return self._state

    def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared upstream client, creating it if lifespan was skipped."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=UPSTREAM_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise ValueError(f"Unsupported ASGI scope type: {scope['type']!r}")

        if await self._handle_admin(scope, receive, send):
            return

        await self._forward(scope, receive, send)

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        """Manage the shared ``httpx.AsyncClient`` across the app lifetime."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    self._ensure_client()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Router startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                else:
                    logger.info(
                        "Router started: legacy=%s modern=%s target=%s state_file=%s",
                        self.config.legacy_url,
                        self.config.modern_url,
                        self._state.target,
                        self.config.state_file,
                    )
                    await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                client, self._client = self._client, None
                if client is not None:
                    await client.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _handle_admin(self, scope: Scope, receive: Receive, send: Send) -> bool:
        """Handle router admin endpoints.  Return ``True`` if handled."""
        path = scope.get("path", "")
        if path not in (STATUS_PATH, FLIP_PATH):
            return False

        method = scope.get("method", "GET")

        if path == STATUS_PATH:
            if method not in ("GET", "HEAD"):
                await _send_json(
                    send,
                    405,
                    {"error": "method_not_allowed"},
                    extra_headers=[(b"allow", b"GET, HEAD")],
                )
                return True
            payload = {
                **self._state.as_dict(),
                "upstream_url": self.config.upstream_url(self._state.target),
            }
            await _send_json(send, 200, payload, omit_body=method == "HEAD")
            return True

        if method != "POST":
            await _send_json(
                send,
                405,
                {"error": "method_not_allowed"},
                extra_headers=[(b"allow", b"POST")],
            )
            return True

        token = _request_header(scope, "x-router-token") or ""
        if not hmac.compare_digest(token.encode("utf-8"), self.config.admin_token.encode("utf-8")):
            await _send_json(send, 403, {"error": "forbidden"})
            return True

        try:
            body = await _read_request_body(receive)
        except ClientDisconnected:
            return True

        try:
            data = json.loads(body or b"{}")
        except (json.JSONDecodeError, ValueError):
            await _send_json(send, 400, {"error": "invalid_json"})
            return True

        if not isinstance(data, dict):
            await _send_json(send, 400, {"error": "invalid_body"})
            return True

        target = data.get("target")
        if not isinstance(target, str) or target not in TARGETS:
            await _send_json(
                send,
                400,
                {"error": "invalid_target", "allowed": sorted(TARGETS)},
            )
            return True

        approved_by = data.get("approved_by")
        evidence = data.get("evidence")
        # A cutover is an audited governance action: who approved it and on what evidence are required.
        if not (isinstance(approved_by, str) and approved_by.strip() and isinstance(evidence, str) and evidence.strip()):
            await _send_json(send, 400, {"error": "approved_by_and_evidence_required"})
            return True

        state = RouterState(
            target=cast(Target, target),
            flipped_at=datetime.now(timezone.utc).isoformat(),
            approved_by=cast(str | None, approved_by),
            evidence=cast(str | None, evidence),
        )
        try:
            self._store.save(state)
        except OSError:
            logger.exception("Failed to persist router state to %s", self.config.state_file)
            await _send_json(send, 500, {"error": "state_persist_failed"})
            return True

        self._state = state
        logger.info(
            "Router flipped to %s (approved_by=%s, evidence=%s)",
            state.target,
            state.approved_by,
            state.evidence,
        )
        await _send_json(send, 200, state.as_dict())
        return True

    async def _forward(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward an arbitrary request to the currently selected upstream."""
        target = self._state.target
        upstream = self.config.upstream_url(target)
        url = _target_url(upstream, scope)
        method = scope.get("method", "GET").upper()
        headers = _forward_request_headers(scope.get("headers", []))

        try:
            body = await _read_request_body(receive)
        except ClientDisconnected:
            logger.info("Client disconnected while sending %s %s", method, url)
            return

        client = self._ensure_client()
        try:
            # Built directly (not client.build_request) so the shared client's cookie jar is never
            # merged into a caller's request: a Set-Cookie answered to one caller must not ride along
            # on the next caller's request, nor cross from one upstream port to the other. The
            # caller's own cookies travel verbatim in its Cookie header.
            request = httpx.Request(method, url, headers=headers, content=body if body else None,
                                    extensions={"timeout": UPSTREAM_TIMEOUT.as_dict()})
            response = await client.send(request, stream=True)
            try:
                raw = b"".join([chunk async for chunk in response.aiter_raw()])
            finally:
                await response.aclose()
                client.cookies.clear()
        except httpx.HTTPError as exc:
            logger.warning("Upstream %s unavailable for %s %s: %s", target, method, url, exc)
            await _send_json(send, 502, {"error": "upstream_unavailable"})
            return

        response_headers = _forward_response_headers(response.headers.multi_items())
        response_headers.append((b"x-strangler-upstream", target.encode("ascii")))
        if method != "HEAD":
            response_headers.append((b"content-length", str(len(raw)).encode("ascii")))

        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": response_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if method == "HEAD" else raw,
            }
        )


_app: StranglerRouter | None = None


def get_app() -> StranglerRouter:
    """The process-wide router, built on first use rather than at import: building it reads the environment,
    creates the state directory and may generate the router token, so merely importing this module (tests,
    tools) must not touch ``.state``."""
    global _app
    if _app is None:
        _app = StranglerRouter()
    return _app


def __getattr__(name: str) -> Any:
    """Module attribute ``app`` (``uvicorn targets.proxy.router:app``), built lazily by :func:`get_app`."""
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Strangler reverse proxy (port 9000)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: %(default)s)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="bind port (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ASGI app with uvicorn."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(get_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
