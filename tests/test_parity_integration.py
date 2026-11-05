"""End-to-end StranglerRoom loop *without* Switch (docs/SPEC.md §3, §4, §7).

Real legacy (web.py) and real modern (FastAPI, reloader ON) targets run as subprocesses
on free ports; the modern target serves a generated ``lists.py`` from a tmp file
(``MODERN_LISTS_PATH``), never the repo's ``targets/modern_fastapi/app/lists.py``.

The test walks the demo storyline:

1. naive v1 module (empty policy) -> the divergence engine finds exactly the five SPEC §3
   quirks, numbered ``D-1`` .. ``D-5`` in quirk-table order, all ``open``;
2. the human decides (preserve D-1/D-2/D-4, deprecate D-3/D-5), the translator regenerates
   the module, the reloader picks it up (``/_meta`` rev) and a second campaign on the *same*
   registry reports ``100% Approved Parity (5/5)``;
3. negative control: an all-preserve module while D-3 is decided ``deprecate`` breaks the
   approved contract -> ``deprecated-violated`` and no approval.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

from agents.codegen import QUIRK_TAGS, module_rev, render_lists_module
from agents.divergence import CampaignReport, Registry, run_campaign
from tests.procs import free_port, service

#: Demo decisions as the generator's quirk policy (SPEC §3).
DEMO_POLICY = {"json_suffix": "preserve", "loose_query": "preserve", "non_positive_quantity": "preserve",
               "missing_resource_200": "deprecate", "empty_body_500": "deprecate"}
ALL_PRESERVE_POLICY = {tag: "preserve" for tag in QUIRK_TAGS}

#: The same demo decisions as cluster decisions (the fuzzer's input after the human's commands).
DEMO_DECISIONS = {"D-1": {"decision": "preserve"}, "D-2": {"decision": "preserve"},
                  "D-3": {"decision": "deprecate"}, "D-4": {"decision": "preserve"},
                  "D-5": {"decision": "deprecate"}}

EXPECTED_IDS = ["D-1", "D-2", "D-3", "D-4", "D-5"]
EXPECTED_TAGS = list(QUIRK_TAGS)  # json_suffix, loose_query, missing_resource_200, non_positive_quantity, empty_body_500

RELOAD_TIMEOUT_S = 45.0


def wait_for_rev(url: str, source: str, timeout: float = RELOAD_TIMEOUT_S) -> None:
    """Block until the modern target serves ``source`` (``/_meta`` rev) with its routes mounted."""
    want = module_rev(source)
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            meta = httpx.get(url + "/_meta", timeout=2).json()
            last = meta
            if meta.get("rev") == want and "import_error" not in meta and len(meta.get("routes", [])) > 1:
                return
        except (httpx.HTTPError, ValueError) as exc:  # worker restarting
            last = exc
        time.sleep(0.25)
    pytest.fail(f"modern target did not serve rev {want} within {timeout:.0f}s; last /_meta: {last!r}")


def deploy(lists_path: Path, url: str, policy: dict[str, str]) -> str:
    """Write the module for ``policy`` (as @translator does) and wait for the reloader to serve it.

    Identical bytes are not rewritten: the write itself would trigger a reload that
    ``wait_for_rev`` cannot tell apart from the already-serving worker (same rev), so a
    campaign could start against a restarting target.
    """
    source = render_lists_module(policy)
    if not lists_path.is_file() or lists_path.read_text(encoding="utf-8") != source:
        tmp = lists_path.with_name(lists_path.name + ".tmp")  # atomic: one reload, never a torn file
        tmp.write_text(source, encoding="utf-8")
        os.replace(tmp, lists_path)
    wait_for_rev(url, source)
    return source


def campaign(stack: dict, registry: Registry, decisions: dict | None = None) -> CampaignReport:
    """Default-size campaign (corpus + 120 generated GETs + 12 generated POSTs)."""
    return asyncio.run(run_campaign(stack["legacy"], stack["modern"], decisions, registry=registry))


def summary(report: CampaignReport) -> str:
    return report.to_markdown()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """Legacy + modern (reloader on) on free ports; modern starts on the naive v1 module."""
    base = tmp_path_factory.mktemp("parity")
    lists_dir = base / "modern_lists"  # its own dir: the reloader watches it
    lists_dir.mkdir()
    lists_path = lists_dir / "lists.py"
    naive = render_lists_module({})
    lists_path.write_text(naive, encoding="utf-8")
    legacy_log = base / "legacy.log"
    with service("targets.legacy_openlibrary.server", free_port(), args=["--log-file", str(legacy_log)]) as legacy, \
            service("targets.modern_fastapi.server", free_port(),
                    env={"MODERN_LISTS_PATH": str(lists_path), "MODERN_NO_RELOAD": "0"}) as modern:
        wait_for_rev(modern, naive)
        yield {"legacy": legacy, "modern": modern, "lists_path": lists_path, "base": base}


def test_parity_loop_naive_then_demo_policy(stack, tmp_path):
    """Naive v1 -> five open clusters; demo policy + decisions -> 100% Approved Parity."""
    registry = Registry(tmp_path / "divergence_registry.json")

    # ---- 1. naive v1 ----------------------------------------------------------------------
    deploy(stack["lists_path"], stack["modern"], {})
    first = campaign(stack, registry)
    md = summary(first)
    assert [c.id for c in first.clusters] == EXPECTED_IDS, md
    assert [c.tag for c in first.clusters] == EXPECTED_TAGS, md
    assert not any(c.tag.startswith("unclassified:") for c in first.clusters), md
    assert all(c.status == "open" for c in first.clusters), md
    assert all(c.count > 0 and c.decision is None for c in first.clusters), md
    assert first.total_requests >= 120, md
    assert first.divergent > 0 and first.identical > 0, md
    assert first.approved_parity is False, md
    assert first.unresolved == 5 and first.resolved == 0, md
    assert first.parity_label == "Parity 0% (0/5 clusters resolved)", md
    # IDs are persisted in the registry, in quirk order.
    assert {tag: entry["id"] for tag, entry in registry.all().items()} == dict(zip(EXPECTED_TAGS, EXPECTED_IDS))

    # ---- 2. demo policy + human decisions ---------------------------------------------------
    deploy(stack["lists_path"], stack["modern"], DEMO_POLICY)
    second = campaign(stack, registry, DEMO_DECISIONS)
    md = summary(second)
    assert [c.id for c in second.clusters] == EXPECTED_IDS, md  # same registry: no renumbering, nothing new
    statuses = {c.id: c.status for c in second.clusters}
    assert statuses == {"D-1": "preserved-ok", "D-2": "preserved-ok", "D-3": "deprecated-ok",
                        "D-4": "preserved-ok", "D-5": "deprecated-ok"}, md
    assert second.approved_parity is True, md
    assert second.parity_label == "100% Approved Parity (5/5)", md
    assert second.resolved == 5 and second.unresolved == 0, md
    # Preserved quirks are reproduced exactly (0 hits); deprecated ones still diverge, as approved.
    by_id = {c.id: c for c in second.clusters}
    assert all(by_id[i].count == 0 for i in ("D-1", "D-2", "D-4")), md
    assert all(by_id[i].count > 0 for i in ("D-3", "D-5")), md
    assert second.total_requests == first.total_requests, md


def test_negative_control_preserving_a_deprecated_quirk_is_a_violation(stack, tmp_path):
    """All-preserve module while D-3 is decided 'deprecate' -> D-3 deprecated-violated, no approval."""
    registry = Registry(tmp_path / "divergence_registry.json")
    deploy(stack["lists_path"], stack["modern"], {})
    baseline = campaign(stack, registry)  # discovers D-1..D-5 (modern_status_at_discovery recorded)
    assert [c.id for c in baseline.clusters] == EXPECTED_IDS, summary(baseline)

    deploy(stack["lists_path"], stack["modern"], ALL_PRESERVE_POLICY)
    decisions = {"D-1": "preserve", "D-2": "preserve", "D-3": "deprecate", "D-4": "preserve", "D-5": "preserve"}
    report = campaign(stack, registry, decisions)
    md = summary(report)
    statuses = {c.id: c.status for c in report.clusters}
    assert statuses["D-3"] == "deprecated-violated", md
    assert all(statuses[i] == "preserved-ok" for i in ("D-1", "D-2", "D-4", "D-5")), md
    assert report.cluster("D-3").count == 0, md  # modern now matches legacy, i.e. not the approved contract
    assert report.approved_parity is False, md
    assert report.unresolved == 1, md
    assert report.parity_label == "Parity 80% (4/5 clusters resolved)", md
