"""Synchronous Mattermost REST v4 client for the end-to-end demo driver.

Login credentials yield a token in the Token response header. Mattermost's
``since`` endpoint also returns older thread roots, older posts edited later,
and deleted posts, so results are filtered by creation time and post status.
Author usernames are resolved in batches and cached for this client instance.
HTTP errors and incomplete author lookups propagate without retries.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx


class MattermostError(RuntimeError):
    """An unsuccessful HTTP response or an unusable Mattermost response.

    ``body`` preserves the raw response text, including for protocol errors
    reported with a successful HTTP status.
    """

    def __init__(
        self,
        status: int,
        method: str,
        url: str,
        body: str,
        *,
        detail: str | None = None,
    ) -> None:
        self.status = status
        self.method = method
        self.url = url
        self.body = body

        try:
            data = json.loads(body)
        except ValueError:
            data = None
        message = data.get("message") if isinstance(data, dict) else None
        description = str(message) if message else body
        if detail:
            description = f"{detail}; {description}" if description else detail
        super().__init__(
            f"Mattermost {method} {url} returned {status}: {description}"
        )


def _error(
    response: httpx.Response, detail: str | None = None
) -> MattermostError:
    """Build an error retaining the original request and response details."""
    return MattermostError(
        response.status_code,
        response.request.method,
        str(response.request.url),
        response.text,
        detail=detail,
    )


class MattermostClient:
    """Own one reusable HTTP client connected to a Mattermost server root."""

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v4",
            timeout=timeout,
            follow_redirects=False,
        )
        self._usernames: dict[str, str] = {}

    def __enter__(self) -> MattermostClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, int] | None = None,
        payload: Any = None,
    ) -> httpx.Response:
        """Send a request, rejecting every status outside the 2xx range."""
        response = self._client.request(
            method, path, params=params, json=payload
        )
        if not 200 <= response.status_code < 300:
            raise _error(response)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise _error(response, "response is not valid JSON") from exc

    @classmethod
    def _object(cls, response: httpx.Response) -> dict[str, Any]:
        data = cls._json(response)
        if not isinstance(data, dict):
            raise _error(response, "expected a JSON object")
        return data

    def login(self, user: str, password: str) -> dict[str, Any]:
        """Authenticate and use the Token response header for later requests."""
        response = self._request(
            "POST",
            "/users/login",
            payload={"login_id": user, "password": password},
        )
        token = response.headers.get("Token")
        if not token or not token.strip():
            raise _error(response, "login response lacks the Token header")
        account = self._object(response)
        self._client.headers["Authorization"] = f"Bearer {token}"
        return account

    def me(self) -> dict[str, Any]:
        """Return the authenticated user's account."""
        return self._object(self._request("GET", "/users/me"))

    def channel(self, channel_id: str) -> dict[str, Any]:
        """Return a channel by its Mattermost ID."""
        return self._object(
            self._request("GET", f"/channels/{quote(channel_id, safe='')}")
        )

    def team(self, name: str) -> dict[str, Any]:
        """Return a team by its URL name."""
        return self._object(
            self._request("GET", f"/teams/name/{quote(name, safe='')}")
        )

    def user_by_username(self, username: str) -> dict[str, Any]:
        """Return an account by its username."""
        return self._object(
            self._request(
                "GET", f"/users/username/{quote(username, safe='')}"
            )
        )

    def post(
        self, channel_id: str, message: str, root_id: str = ""
    ) -> dict[str, Any]:
        """Create a channel post, optionally replying to a thread root."""
        return self._object(
            self._request(
                "POST",
                "/posts",
                payload={
                    "channel_id": channel_id,
                    "message": message,
                    "root_id": root_id,
                },
            )
        )

    def posts_since(
        self, channel_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        """Return live, ordinary posts created strictly after ``since_ms``.

        Results are ordered by creation time and include ``username``.
        Unknown author IDs are resolved with at most one batched request.
        """
        response = self._request(
            "GET",
            f"/channels/{quote(channel_id, safe='')}/posts",
            params={"since": since_ms},
        )
        data = self._object(response)
        raw_posts = data.get("posts")
        if not isinstance(raw_posts, dict):
            raise _error(response, "expected a posts object")

        posts: list[dict[str, Any]] = []
        for raw in raw_posts.values():
            if not isinstance(raw, dict):
                raise _error(response, "expected each post to be an object")
            created = raw.get("create_at")
            deleted = raw.get("delete_at", 0)
            if not isinstance(created, int) or not isinstance(deleted, int):
                raise _error(response, "post has invalid timestamps")
            if created <= since_ms or deleted > 0 or raw.get("type"):
                continue
            if not isinstance(raw.get("user_id"), str) or not raw["user_id"]:
                raise _error(response, "post lacks a valid author ID")
            posts.append(dict(raw))

        posts.sort(key=lambda post: post["create_at"])
        missing = sorted(
            {post["user_id"] for post in posts} - self._usernames.keys()
        )
        if missing:
            users_response = self._request(
                "POST", "/users/ids", payload=missing
            )
            users = self._json(users_response)
            if not isinstance(users, list):
                raise _error(users_response, "expected a list of users")
            resolved: dict[str, str] = {}
            for user in users:
                if (
                    not isinstance(user, dict)
                    or not isinstance(user.get("id"), str)
                    or not isinstance(user.get("username"), str)
                    or not user["username"]
                ):
                    raise _error(users_response, "invalid user lookup result")
                resolved[user["id"]] = user["username"]

            # Preserve successfully resolved IDs even if the batch is incomplete.
            self._usernames.update(
                (user_id, resolved[user_id])
                for user_id in missing
                if user_id in resolved
            )
            omitted = [
                user_id for user_id in missing if user_id not in resolved
            ]
            if omitted:
                raise _error(
                    users_response,
                    "user lookup omitted requested IDs: " + ", ".join(omitted),
                )

        for post in posts:
            post["username"] = self._usernames[post["user_id"]]
        return posts

    def wait_for(
        self,
        channel_id: str,
        predicate: Callable[[dict[str, Any]], object],
        *,
        since_ms: int,
        timeout: float,
        poll: float = 1.0,
    ) -> dict[str, Any]:
        """Poll for the earliest matching post, retaining timeout diagnostics.

        The original creation-time boundary is used on every poll. HTTP and
        predicate errors propagate immediately. The deadline uses a monotonic
        clock; an in-flight HTTP request remains subject to the client timeout.
        """
        if poll < 0:
            raise ValueError("poll must be non-negative")
        deadline = time.monotonic() + timeout
        recent: dict[str, dict[str, Any]] = {}

        while True:  # always poll at least once, even with timeout <= 0
            posts = self.posts_since(channel_id, since_ms)
            recent.update((post["id"], post) for post in posts)
            latest = sorted(
                recent.values(), key=lambda post: post["create_at"]
            )[-10:]
            recent = {post["id"]: post for post in latest}

            for post in posts:
                if predicate(post):
                    return post

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll, remaining))

        descriptions = [
            f"{post['username']}: {' '.join(post['message'].split())[:200]}"
            for post in recent.values()
        ]
        raise TimeoutError(
            f"no post matched within {timeout}s in channel {channel_id}; "
            f"last posts seen: {'; '.join(descriptions) or '(none)'}"
        )
