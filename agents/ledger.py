"""Room-document state for StranglerRoom: three single-writer Switch room documents.

Switch 0.31.0 lets only the creating agent update a room document, so each agent owns exactly one
document and is the only writer of it — which makes every section tamper-evident by construction:

* ``Parity & Cutover Audit Ledger`` — owned by **@translator**, the migration's system of record:
  the request, route inventory, builds, every divergence with the human decision (who, when, which
  message), the parity verdict and benchmark mirrored from the other two documents (with sha256
  evidence digests of their state), the cutover sign-off and a hash-chained audit trail that is
  sealed when the cutover is recorded.
* ``Parity Audit Matrix`` — owned by **@fuzzer**: every shadow campaign and its divergence clusters.
* ``Cutover Scorecard`` — owned by **@cutover-gate**: p50/p95/p99/throughput of legacy vs modern and
  the cutover/rollback events.

Every document is human-readable Markdown followed by a fenced ``json stranglerroom-state`` block
holding its full machine-readable state, so any agent (or the e2e driver, via the Gateway) can
rehydrate it with :func:`extract_state`.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

LEDGER_DOC = "Parity & Cutover Audit Ledger"
MATRIX_DOC = "Parity Audit Matrix"
SCORECARD_DOC = "Cutover Scorecard"

DOC_META: dict[str, tuple[str, str]] = {
    LEDGER_DOC: ("System of record for the legacy→FastAPI migration: divergences, human decisions, parity, "
                 "benchmark, cutover sign-off and a hash-chained audit trail. Written only by @translator.",
                 "Read before approving anything. The json stranglerroom-state block is the machine-readable "
                 "source; verify the audit trail with agents.ledger.verify_chain."),
    MATRIX_DOC: ("Differential shadow-testing results (legacy :9001 vs modern :9002), clustered into "
                 "divergence ids D-n. Written only by @fuzzer.",
                 "Each run lists every divergence cluster with its status; decisions come from the ledger."),
    SCORECARD_DOC: ("Load benchmark (500 requests, concurrency 50) of legacy vs modern and the cutover record. "
                    "Written only by @cutover-gate.",
                    "The cutover gate flips the :9000 proxy only after the human posts approve-cutover."),
}

STATE_FENCE = "stranglerroom-state"
_STATE_RE = re.compile(r"```json " + STATE_FENCE + r"\n(.*?)\n```", re.DOTALL)
GENESIS_HASH = "0" * 64

STATUS_BANNERS = {
    "scaffolded": "🟡 v1 scaffolded — shadow testing by @fuzzer",
    "awaiting-decision": "🟠 Divergences found — awaiting the human lead's decisions",
    "patching": "🔧 Human decisions applied — v2 under re-verification by @fuzzer",
    "parity-approved": "🟢 100% approved parity — benchmarking by @cutover-gate",
    "cut-over": "🚦 Cut over — :9000 now serves the modern FastAPI service",
    "sealed": "🧾 Sealed — migration complete, audit trail closed",
}


# ----------------------------------------------------------------------------------- utilities

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj: Any) -> str:
    """sha256 of the canonical JSON of ``obj`` — used as evidence for mirrored state."""
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def embed_state(markdown: str, state: dict) -> str:
    return (markdown.rstrip() + "\n\n---\n_Machine-readable state (do not edit by hand):_\n\n"
            + f"```json {STATE_FENCE}\n" + json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n```\n")


def extract_state(content: str) -> dict | None:
    m = _STATE_RE.search(content or "")
    return json.loads(m.group(1)) if m else None


def _cell(value: Any) -> str:
    text = "—" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _status(code: Any) -> str:
    return "—" if code is None else f"`{code}`"


# --------------------------------------------------------------------------------- audit trail

def append_entry(trail: list[dict], *, actor: str, actor_kind: str, event: str, summary: str,
                 evidence: dict | None = None, source_message_id: str | None = None, at: str | None = None) -> dict:
    """Append a hash-chained entry: ``hash = sha256(canonical(entry without "hash"))`` where the entry
    includes ``prev`` = previous entry's hash (genesis = 64 zeros)."""
    entry = {"seq": len(trail) + 1, "at": at or now_iso(), "actor": actor, "actor_kind": actor_kind,
             "event": event, "summary": summary, "evidence": evidence or {},
             "source_message_id": source_message_id, "prev": trail[-1]["hash"] if trail else GENESIS_HASH}
    entry["hash"] = hashlib.sha256(canonical(entry).encode()).hexdigest()
    trail.append(entry)
    return entry


def verify_chain(trail: list[dict]) -> tuple[bool, str]:
    """Recompute every hash and link. Returns ``(ok, reason)``."""
    prev = GENESIS_HASH
    for i, entry in enumerate(trail, start=1):
        if entry.get("seq") != i:
            return False, f"entry {i}: seq is {entry.get('seq')}"
        if entry.get("prev") != prev:
            return False, f"entry {i}: prev link broken"
        body = {k: v for k, v in entry.items() if k != "hash"}
        if hashlib.sha256(canonical(body).encode()).hexdigest() != entry.get("hash"):
            return False, f"entry {i}: hash mismatch (tampered)"
        prev = entry["hash"]
    return True, f"{len(trail)} entries, head {prev[:12]}"


# ------------------------------------------------------------------------------- ledger (translator)

def new_ledger(*, migration_id: str, path: str, source: str, target: str, requested_by: str,
               request_message_id: str | None) -> dict:
    return {
        "version": 1, "kind": "ledger", "status": "scaffolded",
        "migration": {"id": migration_id, "path": path, "source": source, "target": target,
                      "requested_by": requested_by, "request_message_id": request_message_id,
                      "requested_at": now_iso()},
        "routes": [], "builds": [], "divergences": [], "decisions": {}, "refusals": [],
        "parity": None, "benchmark": None, "cutover": None, "trail": [], "sealed": None,
    }


def render_ledger(state: dict) -> str:
    mig = state["migration"]
    lines = [
        f"# {LEDGER_DOC}",
        "",
        f"**Migration {mig['id']}** · `{mig['path']}` · `{mig['source']}` (web.py) → `{mig['target']}` · "
        f"requested by @{mig['requested_by']} at {mig['requested_at']}",
        "",
        f"**Status:** {STATUS_BANNERS.get(state['status'], state['status'])}",
        "",
        "Real-world case: Open Library's web.py → FastAPI migration, "
        "[internetarchive/openlibrary#12487](https://github.com/internetarchive/openlibrary/issues/12487).",
        "",
        "## 1. Route inventory (AST-extracted from legacy `lists.py` by @translator)",
        "",
        "| Legacy handler | Methods | Legacy path (web.py) | FastAPI route(s) in current build |",
        "|---|---|---|---|",
    ]
    for r in state["routes"]:
        lines.append(f"| `{r['handler']}` | {', '.join(r['methods'])} | `{_cell(r['legacy_path'])}` | "
                     f"{', '.join(f'`{p}`' for p in r.get('fastapi_paths', [])) or '—'} |")
    lines += ["", "## 2. Builds of the modern service (the generated `lists.py` it serves)", "",
              "| Rev | Mode | Policy (per legacy quirk) | Built at | Note |", "|---|---|---|---|---|"]
    for b in state["builds"]:
        policy = ", ".join(f"{t}={d}" for t, d in sorted(b.get("policy", {}).items())) or "naive v1 (textbook port)"
        lines.append(f"| `{b['rev']}` | {b['mode']} | {_cell(policy)} | {b['at']} | {_cell(b.get('note'))} |")
    lines += ["", "## 3. Divergences and human decisions", ""]
    if state["divergences"]:
        lines += ["| ID | Divergence | Legacy | Modern v1 | Hits | Decision | Decided by | When |",
                  "|---|---|---|---|---|---|---|---|"]
        for d in state["divergences"]:
            dec = state["decisions"].get(d["id"], {})
            decision = {"preserve": "✅ preserve (legacy contract)", "deprecate": "🗑️ deprecate (legacy bug)"}.get(
                dec.get("decision"), "⏳ pending")
            by = f"@{dec['by']}" if dec.get("by") else "—"
            lines.append(f"| **{d['id']}** | {_cell(d['title'])} | {_status(d.get('legacy_status'))} | "
                         f"{_status(d.get('modern_status'))} | {d.get('hits', '—')} | {decision} | {by} | "
                         f"{_cell(dec.get('at'))} |")
    else:
        lines.append("_None recorded yet — @fuzzer's first shadow campaign is pending (see the Parity Audit Matrix)._")
    if state["refusals"]:
        lines += ["", "**Governance refusals** (commands only a human may issue):", ""]
        lines += [f"- {r['at']} · @{r['by']} tried `{_cell(r['command'])}` — refused: {r['reason']}"
                  for r in state["refusals"]]
    lines += ["", "## 4. Parity verification (mirrored from @fuzzer's Parity Audit Matrix)", ""]
    p = state.get("parity")
    if p:
        lines += [f"**{p['label']}** on rev `{p['rev']}` · {p['total_requests']} shadow requests · run {p['run']} at "
                  f"{p['at']} · evidence `sha256:{p['evidence'][:16]}…`"]
    else:
        lines.append("_Not yet verified._")
    lines += ["", "## 5. Performance (mirrored from @cutover-gate's Cutover Scorecard)", ""]
    bm = state.get("benchmark")
    if bm:
        lg, md, cmp_ = bm["legacy"], bm["modern"], bm["compare"]
        lines += [f"{bm['total']} requests at concurrency {bm['concurrency']} per target, rev `{bm['rev']}`, {bm['at']}:",
                  "", "| Metric | Legacy :9001 (web.py) | Modern :9002 (FastAPI) | Δ |", "|---|---|---|---|",
                  f"| p50 latency | {lg['p50_ms']:.1f} ms | {md['p50_ms']:.1f} ms | {cmp_['p50_delta_pct']:+.1f}% |",
                  f"| p95 latency | {lg['p95_ms']:.1f} ms | {md['p95_ms']:.1f} ms | {cmp_['p95_delta_pct']:+.1f}% |",
                  f"| **p99 latency** | **{lg['p99_ms']:.1f} ms** | **{md['p99_ms']:.1f} ms** | **{cmp_['p99_delta_pct']:+.1f}%** |",
                  f"| Throughput | {lg['throughput_rps']:.1f} req/s | {md['throughput_rps']:.1f} req/s | "
                  f"{cmp_['throughput_delta_pct']:+.1f}% |",
                  f"| Errors (5xx/transport) | {lg['errors']} | {md['errors']} | |",
                  "", f"Evidence `sha256:{bm['evidence'][:16]}…`"]
    else:
        lines.append("_Not yet benchmarked._")
    lines += ["", "## 6. Cutover sign-off", ""]
    c = state.get("cutover")
    if c:
        lines += [f"- **Approved by:** @{c['approved_by']} (human) at {c['approved_at']} — message `{c['approval_message_id']}`",
                  f"- **Proxy :9000 now routes to:** `{c['proxy_target']}` ({c.get('upstream_url', '')}); "
                  f"verified through the proxy: {'yes ✅' if c.get('verified') else 'NO ❌'}",
                  f"- **Evidence:** {_cell(c.get('evidence'))}"]
    else:
        lines.append("_No cutover yet. Only the human lead can approve it (`@cutover-gate approve-cutover`)._")
    lines += ["", "## 7. Audit trail (hash-chained, sha256)", "",
              "| # | When (UTC) | Actor | Event | Summary | Hash |", "|---|---|---|---|---|---|"]
    for e in state["trail"]:
        lines.append(f"| {e['seq']} | {e['at']} | @{e['actor']} ({e['actor_kind']}) | `{e['event']}` | "
                     f"{_cell(e['summary'])} | `{e['hash'][:12]}` |")
    ok, why = verify_chain(state["trail"])
    lines += ["", f"Chain check: {'✅ intact' if ok else '❌ BROKEN'} — {why}."]
    if state.get("sealed"):
        s = state["sealed"]
        lines += ["", f"**🧾 Sealed** by @translator at {s['at']} · head `sha256:{s['head_hash']}` · {s['entries']} entries."]
    return embed_state("\n".join(lines), state)


# --------------------------------------------------------------------------------- matrix (fuzzer)

MAX_RUNS_KEPT = 12


def new_matrix() -> dict:
    return {"version": 1, "kind": "matrix", "runs": []}


def add_run(state: dict, *, rev: str, trigger: dict, decisions: dict, report: dict) -> dict:
    run = {"run": (state["runs"][-1]["run"] + 1) if state["runs"] else 1, "rev": rev, "at": now_iso(),
           "trigger": trigger, "decisions": decisions, "report": report}
    state["runs"].append(run)
    del state["runs"][:-MAX_RUNS_KEPT]
    return run


def latest_run(state: dict | None) -> dict | None:
    return state["runs"][-1] if state and state.get("runs") else None


def render_matrix(state: dict) -> str:
    lines = [f"# {MATRIX_DOC}", "",
             "Legacy `:9001` (web.py) vs modern `:9002` (FastAPI) — differential shadow testing by @fuzzer: "
             "a deterministic quirk corpus plus Hypothesis-generated requests fired concurrently at both targets; "
             "every difference in status, content type or JSON body is clustered into a divergence id.", ""]
    run = latest_run(state)
    if run is None:
        return embed_state("\n".join(lines + ["_No campaign has run yet._"]), state)
    rep = run["report"]
    lines += [f"## Latest run #{run['run']} — rev `{run['rev']}` — {run['at']}", "",
              f"**{rep['parity_label']}** · {rep['total_requests']} requests · {rep['identical']} identical · "
              f"{rep['divergent']} divergent · {rep['duration_s']:.2f}s · triggered by @{run['trigger'].get('by')}", ""]
    if rep["clusters"]:
        lines += ["| ID | Divergence | Legacy | Modern | Hits | Sample request | Decision | Status |",
                  "|---|---|---|---|---|---|---|---|"]
        for c in rep["clusters"]:
            sample = c["sample_request"] if len(c["sample_request"]) <= 64 else c["sample_request"][:61] + "..."
            lines.append(f"| **{c['id']}** | {_cell(c['title'])} | {_status(c['legacy_status'])} | "
                         f"{_status(c['modern_status'])} | {c['count']} | `{_cell(sample).replace('`', '')}` | "
                         f"{_cell(c.get('decision'))} | {_cell(c['status'])} |")
        lines += ["", "Sample diffs:", ""]
        for c in rep["clusters"]:
            if c.get("sample_diff"):
                lines.append(f"- **{c['id']}** `{_cell(c['sample_request'])[:80]}` → " + "; ".join(
                    _cell(d) for d in c["sample_diff"][:3]))
    else:
        lines.append("No divergences: every probe returned identical status, content type and body.")
    lines += ["", "## Run history", "", "| Run | Rev | At | Result | Requests | Divergent |", "|---|---|---|---|---|---|"]
    for r in reversed(state["runs"]):
        lines.append(f"| {r['run']} | `{r['rev']}` | {r['at']} | {_cell(r['report']['parity_label'])} | "
                     f"{r['report']['total_requests']} | {r['report']['divergent']} |")
    return embed_state("\n".join(lines), state)


# ---------------------------------------------------------------------------- scorecard (cutover)

def new_scorecard() -> dict:
    return {"version": 1, "kind": "scorecard", "benchmarks": [], "cutover": None, "rollbacks": []}


def latest_benchmark(state: dict | None) -> dict | None:
    return state["benchmarks"][-1] if state and state.get("benchmarks") else None


def render_scorecard(state: dict) -> str:
    lines = [f"# {SCORECARD_DOC}", "", "Load test of both targets before the strangler proxy is flipped. "
             "The proxy moves only after the human lead posts `approve-cutover`.", ""]
    bm = latest_benchmark(state)
    if bm is None:
        lines.append("_No benchmark yet._")
    else:
        lg, md, cmp_ = bm["legacy"], bm["modern"], bm["compare"]
        lines += [f"## Benchmark — rev `{bm['rev']}` — {bm['at']}", "",
                  f"{bm['total']} requests per target at concurrency {bm['concurrency']}, round-robin over "
                  f"{len(bm['paths'])} contract endpoints.", "",
                  "| Metric | Legacy :9001 | Modern :9002 | Δ |", "|---|---|---|---|",
                  f"| p50 | {lg['p50_ms']:.1f} ms | {md['p50_ms']:.1f} ms | {cmp_['p50_delta_pct']:+.1f}% |",
                  f"| p95 | {lg['p95_ms']:.1f} ms | {md['p95_ms']:.1f} ms | {cmp_['p95_delta_pct']:+.1f}% |",
                  f"| **p99** | **{lg['p99_ms']:.1f} ms** | **{md['p99_ms']:.1f} ms** | **{cmp_['p99_delta_pct']:+.1f}%** |",
                  f"| max | {lg['max_ms']:.1f} ms | {md['max_ms']:.1f} ms | |",
                  f"| Throughput | {lg['throughput_rps']:.1f} req/s | {md['throughput_rps']:.1f} req/s | "
                  f"{cmp_['throughput_delta_pct']:+.1f}% |",
                  f"| Errors | {lg['errors']} | {md['errors']} | |", "",
                  f"Verdict: {bm['verdict']}"]
    lines += ["", "## Cutover", ""]
    c = state.get("cutover")
    if c:
        lines += [f"- Approved by **@{c['approved_by']}** at {c['approved_at']} (message `{c['approval_message_id']}`)",
                  f"- Proxy `:9000` → `{c['proxy_target']}` ({c.get('upstream_url', '')}) at {c['flipped_at']}",
                  f"- Verified through the proxy: {'yes ✅' if c.get('verified') else 'NO ❌'} — {_cell(c.get('verification'))}"]
    else:
        lines.append("_Awaiting human approval._")
    if state.get("rollbacks"):
        lines += ["", "## Rollbacks", ""] + [f"- {r['at']} by @{r['by']}: {r['note']}" for r in state["rollbacks"]]
    return embed_state("\n".join(lines), state)
