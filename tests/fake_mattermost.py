"""In-process fake of the Mattermost REST v4 endpoints used by ``agents.mattermost.MattermostClient``.

A small threaded HTTP server (port 0 — never a fixed port) that emulates exactly what the e2e demo
driver needs, including the real-server quirks a client must cope with:

* ``POST /api/v4/users/login {login_id, password}`` returns the user and the session token in the
  ``Token`` **response header** (not the body). Every other route needs ``Authorization: Bearer
  <token>``; without it -> 401 with Mattermost's JSON error shape
  ``{"id", "message", "detailed_error", "request_id", "status_code"}``.
* ``GET /api/v4/users/me``, ``GET /api/v4/users/username/{name}``, ``POST /api/v4/users/ids`` (a JSON
  list of ids -> list of users; unknown ids are silently omitted, as on the real server).
* ``GET /api/v4/teams/name/{team}``, ``GET /api/v4/channels/{id}``.
* ``GET /api/v4/channels/{id}/posts?since=<ms>`` -> ``{"order": [...newest first], "posts": {id: post}}``
  with the real ``since`` semantics: every post whose ``update_at`` is newer than ``since`` — so
  posts *edited* after ``since`` (created earlier), and deleted posts (``delete_at > 0``, empty
  message) are included — **plus the root post of every returned reply**, however old. Without
  ``since`` it pages with ``page``/``per_page``.
* ``POST /api/v4/posts {channel_id, message, root_id?}`` -> 201 post authored by the session user.

Test hooks: ``add_user``/``add_team``/``add_channel``/``add_post``/``edit_post``/``delete_post`` mutate the
state directly (``add_post`` is how a test plays a bot); ``on_post(fake, post)`` is called synchronously
after a post made through the API is stored (use it to script bot replies); ``requests`` logs every
``(method, path)`` served; ``omit_token_header`` simulates a broken login response.
``create_at`` is a strictly increasing millisecond clock, so ordering is deterministic.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

API = "/api/v4"


def new_id() -> str:
    """A 26-character lowercase id, like Mattermost's."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(26))


class FakeMattermost:
    """Threaded fake Mattermost server. Use as a context manager or call ``start()``/``stop()``."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users: dict[str, dict] = {}
        self.passwords: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.teams: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.posts: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.on_post: Callable[["FakeMattermost", dict], None] | None = None
        self.omit_token_header = False
        self._clock = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FakeMattermost":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-mattermost", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "FakeMattermost":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("fake Mattermost is not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ------------------------------------------------------------------ state helpers
    def now_ms(self) -> int:
        """Strictly increasing wall-clock milliseconds."""
        with self.lock:
            self._clock = max(int(time.time() * 1000), self._clock + 1)
            return self._clock

    def add_user(self, username: str, password: str | None = None, *, is_bot: bool = False) -> dict:
        with self.lock:
            user = {"id": new_id(), "username": username, "is_bot": is_bot, "first_name": "", "last_name": "",
                    "nickname": "", "email": f"{username}@example.test", "roles": "system_user",
                    "create_at": self.now_ms(), "update_at": self._clock, "delete_at": 0}
            self.users[user["id"]] = user
            if password is not None:
                self.passwords[username] = password
            return dict(user)

    def user_id(self, username: str) -> str:
        with self.lock:
            for u in self.users.values():
                if u["username"] == username:
                    return u["id"]
        raise KeyError(f"no fake user {username!r}")

    def add_team(self, name: str, display_name: str | None = None) -> dict:
        with self.lock:
            team = {"id": new_id(), "name": name, "display_name": display_name or name.title(), "type": "O"}
            self.teams[team["id"]] = team
            return dict(team)

    def add_channel(self, team_id: str, name: str, display_name: str | None = None) -> dict:
        with self.lock:
            ch = {"id": new_id(), "team_id": team_id, "name": name, "display_name": display_name or name,
                  "type": "O", "create_at": self.now_ms(), "delete_at": 0}
            self.channels[ch["id"]] = ch
            return dict(ch)

    def add_post(self, channel_id: str, author: str, message: str, *, root_id: str = "", type: str = "",
                 create_at: int | None = None) -> dict:
        """Store a post by ``author`` (a username or a user id) — how tests play bots."""
        with self.lock:
            user_id = author if author in self.users else self.user_id(author)
            if channel_id not in self.channels:
                raise KeyError(f"no fake channel {channel_id!r}")
            ts = self.now_ms() if create_at is None else create_at
            post = {"id": new_id(), "channel_id": channel_id, "user_id": user_id, "message": message,
                    "root_id": root_id, "type": type, "create_at": ts, "update_at": ts, "edit_at": 0,
                    "delete_at": 0, "props": {}, "hashtags": "", "pending_post_id": "", "metadata": {}}
            self.posts[post["id"]] = post
            return dict(post)

    def edit_post(self, post_id: str, message: str) -> dict:
        with self.lock:
            post = self.posts[post_id]
            ts = self.now_ms()
            post.update(message=message, edit_at=ts, update_at=ts)
            return dict(post)

    def delete_post(self, post_id: str) -> None:
        with self.lock:
            post = self.posts[post_id]
            ts = self.now_ms()
            post.update(message="", delete_at=ts, update_at=ts)

    def count(self, method: str, path_prefix: str) -> int:
        """How many requests matched ``method`` and a path starting with ``path_prefix`` (no /api/v4)."""
        with self.lock:
            return sum(1 for m, p in self.requests if m == method and p.startswith(API + path_prefix))

    def messages(self, channel_id: str) -> list[tuple[str, str]]:
        """``(username, message)`` of the live posts in ``channel_id``, oldest first."""
        with self.lock:
            live = sorted((p for p in self.posts.values() if p["channel_id"] == channel_id and not p["delete_at"]),
                          key=lambda p: p["create_at"])
            return [(self.users[p["user_id"]]["username"], p["message"]) for p in live]

    # ------------------------------------------------------------------ request handling
    def _posts_since(self, channel_id: str, since: int) -> dict:
        with self.lock:
            hits = {p["id"]: dict(p) for p in self.posts.values()
                    if p["channel_id"] == channel_id and p["update_at"] > since}
            for p in list(hits.values()):  # the real server also returns each reply's thread root
                root = self.posts.get(p["root_id"]) if p["root_id"] else None
                if root is not None:
                    hits.setdefault(root["id"], dict(root))
            order = sorted(hits, key=lambda i: hits[i]["create_at"], reverse=True)
            return {"order": order, "posts": hits, "next_post_id": "", "prev_post_id": ""}

    def _posts_page(self, channel_id: str, page: int, per_page: int) -> dict:
        with self.lock:
            live = sorted((dict(p) for p in self.posts.values() if p["channel_id"] == channel_id),
                          key=lambda p: p["create_at"], reverse=True)[page * per_page:(page + 1) * per_page]
            return {"order": [p["id"] for p in live], "posts": {p["id"]: p for p in live},
                    "next_post_id": "", "prev_post_id": ""}

    def handle(self, method: str, raw_path: str, headers: Any, body: bytes) -> tuple[int, Any, dict]:
        """Route one request. Returns ``(status, json_body, extra_headers)``."""
        split = urlsplit(raw_path)
        path, query = split.path, parse_qs(split.query)
        with self.lock:
            self.requests.append((method, path))
        if not path.startswith(API + "/"):
            return _err(404, "api.context.404.app_error", "Sorry, we could not find the page.")
        route = path[len(API):]
        payload: Any = None
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                return _err(400, "api.context.invalid_body_param.app_error", "Invalid or missing body in request.")

        if method == "POST" and route == "/users/login":
            if not isinstance(payload, dict):
                return _err(400, "api.context.invalid_body_param.app_error", "Invalid or missing login body.")
            username, password = payload.get("login_id"), payload.get("password")
            with self.lock:
                ok = username in self.passwords and self.passwords[username] == password
                if not ok:
                    return _err(401, "api.user.login.invalid_credentials_email_username",
                                "Enter a valid email or username and/or password.")
                user_id = self.user_id(username)
                token = new_id()
                self.tokens[token] = user_id
                user = dict(self.users[user_id])
            return 200, user, ({} if self.omit_token_header else {"Token": token})

        auth = headers.get("Authorization") or ""
        with self.lock:
            me = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if me is None:
            return _err(401, "api.context.session_expired.app_error", "Invalid or expired session, please login again.")

        parts = [unquote(p) for p in route.strip("/").split("/")]
        with self.lock:
            if method == "GET" and parts == ["users", "me"]:
                return 200, dict(self.users[me]), {}
            if method == "GET" and len(parts) == 3 and parts[:2] == ["users", "username"]:
                found = [u for u in self.users.values() if u["username"] == parts[2]]
                if not found:
                    return _err(404, "app.user.missing_account.const", "We couldn't find an existing account.")
                return 200, dict(found[0]), {}
            if method == "POST" and parts == ["users", "ids"]:
                if not isinstance(payload, list) or not all(isinstance(i, str) for i in payload):
                    return _err(400, "api.context.invalid_body_param.app_error", "Invalid or missing user_ids.")
                return 200, [dict(self.users[i]) for i in dict.fromkeys(payload) if i in self.users], {}
            if method == "GET" and len(parts) == 3 and parts[:2] == ["teams", "name"]:
                found = [t for t in self.teams.values() if t["name"] == parts[2]]
                if not found:
                    return _err(404, "app.team.get_by_name.missing.app_error", "Unable to find the existing team.")
                return 200, dict(found[0]), {}
            if method == "GET" and len(parts) in (2, 3) and parts[0] == "channels":
                channel = self.channels.get(parts[1])
                if channel is None:
                    return _err(404, "app.channel.get.existing.app_error", "Unable to find the existing channel.")
                if len(parts) == 2:
                    return 200, dict(channel), {}
                if parts[2] == "posts":
                    if "since" in query:
                        try:
                            since = int(query["since"][0])
                        except ValueError:
                            return _err(400, "api.context.invalid_url_param.app_error", "Invalid since parameter.")
                        return 200, self._posts_since(channel["id"], since), {}
                    page = int(query.get("page", ["0"])[0])
                    per_page = int(query.get("per_page", ["60"])[0])
                    return 200, self._posts_page(channel["id"], page, per_page), {}
        if method == "POST" and parts == ["posts"]:
            if not isinstance(payload, dict) or not payload.get("channel_id"):
                return _err(400, "api.context.invalid_body_param.app_error", "Invalid or missing post in request body.")
            if not (payload.get("message") or "").strip():
                return _err(400, "model.post.is_valid.msg.app_error", "Invalid Message.")
            if payload["channel_id"] not in self.channels:
                return _err(403, "api.context.permissions.app_error", "You do not have the appropriate permissions.")
            post = self.add_post(payload["channel_id"], me, payload["message"], root_id=payload.get("root_id") or "")
            hook = self.on_post
            if hook is not None:
                hook(self, post)
            return 201, post, {}
        return _err(404, "api.context.404.app_error", f"no fake route for {method} {route}")


def _err(status: int, err_id: str, message: str) -> tuple[int, dict, dict]:
    return status, {"id": err_id, "message": message, "detailed_error": "", "request_id": new_id(),
                    "status_code": status}, {}


def _make_handler(fake: FakeMattermost) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                status, data, extra = fake.handle(self.command, self.path, self.headers, body)
            except Exception as exc:  # noqa: BLE001 - surface fake bugs as a 500 the test will see
                status, data, extra = _err(500, "fake.internal", f"{type(exc).__name__}: {exc}")
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, *args: Any) -> None:
            pass

    return Handler
