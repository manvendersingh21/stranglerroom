"""Extra edge-case tests for the ``@fuzzer`` room agent (``agents/fuzzer_agent.py``).

Most cases deliberately avoid real targets: ``/_meta`` is served by a tiny in-process HTTP stub on a
free port and the legacy side points at a closed port, so the campaign path fails loudly and quickly.
The last test reuses the real-target fixture from :mod:`tests.test_fuzzer_agent`.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agents import ledger
from agents.commands import MARK, re_verify, shadow_test
from agents.divergence import CampaignReport, Cluster
from tests.fakes import FakeMessage, FakeRoom
from tests.procs import free_port
from tests.test_fuzzer_agent import (  # noqa: F401 - `targets` is a pytest fixture
    MIG_A,
    MIG_B,
    SETTINGS,
    assert_mention_hygiene,
    fuzzer_posts,
    make_fuzzer,
    matrix_state,
    targets,
    translator_says,
    write_ledger,
)

REV = "abcdef1234567890"


# ------------------------------------------------------------------------------------ tiny stubs

def _closed_url() -> str:
    """A URL for a free port nothing is listening on (connection refused)."""
    return f"http://127.0.0.1:{free_port()}"


def _targets_dict(modern_url: str, rev: str, *, legacy_url: str | None = None) -> dict:
    return {"legacy": legacy_url or _closed_url(), "naive": (modern_url, rev)}


@contextlib.contextmanager
def _meta_server(payload):
    """Serve ``GET /_meta`` with the JSON returned by ``payload`` and accept ``/_admin/reset``."""
    port = free_port()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.split("?", 1)[0] == "/_meta":
                self._send(200, payload())
            else:
                self._send(404, {"detail": "Not Found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.split("?", 1)[0] == "/_admin/reset":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"detail": "Not Found"})

        def log_message(self, *args) -> None:  # silence the test log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="fuzzer-meta-stub", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _meta(rev: str, *, delay: float = 0.0, old_rev: str = "000000000000",
          import_error: str | None = None):
    """``/_meta`` stub whose rev becomes ``rev`` ``delay`` seconds after the first poll."""
    first_seen: list[float | None] = [None]

    def payload() -> dict:
        now = time.monotonic()
        if first_seen[0] is None:
            first_seen[0] = now
        if delay and now - first_seen[0] < delay:
            return {"rev": old_rev}
        body = {"rev": rev}
        if import_error is not None:
            body["import_error"] = import_error
        return body

    with _meta_server(payload) as url:
        yield url


# ------------------------------------------------------------------------------------ local fixtures

@pytest.fixture
def room() -> FakeRoom:
    return FakeRoom()


@pytest.fixture
def registry(tmp_path) -> Path:
    return tmp_path / "divergence_registry.json"


def _assert_no_registry(registry: Path) -> None:
    if registry.exists():
        raw = registry.read_text().strip()
        assert raw in ("", "{}"), raw


def _create_ledger_document(room: FakeRoom, content: str) -> None:
    description, instructions = ledger.DOC_META[ledger.LEDGER_DOC]
    room.agent("translator").create_document(name=ledger.LEDGER_DOC, description=description,
                                             instructions=instructions, content=content)


def _bare_ledger_state(migration: str = MIG_A) -> dict:
    return ledger.new_ledger(migration_id=migration, path="/people/{user}/lists", source="legacy_openlibrary",
                             target="FastAPI", requested_by="user", request_message_id="sw_req")


# ------------------------------------------------------------------------------------ (1) .. (4) rev waits

def test_waits_for_delayed_rev_then_reports_campaign_failure_loudly(room, registry):
    """The build appears late; the agent waits, then legacy being down is one loud ⚠️, no matrix."""
    write_ledger(room, MIG_A)
    with _meta(REV, delay=0.5) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry,
                             rev_timeout=1.5, rev_poll=0.05)
        started = time.monotonic()
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))
        elapsed = time.monotonic() - started

    posts = fuzzer_posts(room)
    assert elapsed >= 0.4, f"agent did not wait for the delayed rev (took {elapsed:.2f}s)"
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "could not run" in posts[0]
    assert "did not serve" not in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


def test_matching_rev_with_import_error_is_reported_and_runs_no_campaign(room, registry):
    with _meta(REV, import_error="Traceback: SyntaxError in lists.py") as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry,
                             rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))

    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "failed to import" in posts[0]
    assert "SyntaxError" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


def test_unreachable_meta_warns_after_timeout_with_no_answer(room, registry):
    modern = _closed_url()
    fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry,
                         rev_timeout=0.3, rev_poll=0.05)
    started = time.monotonic()
    translator_says(room, fuzzer, shadow_test(REV, MIG_A))
    elapsed = time.monotonic() - started

    posts = fuzzer_posts(room)
    assert elapsed >= 0.25, f"agent returned before its rev timeout ({elapsed:.2f}s)"
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "no answer" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


def test_upper_case_rev_matches_lower_case_meta_rev(room, registry):
    write_ledger(room, MIG_A)
    with _meta(REV.lower()) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV.lower()), registry,
                             rev_timeout=1.5, rev_poll=0.05)
        # Exercise the case-insensitive comparison directly (the parser lower-cases too).
        assert fuzzer._wait_for_rev(REV.upper()) is True
        translator_says(room, fuzzer, shadow_test(REV.upper(), MIG_A))

    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "could not run" in posts[0]
    assert "did not serve" not in posts[0]


# ------------------------------------------------------------------------------------ (5) malformed commands

def test_malformed_decisions_from_human_warn_but_from_another_agent_are_ignored(room, registry):
    fuzzer = make_fuzzer(room, _targets_dict(_closed_url(), REV), registry)
    body = (f"re-verify rev={REV} migration=MIG-1 — human decisions applied: "
            f"preserve D-1 and deprecate D-1")

    # Another agent saying exactly the same thing must be ignored, not answered.
    room.agent("cutover-gate").send_targeted(body, ["fuzzer"])
    room.pump({"fuzzer": fuzzer})
    assert fuzzer_posts(room) == []

    room.human_says(f"@fuzzer {body}")
    room.pump({"fuzzer": fuzzer})
    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(f"{MARK['error']} malformed command")
    assert "both preserve and deprecate" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None


# ------------------------------------------------------------------------------------ (6) .. (7) ledger checks

def test_handoff_with_decisions_but_no_ledger_warns(room, registry):
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry,
                             rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, re_verify(REV, MIG_A, {"D-1": "preserve"}))

    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "handoff carries decisions" in posts[0]
    assert ledger.LEDGER_DOC in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


def test_ledger_without_state_block_warns(room, registry):
    _create_ledger_document(room, "# Parity & Cutover Audit Ledger\n\nNo state block here.\n")
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry,
                             rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))

    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "no readable state block" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


def test_ledger_with_invalid_decision_values_warns(room, registry):
    state = _bare_ledger_state()
    state["decisions"] = {"D-1": {"decision": "keep"}}
    _create_ledger_document(room, ledger.render_ledger(state))
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry,
                             rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))

    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "invalid decisions" in posts[0] and "keep" in posts[0]
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


# ------------------------------------------------------------------------------------ (8) corrupt matrix

def test_corrupt_matrix_state_is_not_rehydrated(room, registry):
    description, instructions = ledger.DOC_META[ledger.MATRIX_DOC]
    room.agent("fuzzer").create_document(
        name=ledger.MATRIX_DOC, description=description, instructions=instructions,
        content=("# Parity Audit Matrix\n\n"
                 "```json stranglerroom-state\n"
                 "{this is not valid json}\n"
                 "```\n"))
    fuzzer = make_fuzzer(room, _targets_dict(_closed_url(), REV), registry)

    posts = fuzzer_posts(room)
    assert len(posts) == 1, posts
    assert posts[0].startswith(MARK["error"])
    assert "could not rehydrate" in posts[0]
    assert fuzzer.matrix == ledger.new_matrix()
    assert fuzzer.migration is None


# ------------------------------------------------------------------------------------ (9) .. (10) senders

def test_non_participant_sender_cannot_trigger_a_campaign(room, registry):
    fuzzer = make_fuzzer(room, _targets_dict(_closed_url(), REV), registry)
    msg = FakeMessage(id="sw_mallory", sender="mallory", body=shadow_test(REV, MIG_A),
                      at=time.time(), addressed=["fuzzer"])
    room._queues["fuzzer"].put(room._event(msg))
    room.pump({"fuzzer": fuzzer})

    assert fuzzer_posts(room) == []
    assert room.doc_by_name(ledger.MATRIX_DOC) is None
    _assert_no_registry(registry)


def test_status_and_help_from_another_agent_are_answered_without_mentions(room, registry):
    fuzzer = make_fuzzer(room, _targets_dict(_closed_url(), REV), registry)
    room.agent("cutover-gate").send_targeted("status", ["fuzzer"])
    room.agent("cutover-gate").send_targeted("help", ["fuzzer"])
    room.pump({"fuzzer": fuzzer})

    posts = fuzzer_posts(room)
    assert len(posts) == 2, posts
    assert posts[0].startswith("ℹ️ fuzzer status")
    assert posts[1].startswith("ℹ️ fuzzer")
    assert all("@" not in p for p in posts)
    assert_mention_hygiene(room)


# ------------------------------------------------------------------------------------ (11) real targets

def test_without_ledger_posts_notice_then_runs_campaign_with_no_decisions(room, registry, targets):
    fuzzer = make_fuzzer(room, targets, registry)
    naive_rev = targets["naive"][1]
    translator_says(room, fuzzer, shadow_test(naive_rev, MIG_A))

    posts = fuzzer_posts(room)
    assert len(posts) == 3, posts
    assert posts[0].startswith(f"ℹ️ no “{ledger.LEDGER_DOC}”")
    assert "without decisions" in posts[0]
    assert posts[1].startswith(f"{MARK['campaign']} #1 on rev `{naive_rev}`")
    assert posts[2].startswith(f"@{room.human} {MARK['decision_needed']} on D-1 D-2 D-3 D-4 D-5")

    state = matrix_state(room)
    assert state["migration"] == MIG_A
    run = state["runs"][-1]
    assert run["decisions"] == {}
    assert [(c["id"], c["status"]) for c in run["report"]["clusters"]] == [
        (f"D-{i}", "open") for i in range(1, 6)
    ]
    assert_mention_hygiene(room)


# ------------------------------------------------------------------------------------ review regressions

def test_rev_wait_never_exceeds_its_budget(room, registry):
    """A poll interval longer than the timeout is clamped to the remaining budget."""
    fuzzer = make_fuzzer(room, _targets_dict(_closed_url(), REV), registry, rev_timeout=0.3, rev_poll=10)
    started = time.monotonic()
    assert fuzzer._wait_for_rev(REV) is False
    assert time.monotonic() - started < 2.0


def test_mentions_in_import_error_are_defused(room, registry):
    with _meta(REV, import_error="failed importing @translator status") as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry, rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "failed to import" in posts[0]
    assert all(not m.addressed for m in room.messages if m.sender == "fuzzer")
    assert room.pending("translator") == 0


@pytest.mark.parametrize("raw", [[], False, None], ids=["list", "false", "missing"])
def test_ledger_decisions_must_be_a_mapping(room, registry, raw):
    state = _bare_ledger_state()
    if raw is None:
        del state["decisions"]
    else:
        state["decisions"] = raw
    _create_ledger_document(room, ledger.embed_state("# ledger", state))
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry, rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "not a mapping" in posts[0]
    _assert_no_registry(registry)


def test_ledger_conflicting_duplicate_ids_are_rejected(room, registry):
    state = _bare_ledger_state()
    state["decisions"] = {"D-1": {"decision": "preserve", "by": "user"}, "D-01": {"decision": "deprecate", "by": "user"}}
    _create_ledger_document(room, ledger.embed_state("# ledger", state))
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry, rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, re_verify(REV, MIG_A, {"D-1": "deprecate"}))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "both preserve and deprecate" in posts[0]
    _assert_no_registry(registry)


# ------------------------------------------------------------------------------------ self-review regressions

def _cluster(cid: str, tag: str, status: str = "open") -> Cluster:
    return Cluster(id=cid, tag=tag, title=tag, count=1, legacy_status=200, modern_status=404, sample_request="GET /x",
                   sample_diff=[], decision=None, status=status)


def _report(clusters: list[Cluster]) -> CampaignReport:
    return CampaignReport(total_requests=10, identical=9, divergent=1, clusters=clusters, resolved=0,
                          unresolved=len(clusters), approved_parity=False, parity_label="Parity 0%",
                          duration_s=0.1)


def test_rev_change_during_the_campaign_blocks_recording(room, registry):
    with _meta("bbbbbbbbbbbb") as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry)
        problems = fuzzer._verify_campaign(_report([]), REV, {}, {})
    assert problems and "switched to rev `bbbbbbbbbbbb`" in problems[0]


def test_stray_and_renumbered_decisions_block_approval(room, registry):
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry)
        problems = fuzzer._verify_campaign(_report([_cluster("D-1", "missing_resource_200")]), REV,
                                           {"D-1": "deprecate", "D-3": "deprecate"}, {"D-1": "json_suffix"})
    assert any("D-3" in p and "never seen" in p for p in problems)
    assert any("D-1 is `missing_resource_200` here but the human decided on `json_suffix`" in p for p in problems)


@pytest.mark.parametrize("by", ["cutover-gate", "translator", "mallory", None])
def test_ledger_decisions_not_made_by_a_human_are_refused(room, registry, by):
    state = _bare_ledger_state()
    state["decisions"] = {"D-1": {"decision": "preserve", "tag": "json_suffix", "by": by}}
    _create_ledger_document(room, ledger.embed_state("# ledger", state))
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry, rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, re_verify(REV, MIG_A, {"D-1": "preserve"}))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "not a human room member" in posts[0]
    _assert_no_registry(registry)


def test_rev_not_among_the_ledger_builds_is_refused(room, registry):
    state = _bare_ledger_state()
    state["builds"] = [{"rev": "aaaaaaaaaaaa", "mode": "deterministic", "policy": {}, "at": "x"}]
    _create_ledger_document(room, ledger.embed_state("# ledger", state))
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry, rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "not a build recorded" in posts[0]


def test_decision_example_deprecates_unclassified_clusters(room, registry):
    fuzzer = make_fuzzer(room, _targets_dict(_closed_url(), REV), registry)
    fuzzer._request_decisions(_report([_cluster("D-1", "json_suffix"),
                                       _cluster("D-6", "unclassified:GET:/x:200:500")]), REV)
    ask = fuzzer_posts(room)[-1]
    assert ask.startswith(f"@{room.human} {MARK['decision_needed']} on D-1 D-6")
    assert "`preserve D-1 and deprecate D-6`" in ask


def test_older_build_of_the_migration_is_refused(room, registry):
    """Protocol review (integrate:loop): only the ledger's LATEST build can be approved — an older one the modern
    target may still serve (stale reload) would be benchmarked and flipped, yet the translator never seals it."""
    state = _bare_ledger_state()
    state["builds"] = [{"rev": REV, "mode": "deterministic", "policy": {}, "at": "x"},
                       {"rev": "bbbbbbbbbbbb", "mode": "deterministic", "policy": {"json_suffix": "preserve"}, "at": "y"}]
    _create_ledger_document(room, ledger.embed_state("# ledger", state))
    with _meta(REV) as modern:
        fuzzer = make_fuzzer(room, _targets_dict(modern, REV), registry, rev_timeout=1.5, rev_poll=0.05)
        translator_says(room, fuzzer, shadow_test(REV, MIG_A))
    posts = fuzzer_posts(room)
    assert len(posts) == 1 and posts[0].startswith(MARK["error"]) and "older build" in posts[0], posts
    assert "`bbbbbbbbbbbb`" in posts[0] and room.pending("cutover-gate") == 0
    _assert_no_registry(registry)
