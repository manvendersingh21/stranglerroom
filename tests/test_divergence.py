"""Tests for the divergence engine (``agents/divergence.py``, SPEC section 7).

Everything here is self-contained: unit tests use hand-built observations, and the
campaign tests run against two tiny in-process Starlette apps (served by uvicorn on
free ports in background threads) that emulate exactly one ``json_suffix`` and one
``loose_query`` divergence.  The real legacy/modern targets are not used.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agents.divergence import (
    QUIRK_TAGS,
    Observation,
    Probe,
    Registry,
    classify,
    compare,
    deterministic_corpus,
    generated_post_probes,
    generated_probes,
    observe,
    run_campaign,
)
from tests.procs import free_port


def J(status: int, body, ctype: str = "application/json") -> Observation:
    """Shorthand for a JSON observation."""
    return Observation(status=status, content_type=ctype, body=body)


def T(status: int, text: str, ctype: str = "text/html") -> Observation:
    """Shorthand for a text observation."""
    return Observation(status=status, content_type=ctype, body=text)


# --------------------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------------------


def test_probe_template_and_mutating():
    assert Probe("GET", "/people/george08/lists/OL1L/seeds.json").url_template == \
        "/people/{user}/lists/{list_id}/seeds"
    assert Probe("GET", "/people/george08/lists.json").url_template == "/people/{user}/lists"
    assert Probe("GET", "/people/x/lists/OL1L/editions").url_template == "/people/{user}/lists/{list_id}/editions"
    assert Probe("GET", "/books/OL1M/quote.json").url_template == "/books/{olid}/quote"
    assert not Probe("GET", "/x").mutating
    assert Probe("POST", "/x", body={}).mutating
    assert Probe("GET", "/books/OL1M/quote", (("quantity", "2"),)).describe() == "GET /books/OL1M/quote?quantity=2"


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------


def test_compare_identical_is_empty():
    body = {"a": 1, "b": [1, 2.5, {"c": None}], "d": True}
    assert compare(J(200, body), J(200, json.loads(json.dumps(body)))) == []
    assert compare(T(500, "internal server error"), T(500, "internal server error")) == []


def test_compare_status_and_content_type():
    lines = compare(J(200, {"a": 1}), T(404, "nope"))
    assert lines[0] == "status: legacy 200 != modern 404"
    assert lines[1].startswith("content-type:") and "application/json" in lines[1] and "text/html" in lines[1]
    assert len(lines) == 2  # mixed JSON/text bodies are not body-diffed


def test_compare_missing_extra_and_nested_paths():
    legacy = {"links": {"self": "/a", "list": "/b"}, "entries": [{"url": "/w/1"}, {"url": "/w/2"}]}
    modern = {"links": {"self": "/a"}, "entries": [{"url": "/w/1"}, {"url": "/w/X"}], "detail": "x"}
    lines = compare(J(200, legacy), J(200, modern))
    assert any(ln.startswith("missing key /links/list") for ln in lines)
    assert any(ln.startswith("extra key /detail") for ln in lines)
    assert any(ln.startswith("value /entries/1/url") for ln in lines)


def test_compare_type_changes_int_float_bool():
    lines = compare(J(200, {"t": 1, "u": 1, "v": "1"}), J(200, {"t": 1.0, "u": True, "v": 1}))
    assert any("type /t" in ln and "int" in ln and "float" in ln for ln in lines)
    assert any("type /u" in ln and "bool" in ln for ln in lines)
    assert any("type /v" in ln and "string" in ln for ln in lines)


def test_compare_floats_exact_and_list_length():
    lines = compare(J(200, {"total": 0.30000000000000004, "e": [1, 2, 3]}), J(200, {"total": 0.3, "e": [1, 2]}))
    assert any(ln.startswith("value /total") for ln in lines)
    assert any(ln.startswith("length /e: legacy 3 != modern 2") for ln in lines)


def test_compare_caps_at_eight_lines():
    legacy = {f"k{i}": i for i in range(30)}
    modern = {f"k{i}": i + 1 for i in range(30)}
    assert len(compare(J(200, legacy), J(200, modern))) == 8


def test_compare_text_bodies_only_when_status_equal():
    assert compare(T(500, "boom"), T(500, "bang")) == ["body text: legacy \"boom\" != modern \"bang\""]
    assert compare(T(500, "boom"), T(502, "bang")) == ["status: legacy 500 != modern 502"]


def test_compare_transport_errors_never_identical():
    down = Observation(status=0, content_type="", body=None, error="ConnectError: refused")
    assert compare(down, down) != []


# --------------------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------------------

NOT_FOUND_BODY = {"error": "not_found", "entries": []}
NOT_FOUND_404 = J(404, {"detail": "Not Found"})
V422 = J(422, {"detail": [{"msg": "bad"}]})


def test_classify_rule1_json_suffix():
    p = Probe("GET", "/people/george08/lists.json")
    assert classify(p, J(200, {"size": 3}), NOT_FOUND_404) == "json_suffix"
    # legacy application 404 (unknown user) vs modern routing 404 -> still the missing .json route
    assert classify(p, J(404, {"error": "not_found", "key": "/people/ghost"}), NOT_FOUND_404) == "json_suffix"
    # ...but a legacy 404 that is not the application payload is not a suffix quirk
    assert classify(p, T(404, "not found"), NOT_FOUND_404).startswith("unclassified:")
    # ...nor is a modern application not_found 404 that merely differs in body
    assert classify(p, J(404, {"error": "not_found", "key": "a"}),
                    J(404, {"error": "not_found", "key": "b"})).startswith("unclassified:")
    # a modern 404 that is NOT the routing 404 means the .json route exists and answered with the
    # wrong body: a real divergence that must never be absorbed into D-1
    for wrong in (J(404, {"detail": "User not found"}), T(404, "nope", "text/plain"), J(404, {"error": "missing"})):
        assert classify(p, J(404, {"error": "not_found", "key": "/people/ghost"}), wrong).startswith("unclassified:")
        assert classify(p, J(200, {"size": 3}), wrong).startswith("unclassified:")


def test_classify_rule1_precedence_over_loose_param():
    p = Probe("GET", "/people/george08/lists.json", (("limit", "none"),), label="loose_query")
    assert classify(p, J(200, {"size": 3}), NOT_FOUND_404) == "json_suffix"


def test_classify_json_path_with_app_not_found_is_missing_resource():
    # modern already serves the .json alias and answers with its own not_found payload
    p = Probe("GET", "/people/george08/lists/OL99L/seeds.json")
    assert classify(p, J(200, NOT_FOUND_BODY), J(404, NOT_FOUND_BODY)) == "missing_resource_200"
    # ...but a routing 404 on the same path is still the suffix quirk
    assert classify(p, J(200, NOT_FOUND_BODY), NOT_FOUND_404) == "json_suffix"


def test_classify_rule2_empty_body_500():
    legacy = T(500, "internal server error")
    for body in ({}, {"add": []}, {"add": [], "remove": []}, None):
        p = Probe("POST", "/people/george08/lists/OL1L/seeds", body=body)
        assert classify(p, legacy, V422) == "empty_body_500"
    p = Probe("POST", "/people/george08/lists/OL1L/seeds", body={"add": ["/books/OL1M"]})
    assert classify(p, legacy, V422).startswith("unclassified:POST:")


def test_classify_rule3_missing_resource():
    p = Probe("GET", "/people/george08/lists/OL99L/seeds")
    assert classify(p, J(200, NOT_FOUND_BODY), J(404, NOT_FOUND_BODY)) == "missing_resource_200"
    post = Probe("POST", "/people/george08/lists/OL99L/seeds", body={"add": ["/books/OL1M"]})
    assert classify(post, J(200, NOT_FOUND_BODY), J(404, NOT_FOUND_BODY)) == "missing_resource_200"


def test_classify_rule4_non_positive_quantity():
    for q in ("-1", "-10", "0", "-15", " -3 "):
        p = Probe("GET", "/books/OL1M/quote", (("quantity", q),))
        legacy = J(400, {"error": "quantity_out_of_range"}) if q == "-15" else J(200, {"quantity": int(q)})
        assert classify(p, legacy, V422) == "non_positive_quantity", q


def test_classify_rule5_loose_query():
    p = Probe("GET", "/books/OL1M/quote", (("quantity", "abc"),))
    assert classify(p, J(200, {"quantity": 1}), V422) == "loose_query"
    p = Probe("GET", "/people/george08/lists", (("limit", "none"),))
    assert classify(p, J(200, {"size": 3}), V422) == "loose_query"
    assert classify(p, J(404, {"error": "not_found"}), V422) == "loose_query"
    # legacy 422 or 5xx does not qualify
    assert classify(p, V422, J(422, {"detail": "other"})).startswith("unclassified:")
    assert classify(p, T(500, "boom"), V422).startswith("unclassified:")


def test_classify_rule6_unclassified_format():
    p = Probe("GET", "/people/george08/lists/OL1L/seeds", label="json_suffix")  # label is ignored
    tag = classify(p, J(200, {"size": 5}), J(200, {"size": 4}))
    assert tag == "unclassified:GET:/people/{user}/lists/{list_id}/seeds:200:200"


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


def test_registry_first_come_and_persistent(tmp_path):
    path = tmp_path / "state" / "divergence_registry.json"
    reg = Registry(path)
    assert reg.all() == {}
    assert reg.id_for("json_suffix", 404) == "D-1"
    assert reg.id_for("loose_query", 422) == "D-2"
    assert reg.id_for("json_suffix", 500) == "D-1"  # stable, discovery status not overwritten
    assert reg.get("json_suffix")["modern_status_at_discovery"] == 404

    again = Registry(path)
    assert again.id_for("loose_query", 400) == "D-2"
    assert again.id_for("unclassified:GET:/x:200:201", 201) == "D-3"
    assert list(again.all()) == ["json_suffix", "loose_query", "unclassified:GET:/x:200:201"]
    assert again.get("nope") is None

    again.reset()
    assert not path.exists()
    assert Registry(path).id_for("loose_query", 422) == "D-1"


# --------------------------------------------------------------------------------------
# Corpus / generated probes
# --------------------------------------------------------------------------------------


def synthetic_v1(probe: Probe) -> tuple[Observation, Observation]:
    """Fake (legacy, modern-v1) outcomes driven by request features (SPEC section 3)."""
    ok = J(200, {"ok": True, "path": probe.path})
    tmpl = probe.url_template
    if probe.path.endswith(".json"):
        return ok, NOT_FOUND_404
    if probe.method == "POST" and not (probe.body or {}).get("add") and not (probe.body or {}).get("remove"):
        return T(500, "internal server error"), V422
    if "{list_id}" in tmpl and ("OL99L" in probe.path or "/nobody/" in probe.path):
        return J(200, NOT_FOUND_BODY), J(404, NOT_FOUND_BODY)
    if tmpl == "/books/{olid}/quote":
        q = probe.param("quantity")
        if q is not None and q.lstrip("-").isdigit() and -10 <= int(q) <= 0:
            return ok, V422
    if probe.method == "GET" and any(v in ("none", "", "maybe", "abc") for _, v in probe.params):
        return ok, V422
    return ok, J(200, {"ok": True, "path": probe.path})


def test_corpus_shape_and_d1_to_d5_order(tmp_path):
    corpus = deterministic_corpus()
    reg = Registry(tmp_path / "reg.json")
    tags_in_order: list[str] = []
    baseline_diffs = []
    for probe in corpus:
        legacy, modern = synthetic_v1(probe)
        if not compare(legacy, modern):
            if probe.label.startswith("baseline"):
                continue
            pytest.fail(f"quirk probe produced no divergence: {probe.describe()}")
        if probe.label.startswith("baseline"):
            baseline_diffs.append(probe)
        tag = classify(probe, legacy, modern)
        assert tag == probe.label, probe.describe()
        reg.id_for(tag, modern.status)
        if tag not in tags_in_order:
            tags_in_order.append(tag)
    assert baseline_diffs == []
    assert tags_in_order == list(QUIRK_TAGS)
    assert {tag: e["id"] for tag, e in reg.all().items()} == {t: f"D-{i}" for i, t in enumerate(QUIRK_TAGS, 1)}

    counts = {t: sum(p.label == t for p in corpus) for t in QUIRK_TAGS}
    assert all(c >= 3 for c in counts.values()), counts
    assert sum(p.label.startswith("baseline") for p in corpus) >= 8
    assert any(p.mutating and p.path.endswith(".json") for p in corpus)


def test_generated_probes_deterministic():
    a, b = generated_probes(60, seed=7), generated_probes(60, seed=7)
    assert len(a) == 60 and [p.key() for p in a] == [p.key() for p in b]
    assert [p.key() for p in generated_probes(60, seed=8)] != [p.key() for p in a]
    assert len(generated_probes()) == 120
    assert all(p.method == "GET" for p in a)
    templates = {p.url_template for p in generated_probes()}
    assert templates == {"/people/{user}/lists", "/people/{user}/lists/{list_id}/seeds",
                         "/people/{user}/lists/{list_id}/editions", "/books/{olid}/quote"}
    big = generated_probes()
    assert any(p.path.endswith(".json") for p in big) and any(not p.path.endswith(".json") for p in big)
    assert any("/nobody/" in p.path or "/ghost_42/" in p.path for p in big)


def test_generated_post_probes():
    posts = generated_post_probes(12, seed=1337)
    assert len(posts) == 12
    assert [p.key() for p in posts] == [p.key() for p in generated_post_probes(12, seed=1337)]
    for p in posts:
        assert p.method == "POST" and p.url_template == "/people/{user}/lists/{list_id}/seeds"
        assert isinstance(p.body, dict) and set(p.body) <= {"add", "remove"}
        assert all(isinstance(v, list) and all(isinstance(s, str) for s in v) for v in p.body.values())


# --------------------------------------------------------------------------------------
# Campaign against in-process fake targets
# --------------------------------------------------------------------------------------

INT_PARAMS = ("limit", "offset", "quantity")
STRICT_BOOLS = {"true", "false", "1", "0", "yes", "no", "on", "off"}


def make_fake(name: str, flags: dict) -> tuple[Starlette, dict]:
    """A fake target. ``flags``: ``json_suffix`` (serve .json aliases), ``loose`` (coerce params).

    Legacy has both flags on; the fake "modern v1" has both off, giving exactly one
    json_suffix and one loose_query divergence.  Everything else is identical.
    """
    stats = {"resets": 0, "requests": 0}

    async def reset(_: Request) -> Response:
        stats["resets"] += 1
        return JSONResponse({"reset": True})

    async def charset(_: Request) -> Response:
        return Response('{"x": 1}', media_type="application/json; charset=UTF-8")

    async def catch_all(request: Request) -> Response:
        stats["requests"] += 1
        path = request.url.path
        if path.endswith(".json"):
            if not flags["json_suffix"]:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            path = path[:-5]
        coerced: dict = {}
        for key, value in request.query_params.multi_items():
            if key in INT_PARAMS:
                try:
                    coerced[key] = int(value.strip())
                except ValueError:
                    if not flags["loose"]:
                        return JSONResponse({"detail": [{"loc": ["query", key]}]}, status_code=422)
                    coerced[key] = None
            elif key == "details":
                if value.strip().lower() not in STRICT_BOOLS and not flags["loose"]:
                    return JSONResponse({"detail": [{"loc": ["query", key]}]}, status_code=422)
                coerced[key] = value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
        body = await request.body()
        payload = {"route": path, "method": request.method, "params": coerced,
                   "body": json.loads(body) if body else None, "price_total": round(0.1 * 3, 2)}
        return JSONResponse(payload)

    routes = [Route("/_admin/reset", reset, methods=["POST"]), Route("/charset", charset),
              Route("/{path:path}", catch_all, methods=["GET", "POST"])]
    return Starlette(routes=routes), stats


@contextmanager
def serve(app):
    """Run ``app`` with uvicorn on a free port in a daemon thread."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off", access_log=False, ws="none"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("fake server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def fakes():
    legacy_flags = {"json_suffix": True, "loose": True}
    modern_flags = {"json_suffix": False, "loose": False}
    legacy_app, legacy_stats = make_fake("legacy", legacy_flags)
    modern_app, modern_stats = make_fake("modern", modern_flags)
    with serve(legacy_app) as legacy_url, serve(modern_app) as modern_url:
        yield {"legacy": legacy_url, "modern": modern_url, "modern_flags": modern_flags,
               "legacy_stats": legacy_stats, "modern_stats": modern_stats}


def test_observe_normalizes_and_handles_transport_errors(fakes):
    async def go():
        async with httpx.AsyncClient() as client:
            ok = await observe(client, fakes["legacy"], Probe("GET", "/charset"))
            down = await observe(client, f"http://127.0.0.1:{free_port()}", Probe("GET", "/x"))
            return ok, down

    ok, down = asyncio.run(go())
    assert (ok.status, ok.content_type, ok.body, ok.error) == (200, "application/json", {"x": 1}, None)
    assert down.status == 0 and down.error


def test_campaign_open_then_decisions(fakes, tmp_path):
    reg = Registry(tmp_path / "divergence_registry.json")
    legacy, modern = fakes["legacy"], fakes["modern"]

    # Run 1: two open clusters, numbered in corpus order.
    r1 = asyncio.run(run_campaign(legacy, modern, registry=reg, n_random=120, seed=1337))
    assert [(c.id, c.tag, c.status) for c in r1.clusters] == [("D-1", "json_suffix", "open"),
                                                                ("D-2", "loose_query", "open")]
    assert r1.total_requests == r1.identical + r1.divergent and r1.total_requests >= len(deterministic_corpus()) + 120
    assert r1.divergent > 0 and r1.identical > 0
    d1 = r1.clusters[0]
    assert d1.title == "Implicit .json suffix routing" and (d1.legacy_status, d1.modern_status) == (200, 404)
    assert d1.sample_request == "GET /people/george08/lists.json" and d1.sample_diff
    assert r1.clusters[1].modern_status == 422
    assert not r1.approved_parity and r1.unresolved == 2
    assert r1.parity_label == "Parity 0% (0/2 clusters resolved)"
    md = r1.to_markdown()
    assert "| ID | Divergence | Legacy | Modern | Hits | Sample | Decision/Status |" in md
    assert "| D-1 | Implicit .json suffix routing | 200 | 404 |" in md
    assert json.loads(json.dumps(r1.to_dict()))["clusters"][0]["id"] == "D-1"
    # every mutating probe was preceded by a reset on both targets (+ initial and final reset)
    n_posts = sum(p.mutating for p in deterministic_corpus()) + 12
    assert fakes["legacy_stats"]["resets"] == fakes["modern_stats"]["resets"] == n_posts + 2

    # Run 2: decisions applied while modern is still v1.
    decisions = {"D-1": {"decision": "preserve"}, "D-2": {"decision": "deprecate"}}
    r2 = asyncio.run(run_campaign(legacy, modern, decisions, registry=reg, n_random=40))
    statuses = {c.id: (c.decision, c.status) for c in r2.clusters}
    assert statuses == {"D-1": ("preserve", "preserved-regressed"), "D-2": ("deprecate", "deprecated-ok")}
    assert r2.parity_label == "Parity 50% (1/2 clusters resolved)" and not r2.approved_parity

    # Run 3: modern now serves .json aliases -> preserved cluster still listed, 0 hits.
    fakes["modern_flags"]["json_suffix"] = True
    r3 = asyncio.run(run_campaign(legacy, modern, decisions, registry=reg, n_random=40))
    d1, d2 = r3.clusters
    assert (d1.id, d1.count, d1.status) == ("D-1", 0, "preserved-ok")
    assert (d2.id, d2.status) == ("D-2", "deprecated-ok") and d2.count > 0
    assert r3.approved_parity and r3.parity_label == "100% Approved Parity (2/2)"
    assert r3.resolved == 2 and r3.unresolved == 0

    # Run 4: modern silently drops the deprecated 422 contract (matches legacy again).
    fakes["modern_flags"]["loose"] = True
    r4 = asyncio.run(run_campaign(legacy, modern, decisions, registry=reg, n_random=40))
    assert r4.divergent == 0
    assert {c.id: c.status for c in r4.clusters} == {"D-1": "preserved-ok", "D-2": "deprecated-violated"}
    assert not r4.approved_parity

    # Without decisions, a cluster that converged to legacy behaviour is resolved.
    r5 = asyncio.run(run_campaign(legacy, modern, registry=reg, n_random=20))
    assert {c.id: c.status for c in r5.clusters} == {"D-1": "converged", "D-2": "converged"}
    assert r5.approved_parity


def test_campaign_unreachable_target_raises(tmp_path, fakes):
    from agents.divergence import CampaignError

    with pytest.raises(CampaignError):
        asyncio.run(run_campaign(fakes["legacy"], f"http://127.0.0.1:{free_port()}",
                                 registry=Registry(tmp_path / "r.json"), n_random=5))
