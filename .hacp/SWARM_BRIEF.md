# StranglerRoom swarm brief (binding for every node and sub-node)

Project root `/root/another_hackathon` — StranglerRoom: Open Library's legacy web.py lists endpoints are migrated to
FastAPI by three Switch agents (`translator`, `fuzzer`, `cutover-gate`) and a human lead in the Switch room
`strangler-migration` (bridged to Mattermost). Hackathon entry; the lead (the Claude session that spawned you) owns the
framework and integrates your work.

## Read first (binding)
- `docs/AGENTS_SPEC.md` — agents/room contract (§0 ownership rules, §2 agent behaviour, §3 demo driver, §4 ops, §5 template, §6 CI)
- `docs/SPEC.md` — targets + divergence engine; `docs/SWITCH_PROTOCOL.md` — Switch 0.31.0 API with file:line citations
- Framework you build on (read-only): `agents/switch_client.py`, `agents/runtime.py`, `agents/commands.py` (grammar +
  `MARK` message prefixes), `agents/ledger.py`, `agents/provision.py`, `tests/fakes.py` (FakeRoom — faithful Switch
  semantics), `tests/procs.py`. Finished components (read-only): `targets/**`, `agents/codegen/**`,
  `agents/divergence.py`, `agents/bench.py`, `targets/proxy/router.py`.

## Environment
- Venv: `.venv/bin/python`, `.venv/bin/pytest` (do not pip install). 4 CPU cores: keep test suites lean, use small
  campaign/benchmark sizes in unit tests.
- Live stack (native, no Docker): Switch 0.31.0 API `http://localhost:18000`, Mattermost `http://localhost:18065`; creds
  in `.state/switch.env`; provisioned room + agent creds in `.state/room.json`, `.state/agents/*.json` — NEVER
  overwrite, rotate or delete these, never re-register `translator`/`fuzzer`/`cutover-gate`.

## Tree + swarm rules
1. You are one node of a tree. You may spawn your own Claude subagents (Agent tool) for independent sub-tasks; ALWAYS
   set `model` and `effort` explicitly (code: opus/high; mechanical: sonnet/medium). They may spawn their own.
2. You MUST delegate at least one well-specified sub-task to a non-Claude peer over HACP:
   write `.hacp/contracts/<task_id>.json` (unique task_id, prefix it with your node name) and run
   `.venv/bin/python tools/hacp_delegate.py .hacp/contracts/<task_id>.json` with Bash `run_in_background: true`
   (5–30 min; keep working; you are notified on completion), then read `.hacp/results/<task_id>.json` and verify the
   merged files yourself. Contract fields: see the docstring of `tools/hacp_delegate.py`.
   - **codex** `gpt-6.1-sol`, effort `high` — TEXT MODE: it cannot run commands or read files; put EVERY file it needs
     in `context`. Best for: writing a module against tests you wrote first, or a review (`outputs:
     [".hacp/reviews/<x>.md"]`, acceptance `grep -q '## Findings' …`).
   - **agy** `gemini-3.8-flash-high` (effort high) — edits files, runs `.venv/bin/pytest`. (Its Claude models hit the
     account quota at 05:30 UTC; the harness falls back to gemini automatically if one is requested.)
   - **opencode** `opencode/big-pickle` — edits files, runs commands.
   Peers work in an isolated workspace; only declared outputs that pass acceptance are merged (with a conflict check).
   YOU stay accountable: never accept unverified peer code; if a contract escalates, do the work yourself.
3. **Self-review (mandatory before you report):** spawn an adversarial reviewer subagent (model `opus`, effort `high`)
   on your files with this brief: "verify against docs/AGENTS_SPEC.md + docs/SPEC.md line by line; run the tests; try to
   break it — governance bypasses (an agent issuing human-only commands, impersonation via sender_name), malformed
   commands, mention-hygiene violations that would accidentally address another agent, rev/migration mismatches,
   idempotency, restart/rehydration from room documents, error paths that must fail loud, flaky tests, tests touching
   the real `targets/modern_fastapi/app/lists.py` or fixed ports; report defects with file:line and a failing
   scenario". Fix every valid finding with a regression test.

## Ownership
Write ONLY the files your task lists (plus new test helpers under `tests/` named after your task). If a real framework
defect blocks you, make a minimal surgical exact-string Edit (never a rewrite) and report it.

## Test rules
- Unit tests never touch the live Switch/Mattermost. Only `@pytest.mark.integration` tests may, and they clean up.
- Any test starting the modern target sets `MODERN_LISTS_PATH` to a tmp file — never write
  `targets/modern_fastapi/app/lists.py` (the live demo's translator owns it).
- Never bind fixed ports 9000–9002 in tests (the live demo may be running): `tests.procs.free_port()` / `service()`.
- Pass LEGACY/MODERN/PROXY URLs to agents under test explicitly (constructor args).

## Quality bar
Production-quality, documented code. Fail loud — no silent fallbacks; every fallback is announced. Mention hygiene:
agents write `@<agent>` only inside a deliberate `handoff()`. Human-only commands from agents → `MARK["governance_refusal"]`.

## Final report (your last message)
Files written; every HACP delegation (task_id, peer, model, verdict); subagents spawned (model/effort); framework edits
(if any, with why); the exact pytest summary line(s) of your tests and of `.venv/bin/pytest -q -m 'not integration'`.
