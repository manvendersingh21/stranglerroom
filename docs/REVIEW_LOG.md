# Review log — every review round and what happened to each finding

Every component was reviewed adversarially at least once, by a reviewer other than its author: Claude subagents
(opus, high effort) and peer CLIs over HACP (codex `gpt-6.1-sol`, opencode `opencode/big-pickle`, agy). Peer review
texts are in `.hacp/reviews/`; contracts, verdicts and session ledgers are in `.hacp/contracts/`, `.hacp/results/`,
`.hacp/sessions/`. "Fixed" means fixed in code **with a regression test** unless stated otherwise.

## Build phase

| Review | Reviewer | Findings | Disposition |
|---|---|---|---|
| `bench.md` — load benchmark | codex | 2 | both fixed: failed requests keep their latency (a timing-out target can no longer look faster); only transport errors are counted, caller bugs propagate |
| `proxy_router.md` — strangler proxy | codex | 5 | all fixed: no shared cookie jar between callers, non-string `target` → 400, flip requires `approved_by` + `evidence`, raw header bytes forwarded, raw body + `Content-Encoding` passed through |
| `fuzzer_agent.md` — @fuzzer | codex | 8 | all fixed (registry staging per migration, rehydration reconciliation, 30 s rev deadline, defused error text, strict decision parsing, …) |
| `integration_tests.md` — parity + live client tests | codex | 2 | both fixed (no rewrite of identical module bytes; `close()` on a never-started thread) |
| Node self-reviews (translator, fuzzer, cutover, e2e driver, ops, template, CI, integration tests) | Claude opus/high, one per node | 9–12 each | all valid findings fixed by the node; listed in each node's report (governance bypasses, replay handling, mention hygiene, ownership checks on room documents, start-up races, …) |
| Integration-tests node reviewer (delivered to the lead) | Claude opus/high | 4 + concerns | all fixed by the lead: `.json` classification narrowed to FastAPI's exact routing 404, deprecated clusters whose probes converge back → `deprecated-violated`, sha256 state-isolation check, cleanup that never masks the original failure, atomic module deploys |

## Integration phase

| Review | Reviewer | Findings | Disposition |
|---|---|---|---|
| `agents_protocol.md` — cross-agent protocol | codex | 6 | fixed: #2 failed recovery recorded as `proxy_target: unknown`, #3 replayed approval after rollback, #4 gate-fabricated approval (the translator now seals only on the lead's own `approve-cutover` found in room history, posted after build and benchmark), #5 fuzzer approving an older build; #1 (a translator deploy racing a gate flip) fixed later by the lead with a cross-process production lock (`agents/locks.py`, tests in `tests/test_production_lock.py`). **Accepted:** #6 — see residual risks |
| `agents_loop.md` — in-process loop test | opencode (agy hit its quota) | 4 | all fixed (rollback replay, rehydration announcements asserted, transcript checks after restart, idempotent re-posted decisions) |
| `integrate-live-e2e.md` — live driver + `targets.sh` | codex | 11 | 9 fixed (pid-verified stops, probe bound to its own `event_id`, approval evidence bound to the driver's approval, benchmark workload checked, rollback after the settle fails the run, `--fresh` never reuses an untracked server, `flock`, variation-selector tolerance, transient history reads); **declined:** #3 (a second approval on the human account racing the driver — the driver is the only actor using that account and every id is bound exactly), #11 (240 s step timeout ≈ 25× the observed 9 s; `--step-timeout` exists) |
| Live e2e node reviewer (two rounds) | Claude opus/high (read-only `claude -p`) | 2 + 4 | all fixed |
| `docs-readme.md` — README accuracy | codex + Claude | 33 + 21 | fixed except the wording choices the README node listed (overlap between the two reviews) |
| `critic-dod-audit.md` — Definition-of-Done audit | codex | 6 | all fixed (lead-only human commands, forged-approval seal refused, replayed rollback, replay protection across migrations, native stop of exec'd servers, docker Gateway UI port) |
| `critic-hardening.md` — critic's hardening | codex | 12 | 9 fixed by the critic; #2 (public default router token), #6 (replay on timestamp ties / unstamped ledgers) and #11 (approval beyond the last 200 messages) fixed later by the lead — see below |

## Lead's final rounds (the lead's own changes, reviewed by others)

| Round | Reviewer | Findings | Disposition |
|---|---|---|---|
| Benchmark methodology | lead (profiling) | the shared-pool load generator measured itself (httpcore O(requests×connections) scan) | fixed: closed-loop virtual users, one keep-alive connection each, unmeasured warm-up |
| `lead-final-delta.md` + Claude reviewer | codex + Claude | 3 + 4 | all fixed: one token rule for proxy and gate (incl. trimming), paging over equal timestamps, wording, benchmark refused while cut over, atomic token publication, card user count |
| `lead-fix-delta-rereview.md` + Claude reviewer | codex + Claude | 4 + 3 | all fixed: early floor stop removed (an older approval is found and refused with that reason), token files must be regular, non-symlinked, owner-only; `FakeRoom` reports Switch's page minimum timestamp; paging tests really page; the approval lookup became a **one-read** window ending at the approval's recorded Switch timestamp (the scan is only a fallback) |
| `lead-final-round-rereview.md` + Claude reviewer | codex + Claude | 2 + 1 (+1 test gap) | all fixed: fallback overlap capped at half the page span (no skipped partial bucket, strictly decreasing cursor), `O_NONBLOCK` (a FIFO token path can't hang start-up), unusable recorded timestamps fall back to the scan, non-conforming pages stop fail-closed. The Claude reviewer found **no defect that could forge or wrongly accept an approval** and verified timestamp units/boundaries against Switch's source |

## Residual risks (accepted, with reasons)

| Risk | Why it is accepted |
|---|---|
| Single-host trust: every component runs as one OS user, so a local process that can read `.state/` can act as an agent (credentials) or call `/_router/flip` (token) | A property of this local deployment, documented in the README. Closing it needs per-component OS users or hosts (and opening `/root`'s permissions on this box), which is an infrastructure change outside the demo's scope. Within Switch, governance holds: agents cannot issue human-only commands, room documents are creator-only, and the ledger is hash-chained with its head posted in the channel |
| No automatic retry of a lost handoff (protocol #6) | Liveness, not safety: nothing unsafe happens and the human can re-issue the command (`approve-cutover` again re-sends the record; `@fuzzer re-verify …`) |
| Approval lookup fallback limits (records without the recorded timestamp only) | Fails closed — the seal is refused, never forged — when Switch's timestamp-only paging cannot traverse the history (e.g. more than 500 messages within one millisecond). Every cutover the gate records carries the timestamp, so the one-read primary path is used (live runs: 0 fallbacks) |
