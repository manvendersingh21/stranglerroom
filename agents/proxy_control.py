"""Client helpers for the :9000 strangler proxy's cutover control plane.

Used by the ``@cutover-gate`` agent to inspect the router state, flip traffic
between the legacy and modern upstreams, and verify that the proxy really
serves the approved target after a cutover.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = ["status", "flip", "verify_routing", "UPSTREAM_HEADER"]

UPSTREAM_HEADER = "X-Strangler-Upstream"

_MAX_BODY = 500
_TARGETS = frozenset({"legacy", "modern"})
_EXCERPT = 120


def _client(timeout: float) -> httpx.Client:
    """Create a proxy client that ignores env proxies and never follows redirects."""
    return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)


def _excerpt(response: httpx.Response) -> str:
    """Return a short, single-line excerpt of a response body."""
    text = " ".join(response.text.split()) or "<empty>"
    return text[:_EXCERPT] + ("..." if len(text) > _EXCERPT else "")


def _json_or_none(response: httpx.Response) -> tuple[bool, Any]:
    """Parse a response body as JSON, reporting whether parsing succeeded."""
    try:
        return True, response.json()
    except ValueError:
        return False, None


def status(proxy_url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Return the proxy's current router state from ``/_router/status``."""
    url = f"{proxy_url}/_router/status"
    try:
        with _client(timeout) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc

    body = response.text
    if response.status_code != 200:
        raise RuntimeError(f"GET {url} returned HTTP {response.status_code}: {body[:_MAX_BODY]}")

    parsed, data = _json_or_none(response)
    if not parsed:
        raise RuntimeError(
            f"GET {url} returned HTTP {response.status_code} with non-JSON body: {body[:_MAX_BODY]}"
        )
    if not isinstance(data, dict):
        raise RuntimeError(
            f"GET {url} returned HTTP {response.status_code} with a non-object JSON body: "
            f"{body[:_MAX_BODY]}"
        )
    if data.get("target") not in _TARGETS:
        raise RuntimeError(f"GET {url} returned a router state without a valid target: {body[:_MAX_BODY]}")
    return data


def flip(
    proxy_url: str,
    token: str,
    target: str,
    approved_by: str,
    evidence: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Flip the proxy to ``target`` and return the router's new state.

    The admin token is sent only in the ``X-Router-Token`` header and is
    redacted from every error message so it can never leak into a room post.
    """
    url = f"{proxy_url}/_router/flip"
    payload = {"target": target, "approved_by": approved_by, "evidence": evidence}
    try:
        with _client(timeout) as client:
            response = client.post(url, headers={"X-Router-Token": token}, json=payload)
    except httpx.HTTPError as exc:
        detail = str(exc).replace(token, "[redacted]") if token else str(exc)
        raise RuntimeError(f"POST {url} failed: {detail}") from exc

    body = response.text.replace(token, "[redacted]") if token else response.text
    if response.status_code != 200:
        raise RuntimeError(f"POST {url} returned HTTP {response.status_code}: {body[:_MAX_BODY]}")

    parsed, state = _json_or_none(response)
    if not parsed or not isinstance(state, dict):
        raise RuntimeError(
            f"POST {url} returned HTTP {response.status_code} with an invalid router state: "
            f"{body[:_MAX_BODY]}"
        )
    if state.get("target") != target:
        raise RuntimeError(
            f"POST {url} left the router on {state.get('target')!r}, requested {target!r}"
        )
    return state


def verify_routing(
    proxy_url: str,
    direct_url: str,
    path: str,
    expected_target: str,
    *,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Compare a proxied GET with the same request sent directly upstream.

    Returns ``(True, summary)`` only when the proxy reports
    ``X-Strangler-Upstream == expected_target`` and the status and body match
    the direct response. HTTP and transport problems return ``(False, reason)``
    instead of raising.
    """
    proxy_target = proxy_url + path
    direct_target = direct_url + path

    try:
        with _client(timeout) as client:
            proxied = client.get(proxy_target)
    except httpx.HTTPError as exc:
        return False, f"proxy request to {proxy_target} failed: {exc}"

    try:
        with _client(timeout) as client:
            direct = client.get(direct_target)
    except httpx.HTTPError as exc:
        return False, f"direct request to {direct_target} failed: {exc}"

    actual_target = proxied.headers.get(UPSTREAM_HEADER)
    if actual_target != expected_target:
        return (
            False,
            f"proxy answered from upstream {actual_target!r}, expected {expected_target!r}",
        )

    if proxied.status_code != direct.status_code:
        return (
            False,
            f"status mismatch for {path or '/'}: proxy {proxied.status_code} "
            f"!= direct {direct.status_code}",
        )

    proxy_json, proxy_value = _json_or_none(proxied)
    direct_json, direct_value = _json_or_none(direct)
    if proxy_json and direct_json:
        same_body = proxy_value == direct_value
    else:
        same_body = proxied.content == direct.content
    if not same_body:
        return (
            False,
            f"body mismatch for {path or '/'}: proxy={_excerpt(proxied)!r} "
            f"direct={_excerpt(direct)!r}",
        )

    return (
        True,
        f"GET {path or '/'} via proxy -> {proxied.status_code} from {expected_target!r}, "
        f"identical to the direct {expected_target} response",
    )
