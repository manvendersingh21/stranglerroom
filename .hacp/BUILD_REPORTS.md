# Build phase digest (lead-curated) — read before integrating

Full suite after all builds + lead fixes: `.venv/bin/pytest -q -m 'not integration'` → **680 passed, 2 deselected**.
Live integration tests (`-m integration`): switch client (throwaway agents) + template run both pass.

## What exists
| Area | Files | Notes |
|---|---|---|
| translator | agents/translator_agent.py, legacy_introspect.py (agy), llm_synth.py (codex) | refuses migrate/decide during an active cutover; ledger history[]; restores previous module on failed deploy; dedupes re-delivered commands; `record-cutover` cross-checks the approval message id against `read_context` — if not found it records `approval_message_verified: false` (UNVERIFIED whether Switch event ids == read_context ids — check live!) |
| fuzzer | agents/fuzzer_agent.py (+ opencode edge tests) | trusts only translator-created ledger with human-attributed decisions matching rev/migration; registry staged per migration; defuses @ in posts |
| cutover-gate | agents/cutover_agent.py, agents/proxy_control.py (opencode) | probes the 5 bench paths for parity before benchmarking; auto-reverts the proxy if any post-flip step fails; repeat approve re-sends record-cutover |
| e2e driver | scripts/run_e2e_demo.py, agents/mattermost.py (codex) | provisions only with `--fresh`; anchors each step on the human's post; any agent `⚠️` fails the run (except "LLM synthesis rejected"); waits for 3 s quiet before step 1 |
| ops | scripts/agents.sh (opencode, hardened: flock, argv check), Makefile, requirements.txt | `make e2e` = driver `--fresh`; `make cinema` = `--cinema --fresh` |
| template | strangler-room.template.yaml (agy draft), scripts/validate_template.py | offline engine + live gateway + live `--run` (creates + archives `strangler-migration-template-check`) all PASS, 0 warnings |
| CI | .github/workflows/e2e.yml (codex draft) | unit + docker e2e + template validation steps |
| tests | test_parity_integration.py, test_switch_integration.py | core loop proof (D-1..D-5 → 5/5) without Switch; live client test |

## Lead fixes since (framework)
- `commands.py`: command verb must be the FIRST word (after @mentions and filler like "please"); `defuse_mentions()`.
- `runtime.py`: failure posts defuse quoted text; `switch_client.register_agent` rotates keys only on 401;
  `_abort_stream` (socket shutdown) wakes a blocked SSE reader immediately.
- `divergence.classify` rule 1 needs FastAPI's exact routing 404; deprecated clusters with converged corpus probes →
  `deprecated-violated`. Proxy: no shared cookie jar, flip requires approver+evidence, raw body passthrough.

## Watch in the live run (unverified so far)
1. Does the Mattermost bridge rewrite `@mention` text or split long agent posts (campaign tables)?
2. Is `approval_message_verified` true in the sealed ledger (event `message_id` vs `read_context` ids)?
3. Ledger status never shows `awaiting-decision`/`parity-approved` (translator only acts at migrate/decide/record).
4. Timing on the 4-core box: campaign ~120+ requests, benchmark 2×500 requests at concurrency 50.
