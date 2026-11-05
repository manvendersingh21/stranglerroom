"""Contract tests for agents/legacy_introspect.py — static (AST-only) route extraction from legacy web.py plugins.

API:
    @dataclass(frozen=True) class LegacyRoute:
        handler: str            # class name, e.g. "lists_json"
        method: str             # "GET" | "POST"
        regex: str              # the class-level `path` literal
        template: str           # FastAPI-style template; regex groups named from the method's positional params,
                                #   e.g. "/people/{user}/lists/{list_id}/seeds"
        params: tuple[str, ...] # path params in order (method args after self)
        query: tuple[str, ...]  # keyword names passed to web.input(...) inside that method, in source order
        encoding: str | None    # class-level `encoding` literal
        json_alias: bool        # True when encoding == "json" (infogami serves `<path>.json` too)
        reads_body: bool        # method body calls web.data()
        lineno: int             # line of the method def
    def introspect_file(path: str | Path) -> list[LegacyRoute]      # sorted by (lineno)
    def introspect_source(source: str) -> list[LegacyRoute]
    def render_route_table(routes) -> str                             # Markdown table: Handler | Method | Route | Query | .json alias
It must NOT import or execute the legacy module (pure ast).
"""
from pathlib import Path

import pytest

from agents.legacy_introspect import LegacyRoute, introspect_file, introspect_source, render_route_table

ROOT = Path(__file__).resolve().parents[1]
LISTS = ROOT / "targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py"


def test_extracts_all_legacy_routes():
    routes = introspect_file(LISTS)
    sig = [(r.handler, r.method, r.template) for r in routes]
    assert sig == [
        ("lists_json", "GET", "/people/{user}/lists"),
        ("list_seeds", "GET", "/people/{user}/lists/{list_id}/seeds"),
        ("list_seeds", "POST", "/people/{user}/lists/{list_id}/seeds"),
        ("list_editions_json", "GET", "/people/{user}/lists/{list_id}/editions"),
        ("edition_quote_json", "GET", "/books/{olid}/quote"),
    ]
    by = {(r.handler, r.method): r for r in routes}
    assert by[("lists_json", "GET")].query == ("limit", "offset", "details")
    assert by[("list_editions_json", "GET")].query == ("limit", "offset")
    assert by[("edition_quote_json", "GET")].query == ("quantity",)
    assert by[("list_seeds", "POST")].reads_body is True
    assert by[("list_seeds", "GET")].reads_body is False
    assert all(r.encoding == "json" and r.json_alias for r in routes)
    assert by[("list_seeds", "GET")].params == ("user", "list_id")
    assert by[("lists_json", "GET")].regex == r"/people/([^/]+)/lists"


def test_source_without_json_encoding_and_no_exec():
    src = '''
import os
os.system("echo SHOULD-NOT-RUN > /dev/null")  # never executed: introspection is AST-only
from x import delegate, web
class page_a(delegate.page):
    path = r"/a/([^/]+)"
    def GET(self, key):
        i = web.input(q=None)
        return "x"
class not_a_page:
    path = "/ignored"
    def GET(self):
        pass
'''
    routes = introspect_source(src)
    assert len(routes) == 1
    r = routes[0]
    assert isinstance(r, LegacyRoute)
    assert (r.handler, r.method, r.template, r.query, r.encoding, r.json_alias) == ("page_a", "GET", "/a/{key}", ("q",), None, False)


def test_render_route_table():
    table = render_route_table(introspect_file(LISTS))
    lines = table.strip().splitlines()
    assert lines[0].startswith("|") and "Handler" in lines[0] and "Route" in lines[0]
    assert len(lines) == 2 + 5
    assert "`/people/{user}/lists`" in table and "limit, offset, details" in table


@pytest.mark.parametrize("bad", ["def (:", ""])
def test_bad_or_empty_source(bad):
    if bad:
        with pytest.raises(SyntaxError):
            introspect_source(bad)
    else:
        assert introspect_source(bad) == []
