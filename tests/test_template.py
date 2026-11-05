"""Tests for strangler-room.template.yaml and scripts/validate_template.py (docs/AGENTS_SPEC.md §5).

* offline (unit): the real template passes Switch's own engine with 0 errors / 0 warnings and the StranglerRoom
  contract; the validator's contract checks catch each way the template could drift (agent resources, an
  addressed kickoff, unconditional users, agent-owned documents, a wrong bridge param, ...), both on the raw
  document (pure, fast) and through the engine (lint warnings and scenario failures must fail the stage).
  Skipped — with the reason — only when no Switch checkout exists (the engine is Switch's, not ours).
* integration: live ``POST /gateway/templates/validate`` and a real run that creates the check room, asserts its
  members and archives it (skipped with the reason when Switch is down or the agents are not provisioned).
"""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_template as vt  # noqa: E402

TEMPLATE = ROOT / "strangler-room.template.yaml"
SCRIPT = ROOT / "scripts" / "validate_template.py"


def _switch_core_or_skip() -> Path:
    try:
        return vt.find_switch_core()
    except vt.EnvironmentMissing as exc:
        pytest.skip(f"Switch template engine unavailable: {exc}")


def _doc() -> dict:
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _room(doc: dict) -> dict:
    return next(iter(doc["resources"].values()))


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "mutated.template.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------ the real file

def test_agent_owned_docs_match_the_ledger_module():
    from agents.ledger import LEDGER_DOC, MATRIX_DOC, SCORECARD_DOC
    assert vt.AGENT_OWNED_DOCS == [LEDGER_DOC, MATRIX_DOC, SCORECARD_DOC]


def test_template_document_meets_the_contract():
    assert vt.check_document(_doc()) == []


def test_template_never_writes_a_mention():
    # Mention hygiene: nothing the template posts or shows may address a participant.
    assert "@" not in TEMPLATE.read_text(encoding="utf-8")


def test_offline_validation_passes():
    _switch_core_or_skip()
    proc = subprocess.run([sys.executable, str(SCRIPT), "--offline-only", str(TEMPLATE)], cwd=ROOT,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "lint: 0 error(s), 0 warning(s)" in proc.stdout
    assert "[offline] PASS" in proc.stdout and "PASS (offline)" in proc.stdout
    for label in ("bridged-default", "no-bridge", "check-room", "bad-room-name", "dash-room-name"):
        assert f"scenario {label}" in proc.stdout


def test_offline_scenarios_compile_to_the_expected_rooms():
    _switch_core_or_skip()
    scenarios = vt.offline_scenarios()
    report = vt.run_switch_engine(TEMPLATE, scenarios)
    by_label = {s["label"]: s for s in report["scenarios"]}
    bridged = by_label["bridged-default"]["spec"]
    assert bridged["name"] == "strangler-migration"
    assert bridged["agents"] == ["translator", "fuzzer", "cutover-gate"]
    assert bridged["users"] == [vt.OFFLINE_CREATOR] and bridged["bridge"] == vt.BRIDGE_NAME
    assert bridged["docs"] == [] and not bridged["aliases"]
    assert by_label["no-bridge"]["spec"]["users"] == [] and by_label["no-bridge"]["spec"]["bridge"] is None
    assert by_label["check-room"]["spec"]["name"] == vt.CHECK_ROOM_NAME
    assert all(not s.get("new_agents") for s in report["scenarios"] if not s["error"])
    assert "does not match the pattern" in by_label["bad-room-name"]["error"]
    kickoff = by_label["bridged-default"]["kickoff"]
    assert kickoff and "@" not in kickoff and "{$" not in kickoff


# --------------------------------------------------------------------- the validator catches drift (pure)

def _mut_agent_resource(doc):
    doc["resources"]["translator"] = {"type": "agent", "name": "translator", "instructions": "x"}
    _room(doc)["agents"] = ["translator", "fuzzer", "cutover-gate"]


def _mut_kickoff_mentions(doc):
    _room(doc)["kickoff"] = "Welcome. @translator migrate /people/george08/lists from legacy_openlibrary to FastAPI"


def _mut_kickoff_room_wide(doc):
    _room(doc)["kickoff"] = "Hello @all"


def _mut_no_kickoff(doc):
    _room(doc).pop("kickoff", None)


def _mut_users_unconditional(doc):
    _room(doc)["users"] = ["{$creator}"]


def _mut_agent_owned_doc(doc):
    _room(doc)["docs"] = [{"name": "Parity Audit Matrix", "description": "d", "instructions": "i", "content": "c"}]


def _mut_bridge_required(doc):
    doc["params"]["bridge"]["required"] = True


def _mut_bridge_default(doc):
    doc["params"]["bridge"]["default"] = "Mattermost"


def _mut_no_room_param(doc):
    doc["params"].pop("room_name")


def _mut_fixed_name(doc):
    _room(doc)["name"] = "strangler-migration"


def _mut_agents_order(doc):
    _room(doc)["agents"] = ["fuzzer", "translator", "cutover-gate"]


def _mut_alias(doc):
    _room(doc)["aliases"] = {"cutover-gate": "gate"}


def _mut_conditional_room(doc):
    _room(doc)["if"] = "{bridge}"


def _mut_version(doc):
    doc["version"] = 1


def _mut_no_instructions(doc):
    _room(doc)["instructions"] = "  "


def _mut_second_room(doc):
    doc["resources"]["spare"] = copy.deepcopy(_room(doc))


@pytest.mark.parametrize("mutate", [
    _mut_agent_resource, _mut_kickoff_mentions, _mut_kickoff_room_wide, _mut_no_kickoff, _mut_users_unconditional,
    _mut_agent_owned_doc, _mut_bridge_required, _mut_bridge_default, _mut_no_room_param, _mut_fixed_name,
    _mut_agents_order, _mut_alias, _mut_conditional_room, _mut_version, _mut_no_instructions, _mut_second_room,
], ids=lambda f: f.__name__[5:])
def test_check_document_rejects_drift(mutate):
    doc = _doc()
    mutate(doc)
    assert vt.check_document(doc), f"{mutate.__name__} went unnoticed"


def test_check_document_rejects_non_mapping():
    assert vt.check_document(["not", "a", "mapping"])


def test_check_scenario_rejects_bad_compilations():
    good = {"label": "x", "inputs": {}, "error": None, "new_agents": [], "existing": {}, "room_addressing": {},
            "spec": {"name": "n", "bridge": None, "users": [], "agents": list(vt.AGENT_NAMES), "docs": [],
                     "description": "d", "instructions": "i"}, "kickoff": "hi"}
    expect = {"name": "n", "bridge": None, "users": []}
    assert vt.check_scenario(good, expect) == []
    for patch in ({"error": "ValueError: boom"}, {"new_agents": ["translator"]}, {"kickoff": "hi @fuzzer"},
                  {"kickoff": ""}, {"spec": {**good["spec"], "users": ["user"]}},
                  {"spec": {**good["spec"], "instructions": "room {room_name}"}},
                  {"spec": {**good["spec"], "docs": [{"name": "Cutover Scorecard"}]}}):
        assert vt.check_scenario({**good, **patch}, expect), patch
    # A scenario that must be refused but ran is a problem; one that was refused is not.
    assert vt.check_scenario(good, {"error": True})
    assert vt.check_scenario({**good, "error": "ValueError: no"}, {"error": True}) == []


# ------------------------------------------------------------- the validator catches drift (Switch engine)

def test_offline_stage_fails_on_a_lint_warning(tmp_path):
    _switch_core_or_skip()
    doc = _doc()
    _room(doc)["description"] = "Migration of {legacy_system}"  # undeclared placeholder -> lint warning
    problems = vt.offline_stage(_write(tmp_path, doc))
    assert any("undeclared_placeholder" in p for p in problems), problems


def test_offline_stage_fails_on_an_agent_resource(tmp_path):
    _switch_core_or_skip()
    doc = _doc()
    _mut_agent_resource(doc)
    problems = vt.offline_stage(_write(tmp_path, doc))
    assert any("agent_runtime_unsaid" in p for p in problems), problems
    assert any("only a room resource is allowed" in p for p in problems), problems


def test_cli_exit_codes(tmp_path):
    _switch_core_or_skip()
    doc = _doc()
    _mut_kickoff_mentions(doc)
    bad = subprocess.run([sys.executable, str(SCRIPT), "--offline-only", str(_write(tmp_path, doc))], cwd=ROOT,
                         capture_output=True, text=True, timeout=180)
    assert bad.returncode == vt.EXIT_FINDINGS and "kickoff must address nobody" in bad.stdout
    missing = subprocess.run([sys.executable, str(SCRIPT), "--offline-only", str(tmp_path / "nope.yaml")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert missing.returncode == vt.EXIT_FINDINGS and "template not found" in missing.stderr
    both = subprocess.run([sys.executable, str(SCRIPT), "--offline-only", "--run"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert both.returncode == 2 and "not allowed with" in both.stderr


def test_offline_stage_fails_when_the_engine_drops_a_scenario(tmp_path, monkeypatch):
    real = vt.offline_scenarios()

    def fake_engine(template, scenarios):
        return {"lint": {"ok": True, "blocked": False, "errors": [], "warnings": []}, "is_v2": True,
                "document": _doc(), "scenarios": [{"label": real[0]["label"], "inputs": {}, "error": None}]}

    monkeypatch.setattr(vt, "run_switch_engine", fake_engine)
    problems = vt.offline_stage(TEMPLATE)
    assert any("the engine reported scenarios" in p for p in problems), problems


def test_a_crashing_stage_fails_the_run(monkeypatch, capsys):
    def boom(template, scenarios):
        raise RuntimeError("Switch template engine crashed (exit 1)")

    monkeypatch.setattr(vt, "run_switch_engine", boom)
    assert vt.main(["--offline-only", str(TEMPLATE)]) == vt.EXIT_FINDINGS
    assert "stage crashed: RuntimeError" in capsys.readouterr().out


def test_check_document_rejects_mentions_in_description_and_instructions():
    for field in ("description", "instructions"):
        doc = _doc()
        _room(doc)[field] += " Ask @fuzzer for a campaign."
        assert any(f"room {field} must address nobody" in p for p in vt.check_document(doc)), field


def test_check_scenario_rejects_unfilled_kickoff_placeholders():
    sc = {"label": "x", "inputs": {}, "error": None, "new_agents": [], "existing": {}, "room_addressing": {},
          "spec": {"name": "n", "bridge": None, "users": [], "agents": list(vt.AGENT_NAMES), "docs": [],
                   "description": "d", "instructions": "i"}, "kickoff": "Created {$dat} for {owner}"}
    problems = vt.check_scenario(sc, {"name": "n", "bridge": None, "users": []})
    assert any("kickoff still holds an unfilled placeholder" in p for p in problems), problems


# ---------------------------------------------------- run stage: the check room is ALWAYS archived (fakes)

class _FakeSettings:
    mattermost_user = "user"
    switch_url = "http://switch.invalid"


class _FakeGateway:
    def __init__(self, *, post=None, raise_on_post=None):
        self._post, self._raise = post, raise_on_post
        self.closed = False

    def request(self, method, path, **kw):
        assert (method, path) == ("POST", "/gateway/rooms/from-template-document")
        assert kw["json"]["inputs"] == {vt.ROOM_PARAM: vt.CHECK_ROOM_NAME}
        assert kw["json"]["template_name"] == vt.TEMPLATE_NAME
        if self._raise is not None:
            raise self._raise
        return self._post

    def room(self, room_id):
        return {"external_channel_id": "chan"}

    def room_documents(self, room_id):
        return []

    def close(self):
        self.closed = True


class _FakeTranslator:
    """Rooms named like the check room; ``archive_room`` records calls."""

    def __init__(self, rooms=(), *, detail=None, archive_reply=None):
        self.rooms = [dict(r) for r in rooms]
        self.detail = detail
        self.archive_reply = archive_reply
        self.archived: list[str] = []
        self.closed = False

    def list_rooms(self, include_archived=False):
        return [r for r in self.rooms if include_archived or not r.get("archived")]

    def room_detail(self, room_id):
        if isinstance(self.detail, BaseException):
            raise self.detail
        return self.detail

    def archive_room(self, room_id):
        self.archived.append(room_id)
        for r in self.rooms:
            if r["room_id"] == room_id:
                r["archived"] = True
        return self.archive_reply if self.archive_reply is not None else {"room_id": room_id, "archived": True}

    def close(self):
        self.closed = True


def _patch_run(monkeypatch, gateway, translator):
    import agents.switch_client as sc
    monkeypatch.setattr(vt, "_load_settings", lambda: _FakeSettings())
    monkeypatch.setattr(vt, "_gateway", lambda settings: gateway)
    monkeypatch.setattr(sc.AgentCredentials, "load", classmethod(lambda cls, name: cls(name, "id", "key")))
    monkeypatch.setattr(sc, "SwitchAgent", lambda settings, creds: translator)


_GOOD_DETAIL = {"name": vt.CHECK_ROOM_NAME, "agent_names": list(vt.AGENT_NAMES), "connected_user_names": ["user"]}
_CREATED = {"created_agents": [], "room": {"room_id": "R1", "room_name": vt.CHECK_ROOM_NAME,
                                           "failed_attachments": [], "created_document_ids": []}}


def test_run_stage_happy_path_archives(monkeypatch):
    gw, tr = _FakeGateway(post=_CREATED), _FakeTranslator(detail=_GOOD_DETAIL)
    _patch_run(monkeypatch, gw, tr)
    assert vt.run_stage("doc") == []
    assert tr.archived == ["R1"] and tr.closed and gw.closed


def test_run_stage_archives_when_an_assertion_fails(monkeypatch):
    gw = _FakeGateway(post=_CREATED)
    tr = _FakeTranslator(detail={**_GOOD_DETAIL, "agent_names": ["translator"]})
    _patch_run(monkeypatch, gw, tr)
    problems = vt.run_stage("doc")
    assert any("room agents are" in p for p in problems) and tr.archived == ["R1"]


def test_run_stage_archives_when_checking_crashes(monkeypatch):
    gw, tr = _FakeGateway(post=_CREATED), _FakeTranslator(detail=RuntimeError("detail exploded"))
    _patch_run(monkeypatch, gw, tr)
    problems = vt.run_stage("doc")
    assert any("detail exploded" in p for p in problems) and tr.archived == ["R1"]


def test_run_stage_archives_on_keyboard_interrupt(monkeypatch):
    gw, tr = _FakeGateway(post=_CREATED), _FakeTranslator(detail=KeyboardInterrupt())
    _patch_run(monkeypatch, gw, tr)
    with pytest.raises(KeyboardInterrupt):
        vt.run_stage("doc")
    assert tr.archived == ["R1"] and tr.closed


def test_run_stage_sweeps_by_name_when_the_response_is_lost(monkeypatch):
    # The server created the room, then the request failed (or the reply was malformed): sweep by name.
    from agents.switch_client import SwitchError
    for gw in (_FakeGateway(raise_on_post=SwitchError("POST", "/x", 502, "bad gateway")),
               _FakeGateway(post={"room": "not-a-mapping"}), _FakeGateway(post={"room": {}})):
        tr = _FakeTranslator()
        _patch_run(monkeypatch, gw, tr)
        original = tr.list_rooms
        # The room appears only after the POST (the pre-run leftover sweep sees nothing).
        calls = {"n": 0}

        def list_rooms(include_archived=False, _tr=tr):
            calls["n"] += 1
            if calls["n"] > 1 and not _tr.rooms:
                _tr.rooms.append({"room_id": "LOST", "name": vt.CHECK_ROOM_NAME, "archived": False})
            return original(include_archived)

        tr.list_rooms = list_rooms
        problems = vt.run_stage("doc")
        assert problems, gw._post
        assert tr.archived == ["LOST"], (gw._post, tr.archived)


def test_run_stage_archives_leftovers_first(monkeypatch):
    gw = _FakeGateway(post=_CREATED)
    tr = _FakeTranslator([{"room_id": "OLD", "name": vt.CHECK_ROOM_NAME, "archived": False},
                          {"room_id": "LIVE", "name": "strangler-migration", "archived": False}], detail=_GOOD_DETAIL)
    _patch_run(monkeypatch, gw, tr)
    assert vt.run_stage("doc") == []
    assert tr.archived == ["OLD", "R1"]  # never the live strangler-migration room


def test_run_stage_reports_an_archive_that_did_not_take(monkeypatch):
    gw, tr = _FakeGateway(post=_CREATED), _FakeTranslator(detail=_GOOD_DETAIL, archive_reply={"archived": False})
    _patch_run(monkeypatch, gw, tr)
    assert any("did not report the room archived" in p for p in vt.run_stage("doc"))


def test_gateway_5xx_at_login_is_an_environment_error(monkeypatch):
    import agents.switch_client as sc

    def refuse(settings):
        raise sc.SwitchError("POST", "/gateway/auth/login", 503, "unavailable")

    monkeypatch.setattr(sc, "SwitchGateway", refuse)
    with pytest.raises(vt.EnvironmentMissing):
        vt._gateway(_FakeSettings())

    def unauthorized(settings):
        raise sc.SwitchError("POST", "/gateway/auth/login", 401, "bad password")

    monkeypatch.setattr(sc, "SwitchGateway", unauthorized)
    with pytest.raises(sc.SwitchError):
        vt._gateway(_FakeSettings())


def test_missing_switch_core_is_an_environment_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_CORE_DIR", str(tmp_path))
    with pytest.raises(vt.EnvironmentMissing):
        vt.find_switch_core()


# -------------------------------------------------------------------------------------------- integration

def _live_or_skip():
    try:
        settings = vt._load_settings()
    except vt.EnvironmentMissing as exc:
        pytest.skip(f"no Switch settings: {exc}")
    try:
        httpx.get(f"{settings.switch_url}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Switch is down at {settings.switch_url}: {exc}")
    from agents.switch_client import AgentCredentials
    if AgentCredentials.load("translator") is None:
        pytest.skip("agents not provisioned (.state/agents/translator.json missing): run python -m agents.provision")
    return settings


@pytest.mark.integration
def test_live_validate_run_and_archive():
    _switch_core_or_skip()
    settings = _live_or_skip()
    proc = subprocess.run([sys.executable, str(SCRIPT), "--run", str(TEMPLATE)], cwd=ROOT, capture_output=True,
                          text=True, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "[live] POST /gateway/templates/validate: ok=True blocked=False errors=0 warnings=0" in out
    assert "[run] PASS" in out and "archived room" in out
    # Independently: no active check room is left behind.
    from agents.switch_client import AgentCredentials, SwitchAgent
    translator = SwitchAgent(settings, AgentCredentials.load("translator"))
    try:
        assert vt._active_check_rooms(translator) == []
    finally:
        translator.close()
