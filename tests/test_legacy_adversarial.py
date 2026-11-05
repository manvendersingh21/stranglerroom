"""Adversarial regression tests for the legacy Open Library target (docs/SPEC.md sections 1, 2, 5).

These complement ``tests/test_legacy_contract.py`` with edge cases found while
reviewing the legacy target: check ordering on POST, ``added``/``removed``
accounting, loose-parameter corner cases, ``.json`` alias boundaries and
robustness of the D-5 crash path when stdout/stderr are piped and never read.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.procs import free_port, service

SEEDS = "/people/george08/lists/OL1L/seeds"


@pytest.fixture(scope="module")
def legacy():
    with service("targets.legacy_openlibrary.server", free_port()) as url:
        yield url


@pytest.fixture(autouse=True)
def _reset(legacy):
    httpx.post(f"{legacy}/_admin/reset")
    yield


def _ctype(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip()


# --- E3 POST seeds ---------------------------------------------------------------------------

def test_missing_list_checked_before_body_parse(legacy):
    """Step 1 precedes step 2: garbage body on an unknown list is still the D-3 body."""
    r = httpx.post(f"{legacy}/people/nobody/lists/OL1L/seeds.json", content=b"not json")
    assert (r.status_code, r.json()) == (200, {"error": "not_found", "entries": []})


def test_invalid_seed_checked_before_empty_churn(legacy):
    """Step 3 precedes step 4: an invalid remove seed with empty add is 400, not 500."""
    r = httpx.post(f"{legacy}{SEEDS}", json={"remove": ["bogus"]})
    assert (r.status_code, r.json()) == (400, {"error": "invalid_seed", "seed": "bogus"})


def test_invalid_seed_first_offender_add_before_remove(legacy):
    r = httpx.post(f"{legacy}{SEEDS}", json={"add": ["/works/OK", "bad-a"], "remove": ["bad-r"]})
    assert (r.status_code, r.json()) == (400, {"error": "invalid_seed", "seed": "bad-a"})
    # and the rejected request did not mutate the list
    assert httpx.get(f"{legacy}{SEEDS}").json()["size"] == 5


@pytest.mark.parametrize("body", [b"", b"{}", b'{"add": [], "remove": []}', b'{"add": []}'])
def test_D5_variants_on_json_alias(legacy, body):
    r = httpx.post(f"{legacy}{SEEDS}.json", content=body)
    assert r.status_code == 500
    assert _ctype(r) == "text/html"
    assert r.text == "internal server error"


def test_added_removed_are_actual_changes(legacy):
    r = httpx.post(f"{legacy}{SEEDS}", json={
        "add": ["/works/NEW", "/works/NEW", "/books/OL1M"],      # dup + already present
        "remove": ["/subjects/science_fiction", "/authors/NOPE"],  # present + absent
    })
    assert r.status_code == 200 and _ctype(r) == "application/json"
    assert r.json() == {"success": True, "size": 5, "added": 1, "removed": 1}
    seeds = [e["url"] for e in httpx.get(f"{legacy}{SEEDS}").json()["entries"]]
    assert seeds == ["/works/OL45883W", "/books/OL7353617M", "/authors/OL26320A", "/books/OL1M", "/works/NEW"]


def test_post_persists_until_reset(legacy):
    httpx.post(f"{legacy}/people/george08/lists/OL3L/seeds", json={"add": ["/books/OL5M"]})
    lists = httpx.get(f"{legacy}/people/george08/lists.json").json()["entries"]
    shelf = next(e for e in lists if e["url"].endswith("/OL3L"))
    assert shelf["seed_count"] == 1 and shelf["last_update"] == "2025-01-01T00:00:00"
    httpx.post(f"{legacy}/_admin/reset")
    assert httpx.get(f"{legacy}/people/george08/lists/OL3L/seeds").json()["size"] == 0


# --- loose params / paging --------------------------------------------------------------------

@pytest.mark.parametrize("q,expected", [
    ("limit=%20%202%20", 2), ("limit=ALL", 3), ("limit=+1", 1), ("offset=none&limit=1", 1),
    ("offset=-1", 1), ("limit=-1", 2), ("limit=0", 0), ("limit=1&limit=2", 2),
])
def test_lists_paging_corners(legacy, q, expected):
    body = httpx.get(f"{legacy}/people/george08/lists?{q}").json()
    assert body["size"] == 3 and len(body["entries"]) == expected


def test_editions_offset_none_is_zero(legacy):
    body = httpx.get(f"{legacy}/people/openlibrary/lists/OL10L/editions.json?offset=none&limit=2").json()
    assert body["size"] == 3
    assert [e["key"] for e in body["entries"]] == ["/books/OL3M", "/books/OL4M"]
    assert all("price" not in e for e in body["entries"])


@pytest.mark.parametrize("q,qty,total", [
    ("quantity=%203%20", 3, 9.99), ("quantity=NONE", 1, 3.33), ("quantity=", 1, 3.33),
    ("quantity=7", 7, 23.31), ("quantity=-3", -3, -9.99), ("quantity=100", 100, 333.0),
])
def test_quote_coercion_and_rounding(legacy, q, qty, total):
    body = httpx.get(f"{legacy}/books/OL4M/quote?{q}").json()
    assert body["quantity"] == qty and body["total"] == total


def test_quote_unknown_checked_before_range(legacy):
    r = httpx.get(f"{legacy}/books/OL999M/quote.json?quantity=-11")
    assert (r.status_code, r.json()) == (404, {"error": "not_found", "key": "/books/OL999M"})


# --- routing ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/people/george08/lists.json/", "/people/george08/lists.JSON",
    "/people/george08/lists/OL1L/seeds.json.json", "/books/OL1M/quote.jsonx",
])
def test_only_exact_json_alias_is_served(legacy, path):
    assert httpx.get(f"{legacy}{path}").status_code == 404


def test_trailing_newline_in_capture_does_not_match_known_id(legacy):
    r = httpx.get(f"{legacy}/books/OL1M%0A/quote")
    assert r.status_code == 404 and r.json()["key"] == "/books/OL1M\n"


# --- robustness -------------------------------------------------------------------------------

def test_many_d5_crashes_do_not_stall_piped_server(legacy):
    """procs.service pipes our output and never drains it; tracebacks must not fill the pipe."""
    with httpx.Client(timeout=10) as c:
        for _ in range(150):
            assert c.post(f"{legacy}{SEEDS}", content=b"").status_code == 500
        assert c.get(f"{legacy}/health").json() == {"status": "ok", "stack": "web.py"}


def test_concurrent_gets_are_deterministic(legacy):
    paths = ["/people/george08/lists?details=1", "/people/george08/lists/OL1L/seeds.json",
             "/people/openlibrary/lists/OL10L/editions?limit=2", "/books/OL2M/quote?quantity=3"]

    async def run():
        async with httpx.AsyncClient(timeout=30) as c:
            ref = {p: (await c.get(legacy + p)).text for p in paths}
            rs = await asyncio.gather(*[c.get(legacy + paths[i % len(paths)]) for i in range(200)])
            return [r.text == ref[paths[i % len(paths)]] for i, r in enumerate(rs)]

    assert all(asyncio.run(run()))
