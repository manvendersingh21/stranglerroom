"""Fault-tolerant client for the Switch 0.31.0 Agent Bridge and Gateway APIs.

Covers everything StranglerRoom's agents need, verified against the switch-v0.31.0 source
(see docs/SWITCH_PROTOCOL.md for file:line citations):

* agent registration           ``POST /agents`` (bootstrap token = ``AGENT_REGISTRATION_TOKEN``)
* addressing-policy unlock     ``POST /gateway/auth/login`` + ``PUT /gateway/agents/{id}/addressing-policy``
* the SSE event stream          ``GET /agents/{id}/events?connection_id=…&scope=single&filter=addressed&rooms=…``
* the 2-second heartbeat        ``POST /agents/{id}/connection/beat`` ``{connection_id, generation}`` (6 s TTL)
* operations                    ``POST /agents/{id}/ops/{operation}`` (room-bound ops carry
                                ``X-Switch-Connection-Id`` of a live stream that claims the room)

Design notes
------------
* Threads, not asyncio: an agent's handler may run a long blocking job (a fuzz campaign or a
  load benchmark) while the stream-reader and heartbeat threads keep its connection alive.
* Each thread owns its own ``httpx.Client`` so no connection pool is shared across threads.
* The stream reconnects with the same ``connection_id``, ``expected_generation`` (so it never
  steals a taken-over connection back), ``start_from``/``Last-Event-ID`` (so nothing addressed
  while it was away is skipped) and the claimed ``rooms``. Events are de-duplicated by sequence.
* Errors are loud: every non-2xx response raises :class:`SwitchError` carrying the status and body.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import socket
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("strangler.switch")

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("STRANGLER_STATE_DIR", str(ROOT / ".state")))

DEFAULT_SWITCH_URL = "http://localhost:18000"
HEARTBEAT_INTERVAL_S = 2.0
STREAM_READ_TIMEOUT_S = 45.0  # server keepalives arrive every 15 s of idle

INTEGRATION_PROFILE: dict[str, Any] = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
    "command_capabilities": {"reset": "unsupported", "compact": "unsupported", "interrupt": "unsupported"},
}


class SwitchError(RuntimeError):
    """A Switch API call returned a non-success status (or the stream was refused)."""

    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:600]}")
        self.method, self.url, self.status, self.body = method, url, status, body

    def detail_code(self) -> str | None:
        try:
            detail = json.loads(self.body).get("detail")
        except (ValueError, AttributeError):
            return None
        return detail.get("code") if isinstance(detail, dict) else None


class ConnectionTakenOver(RuntimeError):
    """Another stream now holds this connection id. Terminal: reopening would trade it back forever."""


def _abort_stream(resp: httpx.Response | None) -> None:
    """Interrupt a reader blocked on ``resp`` from another thread.

    ``Response.close()`` alone does not wake a thread blocked in ``recv()`` (it would notice only at the
    next 15 s keepalive); shutting the socket down does, so the reader sees EOF at once and reconnects
    (or exits on shutdown)."""
    if resp is None:
        return
    stream = resp.extensions.get("network_stream")
    sock = stream.get_extra_info("socket") if stream is not None else None
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        resp.close()


def _check(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        raise SwitchError(resp.request.method, str(resp.request.url), resp.status_code, resp.text)
    return resp


# --------------------------------------------------------------------------------------- settings

def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file (comments and blank lines ignored)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@dataclass(frozen=True)
class Settings:
    """Connection settings. Process env wins over ``.state/switch.env`` (written by bootstrap_switch.sh)."""

    switch_url: str
    registration_token: str
    admin_email: str
    admin_password: str
    mattermost_url: str
    mattermost_team: str
    mattermost_user: str
    mattermost_user_password: str

    @classmethod
    def load(cls) -> "Settings":
        file_env = read_env_file(STATE_DIR / "switch.env")

        def get(key: str, default: str | None = None) -> str:
            value = os.environ.get(key) or file_env.get(key) or default
            if not value:
                raise RuntimeError(f"missing required setting {key} (set it in the environment or run "
                                   f"scripts/bootstrap_switch.sh to write .state/switch.env)")
            return value

        return cls(
            switch_url=get("SWITCH_URL", DEFAULT_SWITCH_URL).rstrip("/"),
            registration_token=get("AGENT_REGISTRATION_TOKEN"),
            admin_email=get("GATEWAY_ADMIN_EMAIL"),
            admin_password=get("GATEWAY_ADMIN_PASSWORD"),
            mattermost_url=get("MATTERMOST_URL", "http://localhost:18065").rstrip("/"),
            mattermost_team=get("MATTERMOST_TEAM_NAME", "switch"),
            mattermost_user=get("MATTERMOST_USER", "user"),
            mattermost_user_password=get("MATTERMOST_USER_PASSWORD"),
        )

    @classmethod
    def load_agent(cls) -> "Settings":
        """Settings for a room agent, which needs only ``SWITCH_URL`` (it authenticates with its own API key).

        ``SWITCH_URL`` comes from the environment, else from the public ``connection.env`` that bootstrap writes next
        to ``switch.env`` (URLs only, mode 0644), else :data:`DEFAULT_SWITCH_URL`. An agent never opens
        ``switch.env``: the registration token, the gateway admin credentials and the human's Mattermost password
        are never read into an agent process (the fields stay empty strings)."""
        url = (os.environ.get("SWITCH_URL") or "").strip()
        if not url:
            url = read_env_file(STATE_DIR / "connection.env").get("SWITCH_URL", "").strip()
        return cls(switch_url=(url or DEFAULT_SWITCH_URL).rstrip("/"), registration_token="", admin_email="",
                   admin_password="", mattermost_url="", mattermost_team="", mattermost_user="",
                   mattermost_user_password="")


# ------------------------------------------------------------------------------------ credentials

@dataclass(frozen=True)
class AgentCredentials:
    name: str
    agent_id: str
    api_key: str

    @staticmethod
    def path_for(name: str) -> Path:
        return STATE_DIR / "agents" / f"{name}.json"

    def save(self) -> None:
        """Write the key file atomically. The temporary file comes from ``mkstemp`` in the same directory, so it
        is 0600 from creation (the key is never in a file others can read) and has an unpredictable name; it is
        removed if anything fails before the rename."""
        path = self.path_for(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"name": self.name, "agent_id": self.agent_id, "api_key": self.api_key}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, name: str) -> "AgentCredentials | None":
        path = cls.path_for(name)
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        return cls(name=data["name"], agent_id=data["agent_id"], api_key=data["api_key"])


def register_agent(settings: Settings, name: str, description: str, display_name: str) -> AgentCredentials:
    """Return working credentials for ``name``: reuse the stored key if it still authenticates,
    otherwise (re-)register with ``overwrite=true``, which rotates the key of an agent we own."""
    stored = AgentCredentials.load(name)
    if stored is not None:
        probe = httpx.get(f"{settings.switch_url}/agents/{stored.agent_id}/ops",
                          headers={"Authorization": f"Bearer {stored.api_key}"}, timeout=15)
        if probe.status_code == 200:
            return stored
        if probe.status_code != 401:
            # Only a rejected key justifies re-registering (overwrite=true ROTATES the key and resets the
            # profile). A 429/5xx/409 is transient or a different problem: fail loud, keep the stored key.
            raise SwitchError("GET", str(probe.request.url), probe.status_code,
                              f"cannot verify stored credentials for {name}: {probe.text}")
        log.warning("stored credentials for %s rejected (HTTP 401); re-registering", name)
    resp = _check(httpx.post(
        f"{settings.switch_url}/agents",
        headers={"Authorization": f"Bearer {settings.registration_token}"},
        json={"name": name, "description": description, "display_name": display_name,
              "connector_type": "custom", "integration_profile": INTEGRATION_PROFILE, "overwrite": True},
        timeout=30,
    ))
    body = resp.json()
    creds = AgentCredentials(name=name, agent_id=body["id"], api_key=body["api_key"])
    creds.save()
    log.info("registered agent %s id=%s", name, creds.agent_id)
    return creds


# ---------------------------------------------------------------------------------------- gateway

class SwitchGateway:
    """Admin session on the Gateway API (cookie auth; the cookie is replayed by hand because a
    ``Secure`` cookie is dropped by conforming jars over plain http — same as Switch's setup.py)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.Client(base_url=settings.switch_url, timeout=30)
        self._login()

    def _login(self) -> None:
        resp = _check(self.http.post("/gateway/auth/login",
                                     json={"email": self.settings.admin_email, "password": self.settings.admin_password}))
        token = resp.cookies.get("switch_auth")
        if not token:
            raise RuntimeError("gateway login returned no switch_auth cookie")
        self.http.cookies.clear()
        self.http.headers["Cookie"] = f"switch_auth={token}"

    def request(self, method: str, path: str, **kw: Any) -> Any:
        resp = self.http.request(method, path, **kw)
        if resp.status_code == 401:  # token expired: log in again once
            self._login()
            resp = self.http.request(method, path, **kw)
        _check(resp)
        return resp.json() if resp.content else None

    def agents(self) -> list[dict]:
        return self.request("GET", "/gateway/agents")

    def set_addressing_policy(self, agent_id: str, policy: dict | None) -> dict:
        """``policy=None`` clears the policy: the agent becomes addressable by anyone (humans and agents)."""
        detail = self.request("PUT", f"/gateway/agents/{agent_id}/addressing-policy", json={"policy": policy})
        if policy is None and detail.get("addressing_policy") is not None:
            raise RuntimeError(f"addressing policy of {agent_id} did not clear: {detail.get('addressing_policy')}")
        return detail

    def room(self, room_id: str) -> dict:
        return self.request("GET", f"/gateway/rooms/{room_id}")

    def room_documents(self, room_id: str) -> list[dict]:
        return self.request("GET", f"/gateway/rooms/{room_id}/documents")

    def room_document(self, room_id: str, document_id: str) -> dict:
        return self.request("GET", f"/gateway/rooms/{room_id}/documents/{document_id}")

    def validate_template(self, content: str) -> dict:
        return self.request("POST", "/gateway/templates/validate", json={"content": content})

    def close(self) -> None:
        self.http.close()


# ------------------------------------------------------------------------------------------ events

@dataclass(frozen=True)
class Event:
    """One domain event from the stream (``message``, ``room_join``, ``command``)."""

    name: str
    data: dict
    sequence: int | None

    @property
    def payload(self) -> dict:
        return self.data.get("payload") or {}

    @property
    def room_id(self) -> str | None:
        return self.data.get("room_id")

    @property
    def is_message(self) -> bool:
        return self.name == "message" and self.data.get("type", "message") == "message"

    @property
    def addressed(self) -> bool:
        return bool(self.payload.get("addressed"))

    @property
    def body(self) -> str:
        return self.payload.get("body") or ""

    @property
    def sender_name(self) -> str:
        return self.payload.get("sender_name") or ""

    @property
    def message_id(self) -> str | None:
        return self.payload.get("message_id")

    @property
    def thread_id(self) -> str | None:
        return self.payload.get("thread_id")


# ------------------------------------------------------------------------------------------- agent

class SwitchAgent:
    """One registered agent: operations plus a self-healing SSE connection with heartbeats."""

    def __init__(self, settings: Settings, creds: AgentCredentials):
        self.settings = settings
        self.creds = creds
        self.connection_id = str(uuid.uuid4())
        self._auth = {"Authorization": f"Bearer {creds.api_key}"}
        self._ops = httpx.Client(base_url=settings.switch_url, headers=self._auth, timeout=60)
        self._events: queue.Queue[Event] = queue.Queue()
        self._stop = threading.Event()
        self._live = threading.Event()        # a stream is attached right now
        self._reconnect = threading.Event()   # heartbeat says: reopen the stream
        self._fatal: BaseException | None = None
        self._rooms: list[str] = []
        self._filter = "addressed"
        self._generation: int | None = None
        self._cursor: int | None = None
        self._seen: deque[int] = deque(maxlen=4096)
        self._seen_set: set[int] = set()
        self._threads: list[threading.Thread] = []
        self._stream_resp: httpx.Response | None = None

    @property
    def name(self) -> str:
        return self.creds.name

    @property
    def agent_id(self) -> str:
        return self.creds.agent_id

    # ---------------------------------------------------------------- operations
    def op(self, operation: str, args: dict | None = None, *, bound: bool = False) -> Any:
        """Run ``operation``. ``bound=True`` sends ``X-Switch-Connection-Id`` (room-bound ops).

        Retries only what is safe to retry: a 409 for a connection that is momentarily
        re-attaching, 429 (honouring Retry-After) and transport errors raised before a response."""
        headers = {"X-Switch-Connection-Id": self.connection_id} if bound else {}
        url = f"/agents/{self.agent_id}/ops/{operation}"
        delay = 0.5
        for attempt in range(8):
            if bound and not self._live.is_set():
                self._live.wait(timeout=10)
            try:
                resp = self._ops.post(url, json=args or {}, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                log.warning("%s: %s transport error (%s); retrying", self.name, operation, exc)
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if resp.status_code == 200:
                return resp.json().get("result")
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", delay)))
                continue
            if resp.status_code == 409 and bound and attempt < 7:
                log.warning("%s: %s got 409 (connection settling): %s", self.name, operation, resp.text[:200])
                self._live.wait(timeout=5)
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            _check(resp)
        raise SwitchError("POST", url, 0, f"{operation}: retries exhausted")

    def list_rooms(self, include_archived: bool = False) -> list[dict]:
        return self.op("list_rooms", {"include_archived": include_archived})

    def create_room(self, *, name: str, description: str, agent_names: list[str], user_names: list[str],
                    instructions: str, channel_type: str = "channel_public") -> dict:
        return self.op("create_room", {"name": name, "description": description, "agent_names": agent_names,
                                        "user_names": user_names, "channel_type": channel_type,
                                        "instructions": instructions})

    def archive_room(self, room_id: str) -> Any:
        return self.op("archive_room", {"room_id": room_id})

    def invite_agent(self, room_id: str, agent_name: str) -> Any:
        return self.op("invite_agent_to_room", {"room_id": room_id, "agent_name": agent_name})

    def add_users(self, room_id: str, user_names: list[str]) -> Any:
        return self.op("add_users_to_room", {"room_id": room_id, "user_names": user_names})

    def room_detail(self, room_id: str) -> dict:
        return self.op("get_room_detail", {"room_id": room_id})

    def post(self, body: str, *, thread_id: str | None = None) -> str:
        """Broadcast ``body`` into the bound room. Plain ``@name`` tokens in ``body`` address agents."""
        args: dict[str, Any] = {"body": body}
        if thread_id:
            args["thread_id"] = thread_id
        return self.op("post_message", args, bound=True).get("event_id")

    def send_targeted(self, body: str, targets: list[str], *, thread_id: str | None = None) -> dict:
        """Address ``targets`` (agent names / human usernames). Switch prepends ``@target`` itself and
        reports each agent target's liveness in ``target_statuses``."""
        args: dict[str, Any] = {"body": body, "target_names": targets}
        if thread_id:
            args["thread_id"] = thread_id
        return self.op("send_targeted_message", args, bound=True)

    def participants(self) -> list[dict]:
        return self.op("list_participants", bound=True)

    def documents(self) -> list[dict]:
        refs = self.op("list_references", bound=True)
        return list(refs.get("documents", [])) if isinstance(refs, dict) else []

    def create_document(self, *, name: str, description: str, instructions: str, content: str) -> str:
        return self.op("create_room_document", {"name": name, "description": description,
                                                 "instructions": instructions, "content": content},
                       bound=True)["document_id"]

    def update_document(self, document_id: str, *, content: str, description: str | None = None) -> None:
        args: dict[str, Any] = {"document_id": document_id, "content": content}
        if description is not None:
            args["description"] = description
        self.op("update_room_document", args, bound=True)

    def load_documents(self, ids: list[str]) -> list[dict]:
        return self.op("load_internal_documents", {"ids": ids}, bound=True)

    def upsert_document(self, *, name: str, description: str, instructions: str, content: str) -> str:
        """Create the room document ``name`` or, if this agent already created it, overwrite it."""
        for doc in self.documents():
            if doc.get("name") == name:
                if doc.get("created_by_agent_id") != self.agent_id:
                    raise RuntimeError(f"room document {name!r} exists but was created by "
                                       f"{doc.get('created_by_agent_id')}, not {self.name}")
                self.update_document(doc["id"], content=content, description=description)
                return doc["id"]
        return self.create_document(name=name, description=description, instructions=instructions, content=content)

    def set_typing(self, room_id: str, is_typing: bool) -> None:
        """Show (or clear) this agent's typing indicator in the bridged channel."""
        _check(self._ops.post(f"/agents/{self.agent_id}/typing", json={"room_id": room_id, "is_typing": is_typing}))

    def read_context(self, room_id: str, limit: int = 50, before: str | None = None) -> dict:
        """Room history (newest last). ``before`` (ISO-8601) pages further back; the result's ``truncated`` and
        ``oldest_timestamp`` (epoch ms) say whether older messages exist and where this page ends."""
        args: dict[str, Any] = {"room_id": room_id, "limit": limit}
        if before:
            args["before"] = before
        return self.op("read_context", args)

    # -------------------------------------------------------------- event stream
    def connect(self, rooms: list[str], *, delivery_filter: str = "addressed", timeout: float = 30) -> None:
        """Open the stream claiming ``rooms`` (scope=single) and start the 2 s heartbeat."""
        if self._threads:
            raise RuntimeError("already connected")
        self._rooms = list(rooms)
        self._filter = delivery_filter
        reader = threading.Thread(target=self._reader_loop, name=f"{self.name}-sse", daemon=True)
        beater = threading.Thread(target=self._beat_loop, name=f"{self.name}-beat", daemon=True)
        self._threads = [reader, beater]
        reader.start()
        if not self._live.wait(timeout):
            self.close()
            raise RuntimeError(f"{self.name}: event stream did not open within {timeout}s: {self._fatal}")
        beater.start()
        log.info("%s connected connection=%s rooms=%s generation=%s", self.name, self.connection_id,
                 self._rooms, self._generation)

    def next_event(self, timeout: float | None = None) -> Event | None:
        """Block for the next domain event; ``None`` on timeout. Raises if the connection died for good."""
        if self._fatal is not None and self._events.empty():
            raise RuntimeError(f"{self.name}: connection lost permanently") from self._fatal
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            if self._fatal is not None:
                raise RuntimeError(f"{self.name}: connection lost permanently") from self._fatal
            return None

    @property
    def is_live(self) -> bool:
        return self._live.is_set()

    def close(self) -> None:
        self._stop.set()
        _abort_stream(self._stream_resp)
        for t in self._threads:
            if t is not threading.current_thread() and t.ident is not None:  # never-started -> nothing to join
                t.join(timeout=5)
        self._ops.close()

    # ------------------------------------------------------------------ internals
    def _stream_params(self) -> tuple[dict[str, str], dict[str, str]]:
        params = {"connection_id": self.connection_id, "scope": "single", "filter": self._filter,
                  "rooms": ",".join(self._rooms), "client": "stranglerroom", "client_version": "1.0"}
        headers = {**self._auth, "Accept": "text/event-stream"}
        if self._cursor is not None:
            params["start_from"] = str(self._cursor)
            headers["Last-Event-ID"] = str(self._cursor)
        if self._generation is not None:
            params["expected_generation"] = str(self._generation)
        return params, headers

    def _reader_loop(self) -> None:
        backoff = 1.0
        with httpx.Client(base_url=self.settings.switch_url,
                          timeout=httpx.Timeout(15, read=STREAM_READ_TIMEOUT_S)) as http:
            while not self._stop.is_set():
                self._reconnect.clear()
                params, headers = self._stream_params()
                try:
                    with http.stream("GET", f"/agents/{self.agent_id}/events", params=params, headers=headers) as resp:
                        self._stream_resp = resp
                        if resp.status_code != 200:
                            body = resp.read().decode(errors="replace")
                            err = SwitchError("GET", str(resp.request.url), resp.status_code, body)
                            if resp.status_code == 409 and "taken_over" in body:
                                raise ConnectionTakenOver(str(err))
                            if resp.status_code in (400, 401, 403):
                                raise err  # misconfiguration: retrying cannot help
                            raise httpx.TransportError(str(err))
                        backoff = 1.0
                        self._consume(resp)
                except ConnectionTakenOver as exc:
                    log.error("%s: %s", self.name, exc)
                    self._fatal = exc
                    self._live.clear()
                    return
                except SwitchError as exc:
                    log.error("%s: stream refused: %s", self.name, exc)
                    self._fatal = exc
                    self._live.clear()
                    return
                except (httpx.HTTPError, ValueError) as exc:
                    if self._stop.is_set():
                        return
                    log.warning("%s: stream dropped (%s); reconnecting in %.0fs", self.name, exc, backoff)
                finally:
                    self._stream_resp = None
                self._live.clear()
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    def _consume(self, resp: httpx.Response) -> None:
        event_name, event_id, data_lines = "message", None, []
        for line in resp.iter_lines():
            if self._stop.is_set() or self._reconnect.is_set():
                return
            if line == "":
                if data_lines:
                    self._dispatch(event_name, event_id, "\n".join(data_lines))
                event_name, event_id, data_lines = "message", None, []
            elif line.startswith(":"):
                continue  # keepalive comment
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

    def _dispatch(self, name: str, event_id: str | None, raw: str) -> None:
        data = json.loads(raw)
        if name == "connection_state":
            self._generation = data.get("generation")
            self._live.set()
            return
        if name == "evicted":
            code = data.get("code")
            if code == "taken_over":
                raise ConnectionTakenOver(f"evicted: {data}")
            raise httpx.TransportError(f"evicted ({code}): {data.get('reason')}")
        if name == "gap":
            log.warning("%s: stream gap %s", self.name, data)
            return
        if name in ("subscription_changed", "room_released", "approval_outcome", "session_command"):
            return
        seq = int(event_id) if event_id is not None else data.get("sequence")
        if seq is not None:
            if seq in self._seen_set:
                return
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(seq)
            self._seen_set.add(seq)
            self._cursor = seq
        self._events.put(Event(name=name, data=data, sequence=seq))

    def _beat_loop(self) -> None:
        with httpx.Client(base_url=self.settings.switch_url, headers=self._auth, timeout=10) as http:
            while not self._stop.wait(HEARTBEAT_INTERVAL_S):
                if not self._live.is_set():
                    continue
                try:
                    resp = http.post(f"/agents/{self.agent_id}/connection/beat",
                                     json={"connection_id": self.connection_id, "generation": self._generation})
                except httpx.HTTPError as exc:
                    log.warning("%s: heartbeat failed (%s)", self.name, exc)
                    continue
                if resp.status_code == 200:
                    continue
                code = None
                try:
                    detail = resp.json().get("detail")
                    code = detail.get("code") if isinstance(detail, dict) else None
                except ValueError:
                    pass
                if resp.status_code == 409 and code == "taken_over":
                    log.error("%s: heartbeat says connection was taken over; standing down", self.name)
                    self._fatal = ConnectionTakenOver(resp.text)
                    self._live.clear()
                    self._stop.set()
                    return
                log.warning("%s: heartbeat HTTP %s (%s); reopening stream", self.name, resp.status_code, code)
                self._live.clear()
                self._reconnect.set()
                _abort_stream(self._stream_resp)  # wake the reader now, not at the next keepalive
