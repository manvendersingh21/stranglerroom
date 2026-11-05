"""Tests for the modern FastAPI target (docs/SPEC.md §4) and its code generator.

The generator (:mod:`agents.codegen.fastapi_lists`) renders ``app/lists.py`` from a quirk
policy.  For several policies we write the rendered module into an isolated temp file
(``MODERN_LISTS_PATH``; never the real ``app/lists.py``, which the live demo's translator owns),
start the modern server as a subprocess with the reloader disabled, and check each quirk's
preserved/naive behaviour over HTTP.
"""
from __future__ import annotations

import ast
import hashlib
import itertools
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from agents.codegen import QUIRK_TAGS, module_rev, render_lists_module
from tests.procs import ROOT, free_port, service

#: Isolated generated module (never the real app/lists.py, which the live demo's translator owns).
_LISTS_DIR = tempfile.TemporaryDirectory(prefix="modern-lists-")  # removed at interpreter exit
LISTS_PATH = Path(_LISTS_DIR.name) / "lists.py"
DATASET_PATH = ROOT / "targets" / "common" / "dataset.py"
MODERN_ENV = {"MODERN_NO_RELOAD": "1", "MODERN_LISTS_PATH": str(LISTS_PATH)}

NAIVE: dict[str, str] = {}
ALL_PRESERVE = {tag: "preserve" for tag in QUIRK_TAGS}
DEMO = {"json_suffix": "preserve", "loose_query": "preserve", "non_positive_quantity": "preserve",
        "missing_resource_200": "deprecate", "empty_body_500": "deprecate"}
LOOSE_ONLY = {"loose_query": "preserve"}            # coerce, then FastAPI-shaped 422 for q <= 0
NON_POSITIVE_ONLY = {"non_positive_quantity": "preserve"}  # strict int, legacy range

POLICIES = {"naive": NAIVE, "all_preserve": ALL_PRESERVE, "demo": DEMO,
            "loose_only": LOOSE_ONLY, "non_positive_only": NON_POSITIVE_ONLY}

ENDPOINT_PATHS = [
    ("GET", "/people/{user}/lists"),
    ("GET", "/people/{user}/lists/{list_id}/seeds"),
    ("POST", "/people/{user}/lists/{list_id}/seeds"),
    ("GET", "/people/{user}/lists/{list_id}/editions"),
    ("GET", "/books/{olid}/quote"),
]

#: Exactly what FastAPI/pydantic emit for ``quantity: int = Query(1, gt=0)`` given "0".
FASTAPI_GT0_422 = {"detail": [{"type": "greater_than", "loc": ["query", "quantity"],
                               "msg": "Input should be greater than 0", "input": "0", "ctx": {"gt": 0}}]}


def _ctype(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip()


def _wait_for_dataset(timeout: float = 25 * 60) -> None:
    """The shared dataset is built in parallel by another builder; wait for it to land."""
    deadline = time.time() + timeout
    while not DATASET_PATH.exists():
        if time.time() > deadline:
            pytest.fail(f"{DATASET_PATH} did not appear within {timeout:.0f}s")
        time.sleep(5)


@contextmanager
def lists_file(content: str | None):
    """Temporarily set ``app/lists.py`` to ``content`` (None = absent); restore it afterwards."""
    previous = LISTS_PATH.read_bytes() if LISTS_PATH.exists() else None
    try:
        if content is None:
            LISTS_PATH.unlink(missing_ok=True)
        else:
            LISTS_PATH.write_bytes(content.encode())
        yield
    finally:
        if previous is None:
            LISTS_PATH.unlink(missing_ok=True)
        else:
            LISTS_PATH.write_bytes(previous)


@contextmanager
def modern_server(content: str | None):
    """Run the modern target (no reload) against the given ``lists.py`` content."""
    _wait_for_dataset()
    with lists_file(content):
        with service("targets.modern_fastapi.server", free_port(), env=MODERN_ENV) as url:
            with httpx.Client(base_url=url, timeout=10) as client:
                yield client


class Served:
    """A running modern server plus the policy/source it was generated from."""

    def __init__(self, name: str, policy: dict[str, str], source: str, client: httpx.Client):
        self.name, self.policy, self.source, self.client = name, policy, source, client

    def preserves(self, tag: str) -> bool:
        return self.policy.get(tag) == "preserve"


@pytest.fixture(scope="module", params=list(POLICIES), ids=list(POLICIES))
def served(request):
    policy = POLICIES[request.param]
    source = render_lists_module(policy, header_comment=f"test policy: {request.param}")
    with modern_server(source) as client:
        yield Served(request.param, policy, source, client)


@pytest.fixture
def m(served) -> Served:
    """Per-test handle on the served policy, with the store reset first."""
    assert served.client.post("/_admin/reset").json() == {"reset": True}
    return served


# ====================================================================== generator unit tests

ALL_COMBOS = [dict(zip(QUIRK_TAGS, combo)) for combo in itertools.product(("preserve", "deprecate"), repeat=5)]


@pytest.mark.parametrize("policy", ALL_COMBOS, ids=lambda p: "".join("P" if v == "preserve" else "d" for v in p.values()))
def test_render_deterministic_and_compiles(policy):
    a = render_lists_module(policy)
    b = render_lists_module(dict(reversed(list(policy.items()))))
    assert a == b, "rendering must not depend on dict order"
    tree = ast.parse(a)
    compile(a, "lists.py", "exec")
    assert ast.get_docstring(tree)
    assert a.isascii()
    for tag, decision in policy.items():
        assert any(line.split() == [tag, decision] for line in ast.get_docstring(tree).splitlines()), tag
    assigns = {t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)}
    assert "router" in assigns
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"re", "json", "fastapi", "fastapi.responses", "pydantic", "targets.modern_fastapi.app.store"}


def test_all_32_combos_distinct():
    assert len({render_lists_module(p) for p in ALL_COMBOS}) == 32


def _code_only(source: str) -> str:
    """AST dump of ``source`` without the module docstring and the recorded POLICY literal."""
    tree = ast.parse(source)
    tree.body = [node for node in tree.body[1:]
                 if not (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "POLICY" for t in node.targets))]
    return ast.dump(tree)


def test_unset_equals_deprecate_behaviour():
    naive = render_lists_module(None)
    assert naive == render_lists_module({})
    assert naive != render_lists_module({t: "deprecate" for t in QUIRK_TAGS})  # the policy record differs ...
    assert _code_only(naive) == _code_only(render_lists_module({t: "deprecate" for t in QUIRK_TAGS}))  # ... code not


def test_module_rev():
    src = render_lists_module(DEMO)
    assert module_rev(src) == hashlib.sha256(src.encode()).hexdigest()[:12]
    assert len(module_rev(src)) == 12 and module_rev(src) != module_rev(render_lists_module(NAIVE))


def test_header_comment_is_escaped_and_changes_rev():
    nasty = 'approved by user """ \\ end'
    src = render_lists_module(DEMO, header_comment=nasty)
    assert nasty in ast.get_docstring(ast.parse(src))
    assert module_rev(src) != module_rev(render_lists_module(DEMO))


@pytest.mark.parametrize("bad", [{"json_suffix": "keep"}, {"no_such_quirk": "preserve"}])
def test_invalid_policy_rejected(bad):
    with pytest.raises(ValueError):
        render_lists_module(bad)


# ====================================================================== app shell (main.py)

def test_app_without_lists_module():
    with modern_server(None) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/_meta").json() == {"rev": None, "routes": ["GET /health"]}
        assert c.post("/_admin/reset").json() == {"reset": True}
        assert c.get("/people/george08/lists").status_code == 404


def test_app_with_broken_lists_module():
    broken = "raise RuntimeError('boom')\n"
    with modern_server(broken) as c:
        meta = c.get("/_meta").json()
        assert meta["rev"] == module_rev(broken) and meta["routes"] == ["GET /health"]
        assert "boom" in meta["import_error"]
        assert c.get("/health").json() == {"status": "ok"}


def test_reload_picks_up_regenerated_module():
    """With the reloader on (the default), writing a new lists.py changes the served rev."""
    _wait_for_dataset()
    first, second = render_lists_module(NAIVE), render_lists_module(DEMO)
    with lists_file(first), service("targets.modern_fastapi.server", free_port(),
                                    env={"MODERN_LISTS_PATH": str(LISTS_PATH)}) as url:
        assert httpx.get(url + "/_meta").json()["rev"] == module_rev(first)
        LISTS_PATH.write_bytes(second.encode())
        deadline, rev = time.time() + 30, None
        while time.time() < deadline and rev != module_rev(second):
            time.sleep(0.25)
            try:
                rev = httpx.get(url + "/_meta", timeout=2).json()["rev"]
            except httpx.HTTPError:
                continue  # worker restarting
        assert rev == module_rev(second)
        assert httpx.get(url + "/books/OL1M/quote.json?quantity=0").json()["type"] == "noop"


def test_lists_file_restored_after_fixture():
    before = LISTS_PATH.read_bytes() if LISTS_PATH.exists() else None
    with lists_file("x = 1\n"):
        assert LISTS_PATH.read_text() == "x = 1\n"
    assert (LISTS_PATH.read_bytes() if LISTS_PATH.exists() else None) == before


# ====================================================================== served behaviour

def test_meta_rev_and_routes(m):
    meta = m.client.get("/_meta").json()
    assert meta["rev"] == module_rev(m.source)
    assert "import_error" not in meta
    expected = {"GET /health"}
    for method, path in ENDPOINT_PATHS:
        expected.add(f"{method} {path}")
        if m.preserves("json_suffix"):
            expected.add(f"{method} {path}.json")
    assert meta["routes"] == sorted(expected)
    assert m.client.get("/health").json() == {"status": "ok"}


def test_lists_basic_matches_legacy_contract(m):
    from targets.common.dataset import SEED_LISTS

    r = m.client.get("/people/george08/lists")
    assert r.status_code == 200 and _ctype(r) == "application/json"
    body = r.json()
    assert body["links"] == {"self": "/people/george08/lists.json", "user": "/people/george08"}
    assert body["size"] == 3
    rec = SEED_LISTS[("george08", "OL1L")]
    assert body["entries"][0] == {"url": "/people/george08/lists/OL1L",
                                  "full_url": "/people/george08/lists/OL1L/sci-fi-classics",
                                  "name": "Sci-Fi Classics", "seed_count": 5, "last_update": rec["last_update"]}
    assert [e["url"] for e in body["entries"]] == [f"/people/george08/lists/OL{i}L" for i in (1, 2, 3)]
    detailed = m.client.get("/people/george08/lists?details=true&limit=1&offset=1").json()["entries"]
    assert len(detailed) == 1 and detailed[0]["seeds"] == SEED_LISTS[("george08", "OL2L")]["seeds"]
    assert detailed[0]["description"] == SEED_LISTS[("george08", "OL2L")]["description"]
    assert len(m.client.get("/people/george08/lists?limit=-1").json()["entries"]) == 2  # plain slicing


def test_unknown_user_and_olid_404(m):
    r = m.client.get("/people/nobody/lists")
    assert r.status_code == 404 and r.json() == {"error": "not_found", "key": "/people/nobody"}
    assert _ctype(r) == "application/json"
    r = m.client.get("/books/OL404M/quote")
    assert r.status_code == 404 and r.json() == {"error": "not_found", "key": "/books/OL404M"}


def test_seeds_and_editions(m):
    from targets.common.dataset import SEED_EDITIONS

    assert m.client.get("/people/george08/lists/OL2L/seeds").json() == {
        "links": {"self": "/people/george08/lists/OL2L/seeds.json", "list": "/people/george08/lists/OL2L"},
        "size": 2, "entries": [{"url": "/works/OL27258W", "type": "work"}, {"url": "/books/OL2M", "type": "edition"}]}
    types = [e["type"] for e in m.client.get("/people/george08/lists/OL1L/seeds").json()["entries"]]
    assert types == ["work", "edition", "author", "edition", "subject"]
    body = m.client.get("/people/george08/lists/OL1L/editions").json()
    assert body["links"] == {"self": "/people/george08/lists/OL1L/editions.json", "list": "/people/george08/lists/OL1L"}
    assert body["size"] == 2 and [e["key"] for e in body["entries"]] == ["/books/OL7353617M", "/books/OL1M"]
    assert body["entries"][0] == {k: v for k, v in SEED_EDITIONS["OL7353617M"].items() if k != "price"}
    paged = m.client.get("/people/george08/lists/OL1L/editions?limit=1&offset=1").json()
    assert paged["size"] == 2 and [e["key"] for e in paged["entries"]] == ["/books/OL1M"]
    # an edition seed without a record still counts in size and occupies its page slot (legacy semantics)
    m.client.post("/people/george08/lists/OL3L/seeds", json={"add": ["/books/OL999M", "/books/OL1M"]})
    body = m.client.get("/people/george08/lists/OL3L/editions?limit=1").json()
    assert body["size"] == 2 and body["entries"] == []
    body = m.client.get("/people/george08/lists/OL3L/editions?offset=1").json()
    assert [e["key"] for e in body["entries"]] == ["/books/OL1M"]


@pytest.mark.parametrize("olid,q", [("OL1M", 3), ("OL2M", 3), ("OL4M", 3), ("OL7353617M", 100), ("OL5M", 1)])
def test_quote_sale_exact_floats(m, olid, q):
    from targets.common.dataset import SEED_EDITIONS

    ed = SEED_EDITIONS[olid]
    r = m.client.get(f"/books/{olid}/quote?quantity={q}")
    assert r.status_code == 200 and _ctype(r) == "application/json"
    assert r.json() == {"key": f"/books/{olid}", "title": ed["title"], "unit_price": ed["price"], "quantity": q,
                        "total": round(ed["price"] * q, 2), "currency": "USD", "type": "sale"}
    if q == 1:
        assert m.client.get(f"/books/{olid}/quote").json()["quantity"] == 1


def test_quote_over_max_is_400(m):
    r = m.client.get("/books/OL2M/quote?quantity=101")
    assert r.status_code == 400 and r.json() == {"error": "quantity_out_of_range", "min": -10, "max": 100}


def test_post_seeds_success(m):
    r = m.client.post("/people/george08/lists/OL2L/seeds",
                      json={"add": ["/works/OL9W", "/works/OL27258W"], "remove": ["/books/OL2M"]})
    assert r.status_code == 200 and r.json() == {"success": True, "size": 2, "added": 1, "removed": 1}
    entries = m.client.get("/people/george08/lists/OL2L/seeds").json()["entries"]
    assert [e["url"] for e in entries] == ["/works/OL27258W", "/works/OL9W"]
    lst = [e for e in m.client.get("/people/george08/lists").json()["entries"] if e["url"].endswith("OL2L")][0]
    assert lst["last_update"] == "2025-01-01T00:00:00"
    r = m.client.post("/people/george08/lists/OL3L/seeds", json={"add": ["/works/OL1W", "/works/OL1W"]})
    assert r.json() == {"success": True, "size": 1, "added": 1, "removed": 0}


def test_post_invalid_seed_first_offender(m):
    r = m.client.post("/people/george08/lists/OL2L/seeds", json={"add": ["bogus"]})
    assert r.status_code == 400 and r.json() == {"error": "invalid_seed", "seed": "bogus"}
    r = m.client.post("/people/george08/lists/OL2L/seeds", json={"add": ["/works/OL1W", "a1"], "remove": ["r1"]})
    assert r.json() == {"error": "invalid_seed", "seed": "a1"}
    r = m.client.post("/people/george08/lists/OL2L/seeds", json={"remove": ["r1"]})
    assert r.json() == {"error": "invalid_seed", "seed": "r1"}
    # nothing applied
    assert m.client.get("/people/george08/lists/OL2L/seeds").json()["size"] == 2


# ---------------------------------------------------------------- D-1 json_suffix

@pytest.mark.parametrize("path", ["/people/george08/lists", "/people/george08/lists/OL1L/seeds",
                                  "/people/george08/lists/OL1L/editions", "/books/OL1M/quote"])
def test_D1_json_suffix(m, path):
    r = m.client.get(path + ".json")
    if m.preserves("json_suffix"):
        assert r.status_code == 200 and r.json() == m.client.get(path).json()
    else:
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_D1_json_suffix_post(m):
    r = m.client.post("/people/george08/lists/OL2L/seeds.json", json={"add": ["/works/OL9W"]})
    if m.preserves("json_suffix"):
        assert r.status_code == 200 and r.json() == {"success": True, "size": 3, "added": 1, "removed": 0}
    else:
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------- D-2 loose_query

@pytest.mark.parametrize("q,expected_len", [("limit=none", 3), ("limit=all", 3), ("limit=", 3), ("limit=abc", 3),
                                            ("offset=none", 3), ("limit=2&offset=1", 2)])
def test_D2_loose_limit(m, q, expected_len):
    r = m.client.get(f"/people/george08/lists?{q}")
    if m.preserves("loose_query") or q == "limit=2&offset=1":
        assert r.status_code == 200 and len(r.json()["entries"]) == expected_len
    else:
        assert r.status_code == 422 and _ctype(r) == "application/json" and "detail" in r.json()


@pytest.mark.parametrize("v,detailed", [("yes", True), ("1", True), ("maybe", True), ("", False), ("0", False),
                                         ("false", False), ("NO", False), ("none", False)])
def test_D2_loose_details(m, v, detailed):
    r = m.client.get(f"/people/george08/lists?details={v}")
    strictly_valid = v.lower() in {"yes", "1", "0", "false", "no"}
    if m.preserves("loose_query") or strictly_valid:
        e = r.json()["entries"][0]
        assert ("seeds" in e) is detailed and ("description" in e) is detailed
    else:
        assert r.status_code == 422


def test_D2_loose_editions_and_quantity(m):
    r = m.client.get("/people/openlibrary/lists/OL10L/editions?limit=none")
    q = m.client.get("/books/OL2M/quote?quantity=abc")
    if m.preserves("loose_query"):
        assert r.status_code == 200 and len(r.json()["entries"]) == 3
        assert q.status_code == 200 and q.json()["quantity"] == 1
        assert m.client.get("/books/OL2M/quote?quantity=all").json()["quantity"] == 1
        assert m.client.get("/books/OL2M/quote?quantity=%205%20").json()["quantity"] == 5
    else:
        assert r.status_code == 422 and q.status_code == 422


# ---------------------------------------------------------------- D-3 missing_resource_200

@pytest.mark.parametrize("path", ["/people/george08/lists/OL999L/seeds", "/people/nobody/lists/OL1L/seeds",
                                  "/people/george08/lists/OL999L/editions"])
def test_D3_missing_resource(m, path):
    r = m.client.get(path)
    assert r.json() == {"error": "not_found", "entries": []} and _ctype(r) == "application/json"
    assert r.status_code == (200 if m.preserves("missing_resource_200") else 404)


@pytest.mark.parametrize("body", [{"add": ["/works/OL1W"]}, {}, {"add": ["bogus"]}])
def test_D3_post_missing_list_checked_first(m, body):
    r = m.client.post("/people/george08/lists/OL999L/seeds", json=body)
    assert r.json() == {"error": "not_found", "entries": []}
    assert r.status_code == (200 if m.preserves("missing_resource_200") else 404)


# ---------------------------------------------------------------- D-4 non_positive_quantity

@pytest.mark.parametrize("q,typ", [(-1, "credit_reversal"), (-10, "credit_reversal"), (0, "noop")])
def test_D4_non_positive_quantity(m, q, typ):
    r = m.client.get(f"/books/OL4M/quote?quantity={q}")
    if m.preserves("non_positive_quantity"):
        assert r.status_code == 200
        assert r.json()["type"] == typ and r.json()["quantity"] == q and r.json()["total"] == round(3.33 * q, 2)
    else:
        assert r.status_code == 422 and r.json()["detail"][0]["type"] == "greater_than"


def test_D4_fastapi_shaped_422(m):
    r = m.client.get("/books/OL4M/quote?quantity=0")
    if not m.preserves("non_positive_quantity"):
        assert r.status_code == 422 and r.json() == FASTAPI_GT0_422 and _ctype(r) == "application/json"


def test_D4_below_min(m):
    r = m.client.get("/books/OL2M/quote?quantity=-11")
    if m.preserves("non_positive_quantity"):
        assert r.status_code == 400 and r.json() == {"error": "quantity_out_of_range", "min": -10, "max": 100}
    else:
        assert r.status_code == 422


# ---------------------------------------------------------------- D-5 empty_body_500

@pytest.mark.parametrize("body", [{}, {"add": []}, {"add": [], "remove": []}])
def test_D5_empty_body(m, body):
    r = m.client.post("/people/george08/lists/OL1L/seeds", json=body)
    if m.preserves("empty_body_500"):
        assert r.status_code == 500 and _ctype(r) == "text/html" and r.text == "internal server error"
    else:
        assert r.status_code == 422
        assert r.json() == {"detail": "add or remove must contain at least one seed"}


def test_D5_no_body_at_all(m):
    r = m.client.post("/people/george08/lists/OL1L/seeds")
    assert r.status_code == (500 if m.preserves("empty_body_500") else 422)
