#!/usr/bin/env python3
"""Validate the StranglerRoom Switch v2 room template (docs/AGENTS_SPEC.md §5).

Usage::

    .venv/bin/python scripts/validate_template.py [TEMPLATE] [--offline-only | --run]

``TEMPLATE`` defaults to ``strangler-room.template.yaml`` at the project root. Stages:

1. **offline** (always) — runs Switch's own template engine, in Switch's own venv
   (``<switch core>/.venv/bin/python -I -B``, so nothing from this project shadows it), on the document:
   ``template_lint.lint_template`` (must report 0 errors AND 0 warnings), then for several input scenarios
   ``template_v2.compile_v2`` -> ``agent_templates.plan_template`` -> ``RoomYamlService.parse_template``
   — the exact chain ``POST /gateway/rooms/from-template-document`` runs. The compiled room is then held to
   the StranglerRoom contract: exactly one ``room`` resource and no agent/group resources (so no managed
   agent is planned), the room named by the ``room_name`` param, ``agents`` = the three registered custom
   agents, ``users`` = ``[{$creator}]`` only with a bridge, a ``bridge`` param (``type: bridge``,
   ``default: [$first]``, ``required: false``), pipeline instructions, a kickoff that addresses nobody, and
   none of the three agent-owned documents.
2. **live** (unless ``--offline-only``) — ``SwitchGateway(Settings.load()).validate_template(text)``
   (``POST /gateway/templates/validate``) must be ``ok``, not ``blocked``, with no errors and no warnings.
3. **run** (``--run``) — as the Gateway admin, ``POST /gateway/rooms/from-template-document`` with
   ``template_name`` ``strangler-migration`` and ``room_name`` = ``strangler-migration-template-check``;
   asserts the created room holds translator, fuzzer, cutover-gate and the Mattermost user, then archives it
   with the translator's ``archive_room`` op (the translator is a member of every room this template
   creates; ``archive_room`` needs no event-stream connection). The room is archived even when an assertion
   or the request itself fails; an active leftover check room from an earlier crashed run is archived (and
   announced) before the run.

Every finding is printed. Exit status: 0 = every stage passed, 1 = a stage found a problem,
2 = the environment is missing (no Switch checkout, no ``.state/switch.env``, Switch unreachable).

The Switch checkout is ``$SWITCH_CORE_DIR`` when set, else the first ``.switch-infra/core`` (holding
``.venv/bin/python``) found in this script's directory or one of its ancestors — so a copy of this project
nested inside the real tree (an HACP peer workspace) uses the real Switch checkout. Which one is used is
always printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_TEMPLATE = ROOT / "strangler-room.template.yaml"
AGENT_NAMES = ["translator", "fuzzer", "cutover-gate"]
# The single-writer documents each agent creates itself (agents/ledger.py); the template must not pre-create
# them, or the agents' creator-only writes would fail. Spelled out here (and cross-checked against
# agents.ledger by tests/test_template.py) so the offline stage imports nothing from the framework.
AGENT_OWNED_DOCS = ["Parity & Cutover Audit Ledger", "Parity Audit Matrix", "Cutover Scorecard"]
TEMPLATE_NAME = "strangler-migration"
ROOM_PARAM = "room_name"
DEFAULT_ROOM_NAME = "strangler-migration"
CHECK_ROOM_NAME = "strangler-migration-template-check"
BRIDGE_PARAM = "bridge"
BRIDGE_NAME = "Mattermost"          # display name of the stack's only bridge (scripts/bootstrap_switch.sh)
OFFLINE_CREATOR = "user"            # what {$creator} resolves to offline (the admin's linked Mattermost user)
EXPECTED_USERS_CONDITION = [{"if": "{bridge}", "value": ["{$creator}"]}, {"value": []}]
OFFLINE_TIMEOUT_S = 120
EXIT_OK, EXIT_FINDINGS, EXIT_ENV = 0, 1, 2


class EnvironmentMissing(RuntimeError):
    """The stage cannot run here (no Switch checkout / credentials / server) — exit 2, never a pass."""


# --------------------------------------------------------------------------------------------- offline

# Runs inside Switch's venv. argv: template path, scenarios JSON. Prints one JSON report on stdout.
_CHECKER = r'''
import json, sys, traceback
import yaml
from switch_core.template_lint import lint_template
from switch_core.template_v2 import compile_v2, is_v2_text
from switch_core.agent_templates import plan_template, template_kind
from switch_core.rooms_yaml import RoomSpec, RoomYamlService

text = open(sys.argv[1], encoding="utf-8").read()
scenarios = json.loads(sys.argv[2])
lint = lint_template(text)
finding = lambda f: {"code": f.code, "subject": f.subject, "message": f.message, "blocking": f.blocking}
report = {
    "lint": {"ok": lint.ok, "blocked": lint.blocked,
             "errors": [finding(f) for f in lint.errors], "warnings": [finding(f) for f in lint.warnings]},
    "document": None, "is_v2": is_v2_text(text), "kind": None, "scenarios": [],
}
try:
    report["document"] = yaml.safe_load(text)
    report["kind"] = str(template_kind(text))
except Exception as exc:
    report["document_error"] = f"{type(exc).__name__}: {exc}"
svc = RoomYamlService.__new__(RoomYamlService)  # parse_template is pure: no DB session needed
for sc in scenarios:
    out = {"label": sc["label"], "inputs": sc["inputs"], "error": None}
    try:
        b = sc["builtins"]
        compiled = compile_v2(text, sc["inputs"], b)
        plan = plan_template(text, {}, sc["inputs"], builtins=b, machines=None)
        out["compiled_names"] = compiled.names
        out["new_agents"] = [p.name for p in plan.new_agents]
        out["existing"] = plan.existing
        out["room_addressing"] = plan.room_addressing
        out["v1_document"] = plan.document
        if plan.document is None:
            raise ValueError("the plan has no room document")
        parsed = svc.parse_template(plan.document, inputs=plan.inputs or None, builtins=b)
        if not isinstance(parsed.spec, RoomSpec):
            raise ValueError(f"expected a single room, got {type(parsed.spec).__name__}")
        out["spec"] = parsed.spec.model_dump(mode="json")
        out["kickoff"] = parsed.kickoff
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc(limit=4)
    report["scenarios"].append(out)
print(json.dumps(report, default=str))
'''


def find_switch_core() -> Path:
    """The Switch core checkout whose venv runs the offline stage (see module docstring)."""
    env = os.environ.get("SWITCH_CORE_DIR")
    if env:
        core = Path(env).resolve()
        if not (core / ".venv" / "bin" / "python").is_file():
            raise EnvironmentMissing(f"SWITCH_CORE_DIR={env} has no .venv/bin/python")
        return core
    for base in Path(__file__).resolve().parents:
        core = base / ".switch-infra" / "core"
        if (core / ".venv" / "bin" / "python").is_file():
            return core
    raise EnvironmentMissing("no Switch checkout: set SWITCH_CORE_DIR or run scripts/bootstrap_switch.sh "
                             "(expects .switch-infra/core/.venv/bin/python above this script)")


def offline_scenarios() -> list[dict[str, Any]]:
    """Inputs the template must compile under, each with what the compiled room must look like."""
    builtins = {"$creator": OFFLINE_CREATOR, "$creator_email": "user@example.com",
                "$date": "2026-11-05", "$timestamp": "1793836800"}
    return [
        # What the server runs once resolve_defaults turned `bridge: [$first]` into the default bridge.
        {"label": "bridged-default", "inputs": {BRIDGE_PARAM: BRIDGE_NAME}, "builtins": builtins,
         "expect": {"name": DEFAULT_ROOM_NAME, "bridge": BRIDGE_NAME, "users": [OFFLINE_CREATOR]}},
        # A server with no messaging app at all: the chain resolves to nothing and no bridge input is sent (the
        # Console drops empty values), so the room is internal and nobody is invited.
        {"label": "no-bridge", "inputs": {}, "builtins": builtins,
         "expect": {"name": DEFAULT_ROOM_NAME, "bridge": None, "users": []}},
        # The --run stage's inputs.
        {"label": "check-room", "inputs": {ROOM_PARAM: CHECK_ROOM_NAME, BRIDGE_PARAM: BRIDGE_NAME},
         "builtins": builtins, "expect": {"name": CHECK_ROOM_NAME, "bridge": BRIDGE_NAME, "users": [OFFLINE_CREATOR]}},
        # The room-name pattern must refuse a name that is not a channel slug.
        {"label": "bad-room-name", "inputs": {ROOM_PARAM: "Not A Channel!"}, "builtins": builtins,
         "expect": {"error": True}},
        # ... and one a messaging app would normalise into another (taken) name.
        {"label": "dash-room-name", "inputs": {ROOM_PARAM: "strangler-migration-"}, "builtins": builtins,
         "expect": {"error": True}},
    ]


def run_switch_engine(template: Path, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Run Switch's template engine on ``template`` in Switch's venv; return its JSON report."""
    core = find_switch_core()
    python = core / ".venv" / "bin" / "python"
    print(f"[offline] Switch engine: {python} -I -B (core {core})")
    stripped = [{k: v for k, v in s.items() if k != "expect"} for s in scenarios]
    proc = subprocess.run([str(python), "-I", "-B", "-c", _CHECKER, str(template), json.dumps(stripped)],
                          cwd=str(core), capture_output=True, text=True, timeout=OFFLINE_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(f"Switch template engine crashed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unreadable report from the Switch engine: {exc}\nstdout: {proc.stdout!r}\n"
                           f"stderr: {proc.stderr!r}") from exc


def _has_mention(text: str | None) -> bool:
    return bool(text) and "@" in text


_PLACEHOLDER = re.compile(r"\{\$?[A-Za-z_][A-Za-z0-9_]*(?:\.name)?\}")  # rooms_yaml.PLACEHOLDER_RE + {key.name}


def _unfilled(text: str | None) -> list[str]:
    """Placeholders the engine left in compiled text (an undeclared or unknown one is kept verbatim)."""
    return _PLACEHOLDER.findall(text or "")


def check_document(doc: Any) -> list[str]:
    """Contract checks on the template as written (before compilation)."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [f"the template is not a mapping: {type(doc).__name__}"]
    if doc.get("version") != 2:
        problems.append(f"version must be 2, got {doc.get('version')!r}")
    params = doc.get("params") if isinstance(doc.get("params"), dict) else {}
    bridge = params.get(BRIDGE_PARAM)
    if not isinstance(bridge, dict):
        problems.append(f"params.{BRIDGE_PARAM} is missing")
    else:
        for key, want in (("type", "bridge"), ("default", ["$first"]), ("required", False)):
            if bridge.get(key) != want:
                problems.append(f"params.{BRIDGE_PARAM}.{key} must be {want!r}, got {bridge.get(key)!r}")
    room_param = params.get(ROOM_PARAM)
    if not isinstance(room_param, dict):
        problems.append(f"params.{ROOM_PARAM} is missing")
    else:
        if room_param.get("type", "string") != "string":
            problems.append(f"params.{ROOM_PARAM}.type must be string, got {room_param.get('type')!r}")
        if room_param.get("default") != DEFAULT_ROOM_NAME:
            problems.append(f"params.{ROOM_PARAM}.default must be {DEFAULT_ROOM_NAME!r}, "
                            f"got {room_param.get('default')!r}")
    resources = doc.get("resources") if isinstance(doc.get("resources"), dict) else {}
    if len(resources) != 1:
        problems.append(f"exactly one resource expected, got {sorted(resources)}")
    rooms = {k: v for k, v in resources.items() if isinstance(v, dict) and v.get("type") == "room"}
    others = {k: (v.get("type") if isinstance(v, dict) else v) for k, v in resources.items() if k not in rooms}
    if others:
        problems.append(f"only a room resource is allowed (agent resources need Switch agent management and a "
                        f"controller); found {others}")
    if len(rooms) != 1:
        problems.append(f"exactly one room resource expected, got {sorted(rooms)}")
        return problems
    room = next(iter(rooms.values()))
    if "if" in room:
        problems.append("the room resource must be unconditional (no 'if:')")
    if room.get("name") != f"{{{ROOM_PARAM}}}":
        problems.append(f"room name must be '{{{ROOM_PARAM}}}', got {room.get('name')!r}")
    if room.get("agents") != AGENT_NAMES:
        problems.append(f"room agents must be exactly {AGENT_NAMES} (plain names of the registered agents), "
                        f"got {room.get('agents')!r}")
    if room.get("users") != EXPECTED_USERS_CONDITION:
        problems.append(f"room users must be {EXPECTED_USERS_CONDITION!r}, got {room.get('users')!r}")
    if room.get("bridge") != f"{{{BRIDGE_PARAM}}}":
        problems.append(f"room bridge must be '{{{BRIDGE_PARAM}}}', got {room.get('bridge')!r}")
    for field in ("description", "instructions"):
        if _has_mention(room.get(field) if isinstance(room.get(field), str) else None):
            problems.append(f"the room {field} must address nobody (no '@')")
    instructions = room.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        problems.append("room instructions describing the pipeline are required")
    else:
        missing = [a for a in AGENT_NAMES if a not in instructions]
        if missing:
            problems.append(f"room instructions never name {missing}")
        if "human" not in instructions.lower():
            problems.append("room instructions must describe the human-only gates")
    kickoff = room.get("kickoff")
    if not isinstance(kickoff, str) or not kickoff.strip():
        problems.append("a kickoff message is required")
    elif _has_mention(kickoff):
        problems.append(f"the kickoff must address nobody (no '@'): {kickoff!r}")
    if room.get("aliases"):
        problems.append(f"no aliases: the agents are addressed by their registered names, got {room['aliases']!r}")
    for d in room.get("docs") or []:
        name = d.get("name") if isinstance(d, dict) else d
        if name in AGENT_OWNED_DOCS:
            problems.append(f"document {name!r} is agent-owned (single writer) and must not be in the template")
    return problems


def check_scenario(sc: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Contract checks on one compiled + parsed scenario."""
    where = f"scenario {sc['label']}"
    if expect.get("error"):
        return [] if sc.get("error") else [f"{where}: expected the inputs {sc['inputs']} to be refused, they ran"]
    if sc.get("error"):
        return [f"{where}: {sc['error']}\n{sc.get('traceback', '')}"]
    problems: list[str] = []
    if sc.get("new_agents"):
        problems.append(f"{where}: the plan would create managed agents {sc['new_agents']}")
    if sc.get("existing") or sc.get("room_addressing"):
        problems.append(f"{where}: unexpected agent slots {sc.get('existing')} / {sc.get('room_addressing')}")
    spec = sc.get("spec") or {}
    for key in ("name", "bridge", "users"):
        if spec.get(key) != expect[key]:
            problems.append(f"{where}: room {key} must be {expect[key]!r}, got {spec.get(key)!r}")
    if spec.get("agents") != AGENT_NAMES:
        problems.append(f"{where}: room agents must be {AGENT_NAMES}, got {spec.get('agents')!r}")
    bad = [d.get("name") for d in spec.get("docs") or [] if d.get("name") in AGENT_OWNED_DOCS]
    if bad:
        problems.append(f"{where}: agent-owned documents in the room: {bad}")
    for field in ("description", "instructions"):
        text = spec.get(field) or ""
        if not text.strip():
            problems.append(f"{where}: room {field} is empty")
        if _unfilled(text):
            problems.append(f"{where}: room {field} still holds an unfilled placeholder {_unfilled(text)}")
        if _has_mention(text):
            problems.append(f"{where}: the compiled room {field} addresses someone")
    kickoff = sc.get("kickoff")
    if not kickoff or not kickoff.strip():
        problems.append(f"{where}: no kickoff")
    else:
        if _has_mention(kickoff):
            problems.append(f"{where}: the compiled kickoff addresses someone: {kickoff!r}")
        if _unfilled(kickoff):
            problems.append(f"{where}: the compiled kickoff still holds an unfilled placeholder {_unfilled(kickoff)}")
    return problems


def offline_stage(template: Path) -> list[str]:
    scenarios = offline_scenarios()
    report = run_switch_engine(template, scenarios)
    lint = report["lint"]
    for f in lint["errors"]:
        print(f"[offline] lint ERROR   {f['code']} ({f['subject']}): {f['message']}")
    for f in lint["warnings"]:
        print(f"[offline] lint WARNING {f['code']} ({f['subject']}): {f['message']}")
    problems = [f"lint {kind[:-1]} {f['code']}: {f['message']}"
                for kind in ("errors", "warnings") for f in lint[kind]]
    print(f"[offline] lint: {len(lint['errors'])} error(s), {len(lint['warnings'])} warning(s)")
    if not report.get("is_v2"):
        problems.append("the document is not a version 2 template")
    if report.get("document_error"):
        problems.append(f"document: {report['document_error']}")
    else:
        problems += check_document(report["document"])
    labels = [sc.get("label") for sc in report.get("scenarios") or []]
    if labels != [spec["label"] for spec in scenarios]:
        problems.append(f"the engine reported scenarios {labels}, expected {[s['label'] for s in scenarios]}")
        return problems
    for sc, spec in zip(report["scenarios"], scenarios):
        found = check_scenario(sc, spec["expect"])
        status = "ok" if not found else "FAIL"
        detail = sc.get("error") if spec["expect"].get("error") else (sc.get("spec") or {}).get("name")
        print(f"[offline] scenario {sc['label']:<16} {status}  ({detail})")
        problems += found
    return problems


# ------------------------------------------------------------------------------------------------ live

def _load_settings():
    from agents.switch_client import Settings
    try:
        return Settings.load()
    except RuntimeError as exc:
        raise EnvironmentMissing(str(exc)) from exc


def _gateway(settings):
    import httpx

    from agents.switch_client import SwitchError, SwitchGateway
    try:
        return SwitchGateway(settings)
    except httpx.TransportError as exc:
        raise EnvironmentMissing(f"Switch unreachable at {settings.switch_url}: {exc}") from exc
    except SwitchError as exc:
        if exc.status in (502, 503, 504):  # a proxy in front of a Switch that is down
            raise EnvironmentMissing(f"Switch unavailable at {settings.switch_url}: HTTP {exc.status}") from exc
        raise  # e.g. 401: wrong admin credentials — a real failure, not a missing environment


def live_validate_stage(text: str) -> list[str]:
    settings = _load_settings()
    gw = _gateway(settings)
    try:
        result = gw.validate_template(text)
    finally:
        gw.close()
    for f in result.get("errors") or []:
        print(f"[live] ERROR   {f.get('code')} ({f.get('subject')}): {f.get('message')}")
    for f in result.get("warnings") or []:
        print(f"[live] WARNING {f.get('code')} ({f.get('subject')}): {f.get('message')}")
    print(f"[live] POST /gateway/templates/validate: ok={result.get('ok')} blocked={result.get('blocked')} "
          f"errors={len(result.get('errors') or [])} warnings={len(result.get('warnings') or [])}")
    problems = []
    if result.get("ok") is not True or result.get("blocked") is not False:
        problems.append(f"live validation not ok: {result}")
    problems += [f"live error {f.get('code')}: {f.get('message')}" for f in result.get("errors") or []]
    problems += [f"live warning {f.get('code')}: {f.get('message')}" for f in result.get("warnings") or []]
    return problems


def _active_check_rooms(translator) -> list[str]:
    return [r["room_id"] for r in translator.list_rooms()
            if r.get("name") == CHECK_ROOM_NAME and not r.get("archived")]


def _archive(translator, room_id: str, why: str) -> list[str]:
    """Archive ``room_id`` as the translator; returns problems (never raises)."""
    try:
        detail = translator.archive_room(room_id)
    except Exception as exc:  # noqa: BLE001 — reported, the caller keeps cleaning up
        return [f"could not archive room {room_id} ({why}): {type(exc).__name__}: {exc}"]
    if not isinstance(detail, dict) or detail.get("archived") is not True:
        return [f"archive_room({room_id}) did not report the room archived: {detail}"]
    print(f"[run] archived room {room_id} ({why})")
    return []


def run_stage(text: str) -> list[str]:
    """Create a room from the template as the Gateway admin, check it, archive it (always)."""
    from agents.switch_client import AgentCredentials, SwitchAgent, SwitchError

    settings = _load_settings()
    creds = AgentCredentials.load("translator")
    if creds is None:
        raise EnvironmentMissing("no .state/agents/translator.json — run `python -m agents.provision` first")
    gw = _gateway(settings)
    translator = SwitchAgent(settings, creds)
    problems: list[str] = []
    room_id: str | None = None
    try:
        for leftover in _active_check_rooms(translator):
            print(f"[run] WARNING: an active {CHECK_ROOM_NAME!r} room {leftover} is left from an earlier run")
            problems += _archive(translator, leftover, "leftover from an earlier run")
        if problems:
            return problems
        body = {"template": text, "template_name": TEMPLATE_NAME, "inputs": {ROOM_PARAM: CHECK_ROOM_NAME},
                "filled": {}}
        try:
            created = gw.request("POST", "/gateway/rooms/from-template-document", json=body)
        except SwitchError as exc:
            return [f"POST /gateway/rooms/from-template-document failed: HTTP {exc.status} {exc.body[:2000]}"]
        room = (created or {}).get("room") or {}
        room_id = room.get("room_id")
        print(f"[run] created room {room.get('room_name')!r} id={room_id} "
              f"failed_attachments={room.get('failed_attachments')}")
        if not room_id:
            return [f"from-template-document returned no room: {created}"]
        if created.get("created_agents"):
            problems.append(f"the run created managed agents: {created['created_agents']}")
        if room.get("failed_attachments"):
            problems.append(f"some attachments failed: {room['failed_attachments']}")
        if room.get("created_document_ids"):
            problems.append(f"the template created documents: {room['created_document_ids']}")
        detail = translator.room_detail(room_id)
        if detail.get("name") != CHECK_ROOM_NAME:
            problems.append(f"room name is {detail.get('name')!r}, expected {CHECK_ROOM_NAME!r}")
        agents = sorted(detail.get("agent_names") or [])
        if agents != sorted(AGENT_NAMES):
            problems.append(f"room agents are {agents}, expected {sorted(AGENT_NAMES)}")
        users = detail.get("connected_user_names") or []
        if settings.mattermost_user not in users:
            problems.append(f"Mattermost user {settings.mattermost_user!r} not in the room (users: {users})")
        gdetail = gw.room(room_id)
        if not gdetail.get("external_channel_id"):
            problems.append(f"the room is not bridged (no external_channel_id): {gdetail}")
        docs = [d.get("name") for d in gw.room_documents(room_id)]
        bad = [d for d in docs if d in AGENT_OWNED_DOCS]
        if bad:
            problems.append(f"agent-owned documents exist in the fresh room: {bad}")
        print(f"[run] room agents={agents} users={users} channel={gdetail.get('external_channel_id')} docs={docs}")
    except Exception as exc:  # noqa: BLE001 — reported as a finding after cleanup
        problems.append(f"run stage error: {type(exc).__name__}: {exc}")
    finally:
        try:
            targets = [room_id] if room_id else _active_check_rooms(translator)
            for rid in targets:
                problems += _archive(translator, rid, "template check done")
            if not room_id and not targets:
                print("[run] no check room was created; nothing to archive")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"cleanup failed: {type(exc).__name__}: {exc}")
        finally:
            translator.close()
            gw.close()
    return problems


# ------------------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template", nargs="?", default=str(DEFAULT_TEMPLATE), help="template YAML (default: %(default)s)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--offline-only", action="store_true", help="only the offline Switch-engine checks")
    mode.add_argument("--run", action="store_true",
                      help="also create a room from the template on the live Switch, check it and archive it")
    args = ap.parse_args(argv)
    template = Path(args.template).resolve()
    if not template.is_file():
        print(f"template not found: {template}", file=sys.stderr)
        return EXIT_FINDINGS
    text = template.read_text(encoding="utf-8")
    stages = [("offline", lambda: offline_stage(template))]
    if not args.offline_only:
        stages.append(("live", lambda: live_validate_stage(text)))
    if args.run:
        stages.append(("run", lambda: run_stage(text)))
    failed = False
    for name, stage in stages:
        try:
            problems = stage()
        except EnvironmentMissing as exc:
            print(f"[{name}] ENVIRONMENT: {exc}", file=sys.stderr)
            return EXIT_ENV
        except Exception as exc:  # noqa: BLE001 — a crashed stage is a failed stage, printed in full
            import traceback
            traceback.print_exc()
            problems = [f"stage crashed: {type(exc).__name__}: {exc}"]
        for p in problems:
            print(f"[{name}] PROBLEM: {p}")
        print(f"[{name}] {'FAIL' if problems else 'PASS'}")
        if problems:
            failed = True
            break  # later stages assume the earlier ones passed
    print(f"{template.name}: {'FAIL' if failed else 'PASS'} ({', '.join(n for n, _ in stages)})")
    return EXIT_FINDINGS if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
