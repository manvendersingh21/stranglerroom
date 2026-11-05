"""Contract tests for the legacy Open Library web.py target (docs/SPEC.md §1, §2, §5).

These pin down the ground-truth behaviour, including the five undocumented quirks D-1..D-5.
"""
import pytest
import httpx

from targets.common.dataset import SEED_EDITIONS, SEED_LISTS, USERS, Store
from tests.procs import free_port, service


@pytest.fixture(scope="module")
def legacy():
    with service("targets.legacy_openlibrary.server", free_port()) as url:
        with httpx.Client(base_url=url, timeout=10) as c:
            yield c


@pytest.fixture(autouse=True)
def _reset(legacy):
    assert legacy.post("/_admin/reset").json() == {"reset": True}


# ---------- dataset ----------

def test_dataset_shape():
    assert USERS == {"george08", "openlibrary", "mekBot"}
    assert len(SEED_LISTS[("george08", "OL1L")]["seeds"]) == 5
    assert len(SEED_LISTS[("george08", "OL2L")]["seeds"]) == 2
    assert SEED_LISTS[("george08", "OL3L")]["seeds"] == []
    assert len(SEED_LISTS[("openlibrary", "OL10L")]["seeds"]) == 6
    assert len(SEED_LISTS[("mekBot", "OL20L")]["seeds"]) == 1
    for olid in ["OL7353617M", "OL1M", "OL2M", "OL3M", "OL4M", "OL5M"]:
        assert isinstance(SEED_EDITIONS[olid]["price"], float)
    for (_, _), rec in SEED_LISTS.items():
        for s in rec["seeds"]:
            if s.startswith("/books/"):
                assert s.split("/")[2] in SEED_EDITIONS


def test_store_mutation_and_reset():
    st = Store()
    out = st.apply_seed_changes("george08", "OL3L", ["/works/OL1W", "/works/OL1W"], [])
    assert out["seeds"] == ["/works/OL1W"] and out["last_update"] == "2025-01-01T00:00:00"
    st.reset()
    assert st.get_list("george08", "OL3L")["seeds"] == []
    assert [l["key"] for l in st.user_lists("george08")] == [
        "/people/george08/lists/OL1L", "/people/george08/lists/OL2L", "/people/george08/lists/OL3L"]


# ---------- E1 lists_json + D-1 + D-2 ----------

def test_health(legacy):
    assert legacy.get("/health").json() == {"status": "ok", "stack": "web.py"}


def test_lists_basic(legacy):
    r = legacy.get("/people/george08/lists")
    assert r.status_code == 200 and r.headers["content-type"].split(";")[0] == "application/json"
    body = r.json()
    assert body["links"] == {"self": "/people/george08/lists.json", "user": "/people/george08"}
    assert body["size"] == 3
    e = body["entries"][0]
    assert e == {"url": "/people/george08/lists/OL1L", "full_url": "/people/george08/lists/OL1L/sci-fi-classics",
                 "name": "Sci-Fi Classics", "seed_count": 5, "last_update": e["last_update"]}


def test_D1_json_suffix_alias(legacy):
    a = legacy.get("/people/george08/lists")
    b = legacy.get("/people/george08/lists.json")
    assert b.status_code == 200 and a.json() == b.json()
    assert legacy.get("/people/george08/lists/OL1L/seeds.json").json() == legacy.get("/people/george08/lists/OL1L/seeds").json()
    assert legacy.get("/books/OL1M/quote.json").status_code == 200


@pytest.mark.parametrize("q,expected_len", [("limit=none", 3), ("limit=all", 3), ("limit=", 3), ("limit=abc", 3),
                                            ("limit=1", 1), ("limit=2&offset=1", 2), ("offset=none", 3)])
def test_D2_loose_limit(legacy, q, expected_len):
    r = legacy.get(f"/people/george08/lists?{q}")
    assert r.status_code == 200
    assert len(r.json()["entries"]) == expected_len


@pytest.mark.parametrize("v,detailed", [("yes", True), ("1", True), ("maybe", True), ("", False), ("0", False),
                                         ("false", False), ("NO", False), ("none", False)])
def test_D2_loose_details(legacy, v, detailed):
    e = legacy.get(f"/people/george08/lists?details={v}").json()["entries"][0]
    assert ("seeds" in e) is detailed and ("description" in e) is detailed


def test_unknown_user_404(legacy):
    r = legacy.get("/people/nobody/lists")
    assert r.status_code == 404 and r.json() == {"error": "not_found", "key": "/people/nobody"}


# ---------- E2/E4 + D-3 ----------

def test_seeds_get(legacy):
    body = legacy.get("/people/george08/lists/OL2L/seeds").json()
    assert body == {"links": {"self": "/people/george08/lists/OL2L/seeds.json", "list": "/people/george08/lists/OL2L"},
                    "size": 2, "entries": [{"url": "/works/OL27258W", "type": "work"}, {"url": "/books/OL2M", "type": "edition"}]}


@pytest.mark.parametrize("path", ["/people/george08/lists/OL999L/seeds", "/people/nobody/lists/OL1L/seeds.json",
                                  "/people/george08/lists/OL999L/editions"])
def test_D3_missing_list_returns_200(legacy, path):
    r = legacy.get(path)
    assert r.status_code == 200 and r.json() == {"error": "not_found", "entries": []}


def test_editions(legacy):
    body = legacy.get("/people/george08/lists/OL1L/editions.json").json()
    assert body["size"] == 2 and [e["key"] for e in body["entries"]] == ["/books/OL7353617M", "/books/OL1M"]
    assert all("price" not in e for e in body["entries"])
    assert len(legacy.get("/people/george08/lists/OL1L/editions?limit=1&offset=1").json()["entries"]) == 1


# ---------- E5 + D-4 ----------

def test_quote_sale(legacy):
    p = SEED_EDITIONS["OL1M"]["price"]
    body = legacy.get("/books/OL1M/quote?quantity=3").json()
    assert body == {"key": "/books/OL1M", "title": SEED_EDITIONS["OL1M"]["title"], "unit_price": p, "quantity": 3,
                    "total": round(p * 3, 2), "currency": "USD", "type": "sale"}


@pytest.mark.parametrize("q,typ", [(-1, "credit_reversal"), (-10, "credit_reversal"), (0, "noop")])
def test_D4_non_positive_quantity_allowed(legacy, q, typ):
    r = legacy.get(f"/books/OL2M/quote?quantity={q}")
    assert r.status_code == 200 and r.json()["type"] == typ and r.json()["quantity"] == q


@pytest.mark.parametrize("q", [-11, 101])
def test_quote_out_of_range(legacy, q):
    r = legacy.get(f"/books/OL2M/quote?quantity={q}")
    assert r.status_code == 400 and r.json() == {"error": "quantity_out_of_range", "min": -10, "max": 100}


def test_quote_loose_quantity_and_unknown(legacy):
    assert legacy.get("/books/OL2M/quote?quantity=abc").json()["quantity"] == 1
    r = legacy.get("/books/OL404M/quote")
    assert r.status_code == 404 and r.json() == {"error": "not_found", "key": "/books/OL404M"}


# ---------- E3 + D-5 ----------

def test_post_seeds(legacy):
    r = legacy.post("/people/george08/lists/OL2L/seeds.json", json={"add": ["/works/OL9W", "/works/OL27258W"], "remove": ["/books/OL2M"]})
    assert r.status_code == 200 and r.json() == {"success": True, "size": 2, "added": 1, "removed": 1}
    assert [e["url"] for e in legacy.get("/people/george08/lists/OL2L/seeds").json()["entries"]] == ["/works/OL27258W", "/works/OL9W"]


def test_post_invalid_seed(legacy):
    r = legacy.post("/people/george08/lists/OL2L/seeds", json={"add": ["bogus"]})
    assert r.status_code == 400 and r.json() == {"error": "invalid_seed", "seed": "bogus"}


@pytest.mark.parametrize("body", [{}, {"add": []}, {"add": [], "remove": []}])
def test_D5_empty_body_crashes_500(legacy, body):
    r = legacy.post("/people/george08/lists/OL1L/seeds", json=body)
    assert r.status_code == 500
    assert "json" not in r.headers.get("content-type", "")


def test_post_missing_list_D3(legacy):
    r = legacy.post("/people/george08/lists/OL999L/seeds", json={"add": ["/works/OL1W"]})
    assert r.status_code == 200 and r.json() == {"error": "not_found", "entries": []}
