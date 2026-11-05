"""Acceptance tests for ``agents/proxy_control.py`` — the cutover gate's client for the strangler proxy.

Runs the REAL ``targets.proxy.router`` in front of two ``tests.fake_upstream`` echo servers (all on free
ports, router state in a tmp file, a test-only admin token), then drives it through the module API:

* ``status(proxy_url, *, timeout=10.0) -> dict`` — ``GET /_router/status``; RuntimeError on non-200 or transport error.
* ``flip(proxy_url, token, target, approved_by, evidence, *, timeout=10.0) -> dict`` — ``POST /_router/flip`` with
  ``X-Router-Token``; returns the new router state; RuntimeError carrying the HTTP status and response body on
  non-200, on a transport error, or if the returned state does not report the requested target.
* ``verify_routing(proxy_url, direct_url, path, expected_target, *, timeout=10.0) -> tuple[bool, str]`` — GET
  ``path`` through the proxy and directly; ok iff the proxied response carries
  ``X-Strangler-Upstream == expected_target`` and has the same status and JSON body as the direct one. Never
  raises for HTTP/transport problems: returns ``(False, reason)``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agents import proxy_control
from tests.procs import free_port, service

TOKEN = "test-proxy-control-token"
LISTS = "/people/george08/lists.json?details=1"


@pytest.fixture()
def stack(tmp_path):
    state = tmp_path / "router.json"
    # each port is picked right before its service binds it (no duplicate free_port() hand-outs)
    with service("tests.fake_upstream", free_port(), health="/", args=["--name", "legacy"]) as lu, \
         service("tests.fake_upstream", free_port(), health="/", args=["--name", "modern"]) as mu, \
         service("targets.proxy.router", free_port(), health="/_router/status",
                 env={"LEGACY_URL": lu, "MODERN_URL": mu, "ROUTER_STATE_FILE": str(state),
                      "ROUTER_ADMIN_TOKEN": TOKEN}) as pu:
        yield {"proxy": pu, "legacy": lu, "modern": mu, "state": state}


def test_status_defaults_to_legacy(stack):
    st = proxy_control.status(stack["proxy"])
    assert isinstance(st, dict)
    assert st["target"] == "legacy"
    assert st["upstream_url"] == stack["legacy"]
    assert st["approved_by"] is None and st["flipped_at"] is None


def test_status_unreachable_proxy_raises():
    with pytest.raises(RuntimeError):
        proxy_control.status(f"http://127.0.0.1:{free_port()}", timeout=2)


def test_status_rejects_a_reply_without_a_valid_target(stack):
    # the echo upstream answers 200 JSON on /_router/status, but it is not a router state
    with pytest.raises(RuntimeError, match="valid target"):
        proxy_control.status(stack["legacy"])


def test_flip_to_modern_and_back(stack):
    pu = stack["proxy"]
    new = proxy_control.flip(pu, TOKEN, "modern", "user", "100% Approved Parity (5/5) rev=abc123")
    assert new["target"] == "modern"
    assert new["approved_by"] == "user"
    assert new["evidence"] == "100% Approved Parity (5/5) rev=abc123"
    assert new["flipped_at"]
    st = proxy_control.status(pu)
    assert st["target"] == "modern" and st["upstream_url"] == stack["modern"]
    assert json.loads(stack["state"].read_text())["target"] == "modern"

    back = proxy_control.flip(pu, TOKEN, "legacy", "user", "rollback")
    assert back["target"] == "legacy"
    assert proxy_control.status(pu)["target"] == "legacy"


def test_flip_wrong_token_raises_with_body_and_changes_nothing(stack):
    pu = stack["proxy"]
    with pytest.raises(RuntimeError) as exc:
        proxy_control.flip(pu, "not-the-token", "modern", "user", "x")
    msg = str(exc.value)
    assert "403" in msg and "forbidden" in msg
    assert proxy_control.status(pu)["target"] == "legacy"


def test_flip_invalid_target_raises_with_body(stack):
    with pytest.raises(RuntimeError) as exc:
        proxy_control.flip(stack["proxy"], TOKEN, "mars", "user", "x")
    msg = str(exc.value)
    assert "400" in msg and "invalid_target" in msg
    assert proxy_control.status(stack["proxy"])["target"] == "legacy"


def test_flip_unreachable_proxy_raises():
    with pytest.raises(RuntimeError):
        proxy_control.flip(f"http://127.0.0.1:{free_port()}", TOKEN, "modern", "user", "x", timeout=2)


def test_verify_routing_legacy_then_modern(stack):
    pu, lu, mu = stack["proxy"], stack["legacy"], stack["modern"]

    ok, why = proxy_control.verify_routing(pu, lu, LISTS, "legacy")
    assert ok is True, why
    assert isinstance(why, str) and "legacy" in why

    ok, why = proxy_control.verify_routing(pu, mu, LISTS, "modern")
    assert ok is False
    assert "legacy" in why  # the header the proxy actually reported is named in the reason

    proxy_control.flip(pu, TOKEN, "modern", "user", "evidence")
    ok, why = proxy_control.verify_routing(pu, mu, LISTS, "modern")
    assert ok is True, why
    assert "modern" in why


def test_verify_routing_detects_body_mismatch(stack):
    pu, lu = stack["proxy"], stack["legacy"]
    proxy_control.flip(pu, TOKEN, "modern", "user", "evidence")
    # header says modern (as expected) but the direct reference is the legacy upstream: bodies differ
    ok, why = proxy_control.verify_routing(pu, lu, LISTS, "modern")
    assert ok is False
    assert "body" in why.lower()


def test_verify_routing_detects_status_mismatch(stack):
    pu, lu = stack["proxy"], stack["legacy"]
    # proxied /echo/status/418 vs direct /echo/status/418 agree ...
    ok, why = proxy_control.verify_routing(pu, lu, "/echo/status/418", "legacy")
    assert ok is True, why
    # ... but a different path on the direct side would not: emulate via a direct URL whose path prefix
    # changes the status (fake_upstream serves /echo/status/<code> as that code).
    ok, why = proxy_control.verify_routing(pu, lu + "/echo/status/503", "", "legacy")
    assert ok is False
    assert "status" in why.lower()


def test_verify_routing_unreachable_direct_returns_false(stack):
    ok, why = proxy_control.verify_routing(stack["proxy"], f"http://127.0.0.1:{free_port()}", LISTS, "legacy",
                                           timeout=2)
    assert ok is False and why


def test_verify_routing_unreachable_proxy_returns_false():
    ok, why = proxy_control.verify_routing(f"http://127.0.0.1:{free_port()}", f"http://127.0.0.1:{free_port()}",
                                           LISTS, "legacy", timeout=2)
    assert ok is False and why


def test_router_token_sent_as_header_not_logged_in_errors(stack):
    """The admin token must never leak into the RuntimeError text (it may be posted to the room)."""
    with pytest.raises(RuntimeError) as exc:
        proxy_control.flip(stack["proxy"], "secret-wrong-token-xyz", "modern", "user", "x")
    assert "secret-wrong-token-xyz" not in str(exc.value)
    assert httpx.get(stack["proxy"] + "/_router/status").json()["target"] == "legacy"
