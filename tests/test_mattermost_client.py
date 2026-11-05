"""Contract for ``agents/mattermost.py`` — the Mattermost REST v4 client the e2e demo driver uses.

API (``from agents.mattermost import MattermostClient, MattermostError``):

* ``MattermostClient(base_url, *, timeout=15.0)`` — ``base_url`` is the server root (e.g.
  ``http://localhost:18065``, trailing slash allowed); requests go to ``<base_url>/api/v4/...`` through
  one ``httpx.Client``. Usable as a context manager; ``close()`` closes the HTTP client.
* ``MattermostError(RuntimeError)`` — raised for EVERY non-2xx response, with attributes ``status``
  (int), ``method``, ``url`` and ``body`` (raw text); ``str()`` contains the status and the server's
  error message.
* ``login(user, password) -> dict`` — ``POST /users/login {"login_id", "password"}``; the session token
  comes from the ``Token`` RESPONSE HEADER and is sent as ``Authorization: Bearer <token>`` on every
  later call. A 2xx without a ``Token`` header raises ``MattermostError`` (status = the response's).
  Returns the logged-in user.
* ``me() -> dict`` (``GET /users/me``), ``channel(channel_id) -> dict`` (``GET /channels/{id}``),
  ``team(name) -> dict`` (``GET /teams/name/{name}``), ``user_by_username(username) -> dict``
  (``GET /users/username/{username}``).
* ``post(channel_id, message, root_id="") -> dict`` — ``POST /posts``; returns the created post.
* ``posts_since(channel_id, since_ms) -> list[dict]`` — ``GET /channels/{id}/posts?since=<since_ms>``;
  returns ONLY posts with ``create_at > since_ms`` (the server also returns older thread roots and
  older posts edited later — drop them), drops deleted posts (``delete_at > 0``) and system posts
  (non-empty ``type``); sorted ascending by ``create_at``; each post is the server dict plus
  ``"username"``, resolved with ``POST /users/ids`` for ids not yet cached (one batched call per
  ``posts_since`` at most, never for already-known ids). An id the server does not return raises
  ``MattermostError``.
* ``wait_for(channel_id, predicate, *, since_ms, timeout, poll=1.0) -> dict`` — polls ``posts_since``
  every ``poll`` seconds and returns the EARLIEST post (by ``create_at``) for which ``predicate(post)``
  is truthy; on ``timeout`` raises ``TimeoutError`` whose message says nothing matched within the
  timeout and lists the last (up to 10) posts seen as ``username: message`` (messages may be truncated
  to ~200 chars). A ``MattermostError`` while polling propagates (fail loud).
"""
from __future__ import annotations

import threading
import time

import pytest

from agents.mattermost import MattermostClient, MattermostError
from tests.fake_mattermost import FakeMattermost


@pytest.fixture()
def mm():
    with FakeMattermost() as fake:
        fake.add_user("user", "pw")
        fake.add_user("translator", is_bot=True)
        fake.add_user("fuzzer", is_bot=True)
        team = fake.add_team("switch")
        fake.channel_ = fake.add_channel(team["id"], "strangler-migration", "strangler-migration")
        yield fake


def client(fake: FakeMattermost, login: bool = True) -> MattermostClient:
    c = MattermostClient(fake.url + "/")
    if login:
        c.login("user", "pw")
    return c


# ----------------------------------------------------------------------------------------- auth

def test_login_uses_token_response_header(mm):
    with MattermostClient(mm.url) as c:
        user = c.login("user", "pw")
        assert user["username"] == "user"
        assert c.me()["id"] == user["id"]


def test_bad_password_raises_with_status(mm):
    with MattermostClient(mm.url) as c:
        with pytest.raises(MattermostError) as ei:
            c.login("user", "nope")
        assert ei.value.status == 401
        assert "401" in str(ei.value)


def test_login_without_token_header_fails_loud(mm):
    mm.omit_token_header = True
    with MattermostClient(mm.url) as c:
        with pytest.raises(MattermostError):
            c.login("user", "pw")


def test_calls_before_login_are_401(mm):
    with MattermostClient(mm.url) as c:
        with pytest.raises(MattermostError) as ei:
            c.me()
        assert ei.value.status == 401
        assert "expired session" in str(ei.value) or "Invalid" in str(ei.value)


# --------------------------------------------------------------------------------------- lookups

def test_channel_team_and_user_lookups(mm):
    c = client(mm)
    ch = c.channel(mm.channel_["id"])
    assert ch["name"] == "strangler-migration"
    assert c.team("switch")["id"] == ch["team_id"]
    assert c.user_by_username("fuzzer")["id"] == mm.user_id("fuzzer")
    with pytest.raises(MattermostError) as ei:
        c.channel("doesnotexist")
    assert ei.value.status == 404
    with pytest.raises(MattermostError):
        c.team("nope")
    with pytest.raises(MattermostError):
        c.user_by_username("ghost")


# ----------------------------------------------------------------------------------------- posts

def test_post_returns_created_post(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    p = c.post(cid, "@translator status")
    assert p["message"] == "@translator status" and p["channel_id"] == cid
    assert p["user_id"] == mm.user_id("user") and p["id"] and p["create_at"] > 0
    reply = c.post(cid, "threaded", root_id=p["id"])
    assert reply["root_id"] == p["id"]
    assert mm.messages(cid)[-2:] == [("user", "@translator status"), ("user", "threaded")]
    with pytest.raises(MattermostError) as ei:
        c.post(cid, "   ")
    assert ei.value.status == 400


def test_posts_since_filters_sorts_and_resolves_usernames(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    old_root = mm.add_post(cid, "user", "old root")
    old_edited = mm.add_post(cid, "translator", "old, edited later")
    anchor = mm.now_ms()
    a = mm.add_post(cid, "translator", "first", root_id=old_root["id"])  # reply -> server returns old root too
    mm.edit_post(old_edited["id"], "edited text")                       # update_at > anchor, create_at < anchor
    b = mm.add_post(cid, "fuzzer", "second")
    gone = mm.add_post(cid, "fuzzer", "deleted")
    mm.delete_post(gone["id"])
    mm.add_post(cid, "user", "user joined", type="system_join_channel")
    d = mm.add_post(cid, "user", "third")
    posts = c.posts_since(cid, anchor)
    assert [p["id"] for p in posts] == [a["id"], b["id"], d["id"]]
    assert [p["username"] for p in posts] == ["translator", "fuzzer", "user"]
    assert [p["message"] for p in posts] == ["first", "second", "third"]
    assert all(p["create_at"] > anchor for p in posts)


def test_posts_since_caches_usernames(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    anchor = mm.now_ms()
    mm.add_post(cid, "translator", "a")
    mm.add_post(cid, "fuzzer", "b")
    c.posts_since(cid, anchor)
    calls = mm.count("POST", "/users/ids")
    assert calls == 1
    mm.add_post(cid, "translator", "c")
    assert [p["username"] for p in c.posts_since(cid, anchor)] == ["translator", "fuzzer", "translator"]
    assert mm.count("POST", "/users/ids") == calls  # all ids known: no new lookup
    mm.add_user("cutover-gate", is_bot=True)
    mm.add_post(cid, "cutover-gate", "d")
    assert c.posts_since(cid, anchor)[-1]["username"] == "cutover-gate"
    assert mm.count("POST", "/users/ids") == calls + 1


def test_posts_since_empty_and_bad_channel(mm):
    c = client(mm)
    assert c.posts_since(mm.channel_["id"], mm.now_ms()) == []
    with pytest.raises(MattermostError) as ei:
        c.posts_since("doesnotexist", 0)
    assert ei.value.status == 404


def test_posts_since_unknown_author_fails_loud(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    anchor = mm.now_ms()
    p = mm.add_post(cid, "user", "orphan")
    with mm.lock:
        mm.posts[p["id"]]["user_id"] = "zzzzzzzzzzzzzzzzzzzzzzzzzz"
    with pytest.raises(MattermostError):
        c.posts_since(cid, anchor)


# -------------------------------------------------------------------------------------- wait_for

def test_wait_for_returns_post_that_arrives_later(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    anchor = mm.now_ms()
    mm.add_post(cid, "translator", "noise")

    def later():
        time.sleep(0.3)
        mm.add_post(cid, "fuzzer", "🔬 Shadow campaign #1")

    t = threading.Thread(target=later)
    t.start()
    try:
        hit = c.wait_for(cid, lambda p: p["username"] == "fuzzer" and "Shadow" in p["message"],
                         since_ms=anchor, timeout=5, poll=0.05)
    finally:
        t.join()
    assert hit["message"] == "🔬 Shadow campaign #1" and hit["username"] == "fuzzer"


def test_wait_for_ignores_posts_before_since_and_returns_earliest(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    mm.add_post(cid, "translator", "match old")
    anchor = mm.now_ms()
    first = mm.add_post(cid, "translator", "match 1")
    mm.add_post(cid, "translator", "match 2")
    hit = c.wait_for(cid, lambda p: p["message"].startswith("match"), since_ms=anchor, timeout=2, poll=0.05)
    assert hit["id"] == first["id"]


def test_wait_for_timeout_lists_last_posts(mm):
    c = client(mm)
    cid = mm.channel_["id"]
    anchor = mm.now_ms()
    for i in range(12):
        mm.add_post(cid, "translator", f"msg-{i:02d}")
    started = time.monotonic()
    with pytest.raises(TimeoutError) as ei:
        c.wait_for(cid, lambda p: False, since_ms=anchor, timeout=0.4, poll=0.05)
    assert time.monotonic() - started < 3
    text = str(ei.value)
    assert "translator: msg-11" in text and "msg-02" in text
    assert "msg-01" not in text  # only the last 10


def test_wait_for_propagates_api_errors(mm):
    c = client(mm)
    with pytest.raises(MattermostError):
        c.wait_for("doesnotexist", lambda p: True, since_ms=0, timeout=1, poll=0.05)
