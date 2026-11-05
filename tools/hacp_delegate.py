#!/usr/bin/env python3
"""HACP/2.0 delegation harness for heterogeneous peer CLIs: codex, opencode, agy.

Dev-process tooling only — the StranglerRoom product itself talks Switch, not HACP.
Follows the bilateral lifecycle of github.com/manvendersingh21/hacp (spec/HACP-2.0-draft.md):

  session.open -> contract.propose -> agree(requester, worker) -> freeze(revision = sha256 of the
  canonical terms) -> work -> submission (artifact manifest: path, size, sha256) -> verification
  (measured checks: every acceptance command's exit code) -> settlement | rework (bounded) | escalation

Swarm safety
------------
Every session runs in an ISOLATED WORKSPACE (``.hacp/work/<session>/``): a snapshot of the repo
(``.venv`` symlinked; Switch checkout, native stack and runtime state excluded). The peer can only
touch its copy. Acceptance checks run inside the copy. Only the contract's declared outputs are merged
back into the real tree, and only if the real file did not change since the snapshot (optimistic
concurrency — a concurrent writer causes an escalation, never a silent overwrite). After merging, the
acceptance checks run again in the real tree.

Peers
-----
* ``agy``      — Antigravity CLI headless, ``--mode accept-edits`` plus a session-only shell allow-list
                 (no permission bypass): ``.venv/bin/pytest``, ``.venv/bin/python`` and read-only tools.
* ``opencode`` — ``opencode run -m <model>`` in the workspace.
* ``codex``    — TEXT MODE. Codex's bubblewrap sandbox cannot start in this unprivileged pod and its
                 sandbox bypass is not permitted, so codex runs ``-s read-only`` with no tool use: the
                 harness inlines the contract's ``context`` files into the prompt, codex answers with
                 ``=== FILE: path ===`` blocks, the harness materialises them in the workspace, then
                 verifies exactly as for the other peers.

Usage::

  tools/hacp_delegate.py contract.json        # result: .hacp/results/<task_id>.json, exit 0 = settled

contract.json::

  {"task_id": "...", "peer": "codex|opencode|agy", "model": "...", "effort": "high",
   "summary": "...", "prompt": "...", "outputs": ["rel/path.py", ...], "context": ["rel/file", ...],
   "acceptance": ["shell command that must exit 0", ...], "max_rework": 2, "timeout_s": 1800}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HACP_DIR = ROOT / ".hacp"
PROTOCOL = "HACP/2.0"
REQUESTER = "urn:hacp:agent:claude-orchestrator"
AGY_READONLY = ("ls", "cat", "grep", "head", "tail", "wc", "mkdir", "pwd", "find", "diff")
EXCLUDE_TOP = {".venv", ".switch-infra", ".native", ".state", ".hacp", ".git", ".hypothesis", ".claude",
               ".pytest_cache", "node_modules"}
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached|quota exceeded|exceeded your current quota|"
                      r"hit your usage limit|usage limit reached", re.I)
AGY_FALLBACK_MODEL = "gemini-3.8-flash-high"
FILE_BLOCK_RE = re.compile(r"^=== FILE: (?P<path>[^\n=]+?) ===\n(?P<body>.*?)\n=== END FILE ===$", re.S | re.M)


def canonical(value) -> str:
    """HACP canonical form (§5.1): keys sorted by UTF-8 bytes, no whitespace, integers only."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise ValueError("canonical form carries integers only")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-8"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(v) for k, v in items) + "}"
    raise ValueError(f"unserializable {value!r}")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class Ledger:
    def __init__(self, session_id: str):
        (HACP_DIR / "sessions").mkdir(parents=True, exist_ok=True)
        self.path = HACP_DIR / "sessions" / f"{session_id}.jsonl"
        self.session_id = session_id

    def emit(self, kind: str, sender: str, body: dict) -> dict:
        env = {"protocol": PROTOCOL, "message_id": "m-" + uuid.uuid4().hex[:16], "session_id": self.session_id,
               "kind": kind, "from": sender, "sent_at": now(), "body": body}
        with self.path.open("a") as f:
            f.write(json.dumps(env) + "\n")
        print(f"[hacp {self.session_id}] {kind:<18} {sender.split(':')[-1][:28]:<28} {json.dumps(body)[:150]}",
              flush=True)
        return env


# ------------------------------------------------------------------------------------ workspace

def make_workspace(session_id: str) -> Path:
    ws = HACP_DIR / "work" / session_id
    ws.mkdir(parents=True)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = {n for n in names if n in ("__pycache__",) or n.endswith(".pyc")}
        if Path(directory).resolve() == ROOT:
            skip |= {n for n in names if n in EXCLUDE_TOP}
        return skip

    for entry in ROOT.iterdir():
        if entry.name in EXCLUDE_TOP or entry.name == "__pycache__":
            continue
        dest = ws / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, ignore=ignore, symlinks=True)
        else:
            shutil.copy2(entry, dest)
    (ws / ".venv").symlink_to(ROOT / ".venv")
    (ws / ".state").mkdir()
    # Its own (empty) git repository: peers that locate their project root via git (opencode) must stop here,
    # not at the real checkout above .hacp/work/, or they would write into the real tree.
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    return ws


def baseline(outputs: list[str]) -> dict[str, str | None]:
    return {rel: sha256_file(ROOT / rel) for rel in outputs}


def merge_back(ws: Path, outputs: list[str], base: dict[str, str | None]) -> tuple[list[str], list[str]]:
    merged, conflicts = [], []
    for rel in outputs:
        src, dst = ws / rel, ROOT / rel
        if not src.is_file():
            continue
        if sha256_file(dst) != base.get(rel):
            conflicts.append(rel)
            continue
        if sha256_file(dst) == sha256_file(src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + f".hacp-{uuid.uuid4().hex[:6]}")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        merged.append(rel)
    return merged, conflicts


# ---------------------------------------------------------------------------------------- peers

def pack_context(ws: Path, files: list[str]) -> str:
    parts = []
    for rel in files:
        p = ws / rel
        if p.is_file():
            parts.append(f"=== CONTEXT FILE: {rel} ===\n{p.read_text(errors='replace')}\n=== END CONTEXT FILE ===")
        else:
            parts.append(f"=== CONTEXT FILE: {rel} === (does not exist yet)")
    return "\n\n".join(parts)


def build_prompt(c: dict, ws: Path, revision: str, rework: str | None) -> str:
    acceptance = "\n- ".join(c["acceptance"])
    head = (f"HACP contract {c['task_id']} (revision {revision}). Workspace root: {ws}.\n"
            f"Deliverables (must exist, nothing else may be changed): {', '.join(c['outputs'])}.\n"
            f"Acceptance checks the requester runs from the workspace root (all must exit 0):\n- {acceptance}\n\n")
    task = c["prompt"]
    if rework:
        task = f"REWORK — your previous submission failed verification:\n{rework}\n\nOriginal task:\n{task}"
    if c["peer"] == "codex":
        ctx = pack_context(ws, sorted(set(c.get("context", [])) | set(c["outputs"])))
        return (head + task + "\n\nYou cannot run commands or read files: everything you need is inlined below. "
                "Answer ONLY with the complete final content of every deliverable, each wrapped exactly as\n"
                "=== FILE: <relative path> ===\n<entire file content>\n=== END FILE ===\n"
                "No other prose, no markdown fences around the blocks.\n\n" + ctx)
    if c.get("context"):
        task += "\n\nRead these files first: " + ", ".join(c["context"])
    if c["peer"] == "agy":
        task += ("\n\nShell policy: you may ONLY run: .venv/bin/pytest, .venv/bin/python, " + ", ".join(AGY_READONLY)
                 + ". Create and edit files with your file-editing tools, not the shell. Run the acceptance checks "
                   "yourself before finishing.")
    else:
        task += "\n\nRun the acceptance checks yourself before finishing."
    return head + task


def run_peer(c: dict, ws: Path, prompt: str, log: Path) -> int:
    peer, model, effort = c["peer"], c["model"], c.get("effort", "high")
    timeout = int(c.get("timeout_s", 1800))
    with log.open("w") as fh:
        if peer == "codex":
            answer = log.with_suffix(".answer.md")
            cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-C", str(ws),
                   "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-o", str(answer), "-"]
            proc = subprocess.run(cmd, cwd=ws, input=prompt, text=True, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=timeout)
            written = materialise(ws, answer.read_text() if answer.is_file() else "", c["outputs"])
            fh.write(f"\n[harness] materialised from codex answer: {written}\n")
            return proc.returncode
        if peer == "agy":
            allow = ["command(.venv/bin/pytest)", "command(.venv/bin/python)",
                     f"command({ws}/.venv/bin/pytest)", f"command({ws}/.venv/bin/python)"]
            allow += [f"command({x})" for x in AGY_READONLY]
            # agy's own file tools need explicit rules in headless mode (they cannot prompt): read-only, workspace only
            allow += [f"read_file({ws})", f"read_file({ws}/**)", f"list_dir({ws})", f"list_dir({ws}/**)"]
            cmd = ["agy", "-p", prompt, "--mode", "accept-edits", "--settings",
                   json.dumps({"permissions": {"allow": allow}}), "--model", model]
            if model.startswith("gemini"):  # agy rejects --effort for models with a fixed thinking budget
                cmd += ["--effort", effort]
        elif peer == "opencode":
            cmd = ["opencode", "run", "-m", model, prompt]
        else:
            raise SystemExit(f"unknown peer {peer}")
        return subprocess.run(cmd, cwd=ws, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              timeout=timeout).returncode


def materialise(ws: Path, answer: str, outputs: list[str]) -> list[str]:
    allowed = set(outputs)
    written = []
    for m in FILE_BLOCK_RE.finditer(answer):
        rel = m.group("path").strip()
        rel = rel[2:] if rel.startswith("./") else rel
        if rel not in allowed:
            continue
        dest = ws / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(m.group("body") + "\n")
        written.append(rel)
    return written


def manifest(base_dir: Path, outputs: list[str]) -> list[dict]:
    out = []
    for rel in outputs:
        p = base_dir / rel
        if p.is_file():
            data = p.read_bytes()
            out.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        else:
            out.append({"path": rel, "missing": True})
    return out


def run_checks(cmds: list[str], cwd: Path) -> list[dict]:
    results = []
    for cmd in cmds:
        t0 = time.time()
        try:
            p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=900)
            code, tail = p.returncode, (p.stdout + p.stderr)[-3500:]
        except subprocess.TimeoutExpired as exc:
            code, tail = 124, f"timeout after 900s: {exc}"
        results.append({"check": cmd, "exit_code": code, "passed": code == 0,
                        "duration_ms": int((time.time() - t0) * 1000), "tail": tail})
    return results


# ----------------------------------------------------------------------------------------- main

def delegate(c: dict, *, isolate: bool = True) -> dict:
    worker = f"urn:hacp:agent:{c['peer']}.{c['model'].replace('/', '.').lower()}"[:80]
    sid = f"s-{c['task_id']}-{uuid.uuid4().hex[:6]}"
    led = Ledger(sid)
    led.emit("session.open", REQUESTER, {"requester": REQUESTER, "worker": worker, "isolated": isolate})
    led.emit("session.accept", worker, {})
    terms = {"task_id": c["task_id"], "summary": c["summary"], "outputs": c["outputs"],
             "context": c.get("context", []), "acceptance": c["acceptance"],
             "limits": {"max_rework": int(c.get("max_rework", 2))}}
    cid = "c-" + c["task_id"]
    led.emit("contract.propose", REQUESTER, {"contract_id": cid, "relationship": "delegation", "terms": terms})
    led.emit("contract.agree", REQUESTER, {"contract_id": cid})
    led.emit("contract.agree", worker, {"contract_id": cid})
    revision = hashlib.sha256(canonical(terms).encode()).hexdigest()
    led.emit("contract.freeze", REQUESTER, {"contract_id": cid, "revision": revision})

    base = baseline(c["outputs"])
    ws = make_workspace(sid) if isolate else ROOT
    result = {"task_id": c["task_id"], "session": sid, "ledger": str(led.path.relative_to(ROOT)),
              "workspace": str(ws), "revision": revision, "verdict": "escalated", "merged": [], "conflicts": []}
    rework = None
    for attempt in range(int(c.get("max_rework", 2)) + 1):
        t0 = time.time()
        log = HACP_DIR / "sessions" / f"{sid}.attempt{attempt}.log"
        try:
            code = run_peer(c, ws, build_prompt(c, ws, revision, rework), log)
        except subprocess.TimeoutExpired:
            code = 124
        elapsed = time.time() - t0
        led.emit("work.complete", worker, {"attempt": attempt, "exit_code": code,
                                            "duration_s": int(elapsed), "log": str(log.relative_to(ROOT))})
        tail_all = log.read_text(errors="replace")[-1500:] if log.is_file() else ""
        produced = all((ws / rel).is_file() for rel in c["outputs"])
        if not produced and QUOTA_RE.search(tail_all):  # provider quota, not a model/test failure
            if c["peer"] == "agy" and c["model"] != AGY_FALLBACK_MODEL:
                led.emit("model.fallback", REQUESTER, {"from": c["model"], "to": AGY_FALLBACK_MODEL,
                                                        "reason": "peer quota exhausted"})
                c = {**c, "model": AGY_FALLBACK_MODEL}
            else:
                led.emit("escalation", REQUESTER, {"reason": "peer quota exhausted", "detail": tail_all[-600:]})
                result["peer_error"] = "quota exhausted: " + tail_all[-400:]
                break
        if code not in (0, None) and elapsed < 15:
            tail = log.read_text(errors="replace")[-1500:] if log.is_file() else ""
            if "error" in tail.lower() and not any((ws / rel).is_file() for rel in c["outputs"]):
                # The peer CLI refused to start (bad flag/model/auth): reworking cannot help.
                led.emit("escalation", REQUESTER, {"reason": "peer CLI failed to start", "detail": tail[-600:]})
                result["peer_error"] = tail[-600:]
                break
        arts = manifest(ws, c["outputs"])
        led.emit("submission", worker, {"revision": revision, "artifacts": arts})
        checks = run_checks(c["acceptance"], ws)
        missing = [a["path"] for a in arts if a.get("missing")]
        ok = not missing and all(ch["passed"] for ch in checks)
        led.emit("verification", REQUESTER, {"revision": revision, "verdict": "accept" if ok else "reject",
                                              "where": "workspace", "subject": [a for a in arts if not a.get("missing")],
                                              "checks": [{k: v for k, v in ch.items() if k != "tail"} for ch in checks],
                                              "missing": missing})
        if ok:
            if isolate:
                merged, conflicts = merge_back(ws, c["outputs"], base)
                result.update(merged=merged, conflicts=conflicts)
                led.emit("merge", REQUESTER, {"merged": merged, "conflicts": conflicts})
                if conflicts:
                    led.emit("escalation", REQUESTER, {"reason": "concurrent change in the real tree",
                                                        "conflicts": conflicts})
                    break
                root_checks = run_checks(c["acceptance"], ROOT)
                root_ok = all(ch["passed"] for ch in root_checks)
                led.emit("verification", REQUESTER, {"revision": revision, "where": "root",
                                                      "verdict": "accept" if root_ok else "reject",
                                                      "checks": [{k: v for k, v in ch.items() if k != "tail"}
                                                                 for ch in root_checks]})
                result["root_checks"] = [{"check": ch["check"], "passed": ch["passed"], "tail": ch["tail"][-800:]}
                                         for ch in root_checks]
                if not root_ok:
                    led.emit("escalation", REQUESTER, {"reason": "merged artifacts fail in the real tree"})
                    result["verdict"] = "merged-but-root-failing"
                    break
            led.emit("settlement", REQUESTER, {"revision": revision, "outcome": "settled",
                                                "artifacts": manifest(ROOT, c["outputs"])})
            result["verdict"] = "settled"
            break
        rework = (f"Missing outputs: {missing}\n" + "\n\n".join(
            f"$ {ch['check']}\n(exit {ch['exit_code']})\n{ch['tail']}" for ch in checks if not ch["passed"]))
        led.emit("rework.request", REQUESTER, {"attempt": attempt + 1, "missing": missing})
    else:
        led.emit("escalation", REQUESTER, {"reason": "rework limit exhausted", "revision": revision})
    led.emit("session.close", REQUESTER, {"reason": result["verdict"]})
    (HACP_DIR / "results").mkdir(parents=True, exist_ok=True)
    (HACP_DIR / "results" / f"{c['task_id']}.json").write_text(json.dumps(result, indent=2))
    print(f"{result['verdict'].upper()} {c['task_id']} ledger={result['ledger']} merged={result['merged']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Delegate one HACP contract to a peer CLI.")
    ap.add_argument("contract")
    ap.add_argument("--no-isolate", action="store_true", help="let the peer work in the real tree (not recommended)")
    a = ap.parse_args()
    result = delegate(json.loads(Path(a.contract).read_text()), isolate=not a.no_isolate)
    return 0 if result["verdict"] == "settled" else 2


if __name__ == "__main__":
    sys.exit(main())
