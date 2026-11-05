## Findings

1. **High — Sender classification can authorize an ambiguous agent or guest as the lead.**

   **Evidence:** `agents/runtime.py:92–101` refreshes participants only when the lowercase name is absent from the cache. Line 97 marks duplicates ambiguous only when their types differ.

   **Scenario:** After `user` has been cached as a human, an agent named `USER` joins. Its messages reuse the cached `user → user` classification and pass every human-only gate. Independently, two human participants named `user` and `USER` never become ambiguous, even on a fresh lookup; the non-lead human receives lead privileges. Both contradict the contract’s ambiguity rule.

   **Exact fix:** Refresh participant identity before authorizing privileged commands, and mark every repeated case-folded name ambiguous, including repetitions with the same type. Prefer authenticated participant IDs for authorization when available; names alone cannot distinguish these participants.

2. **High — The default router credential bypasses room governance entirely.**

   **Evidence:** `agents/runtime.py:37` supplies the public default token `strangler-local-token`. `targets/proxy/router.py`, at `DEFAULT_ADMIN_TOKEN` and `_handle_admin`, accepts that same default and checks only that `approved_by` and `evidence` are nonempty strings.

   **Scenario:** In the shipped default configuration, a compromised local fuzzer process can POST directly to `/_router/flip` with that token, `target="modern"`, `approved_by="user"`, and arbitrary evidence. It changes production routing without submitting any human approval. This requires access to the local router, not merely the ability to post room messages.

   **Exact fix:** Remove the public fallback and require a generated credential at startup. Make the credential and router admin endpoint accessible only to the gate’s execution identity; do not expose them through the other agents’ environment or shared configuration.

3. **High — Seal verification accepts an approval that the gate correctly rejected as preceding the benchmark.**

   **Evidence:** `agents/translator_agent.py:737` passes only the latest **build** time to `_check_approval`. `_find_approval`, at lines 854–863, checks that floor and the command’s author/verb. It never checks that the approval followed the benchmark being sealed.

   **Scenario:** After v2 is deployed, the lead posts `approve-cutover` while benchmarking is still running. The gate correctly refuses it. Once the benchmark exists, a compromised cutover-gate can write its own scorecard with that genuine but rejected approval ID, mark the cutover verified, and send `record-cutover`. The translator accepts the message because it followed the build and seals a human sign-off on numbers the human had not approved.

   **Exact fix:** Require approval to follow both the current build and the benchmark being sealed, using precise completion/publication ordering. Bind approval to a specific benchmark, and validate that the cutover’s benchmark evidence identifies that same record. A genuine command after the build is insufficient evidence of approval for a later benchmark.

4. **High — Whole-second benchmark timestamps admit stale approvals.**

   **Evidence:** `agents/cutover_agent.py:280` stores `at=now_iso()`. `_require_approval_after_benchmark`, at lines 494–507, explicitly parses that second-truncated value and accepts timestamps equal to or newer than its second boundary.

   **Scenario:** A benchmark finishes at `12:00:10.900`, but a queued human approval was posted at `12:00:10.100`. The recorded benchmark time is `12:00:10Z`, so the approval passes and flips the proxy despite having been posted 800 ms before completion. It also preceded the subsequently published benchmark card.

   **Exact fix:** Preserve completion time at message timestamp precision and bind approvals to the published benchmark/card. For timestamps that cannot establish ordering, require an explicit benchmark reference or causal publication token rather than accepting the entire completion second.

5. **High — A previously handled, no-op decision can replay and undo a later human decision.**

   **Evidence:** `agents/translator_agent.py:581–585` deduplicates only messages that produced `decision-recorded` trail entries. The “already applied” branch at lines 623–633 returns without recording the incoming message ID.

   **Scenario:** The lead preserves D-1, then repeats that instruction in message A; A is correctly treated as a no-op. Later the lead changes D-1 to deprecate. Redelivering A now finds no matching trail entry, passes the migration timestamp floor, and deploys another build preserving D-1 again.

   **Exact fix:** Persist a separate set of handled decision message IDs, including successful no-op decisions. Check it before evaluating current policy, and save the no-op receipt before returning. Preserve enough receipt history to uphold replay protection for the migration.

6. **Medium — Migration replay protection fails on timestamp ties and unstamped existing ledgers.**

   **Evidence:** `agents/translator_agent.py:123–126` treats equal timestamps, or a missing reference timestamp, as acceptable. `_migrate`, at lines 480–488, remembers only the current request ID. The migration history constructed at lines 512–518 does not preserve earlier request IDs.

   **Scenario:** Two migration requests A and B are posted in the same millisecond. After B supersedes A, redelivering A passes both checks: its ID differs from B’s, and its timestamp equals B’s floor. It starts another migration and rewrites v1. Also, a ledger created before this revision has no `request_timestamp`; an older request from a superseded migration therefore passes the new floor check.

   **Exact fix:** Retain processed migration request IDs across ledger replacements. Resolve timestamp ties with room ordering or another persisted causal identifier. Backfill an existing ledger’s request timestamp from its original message; if unavailable, require explicit recovery instead of treating the missing floor as permission to replay.

7. **Medium — Participant lookup failures escape dispatch and stop the agent.**

   **Evidence:** `agents/runtime.py:160` calls `classify` before the exception handler beginning at line 164. `classify`, at lines 93–98, calls `switch.participants()` without handling failures. `run_forever`, at lines 183–188, has no per-event recovery outside `dispatch`.

   **Scenario:** The first addressed demo message arrives during a transient participants API failure. The exception escapes `dispatch`, exits `run_forever`, closes the Switch connection, and prevents the migration or approval from being handled. The promised loud handler failure and continued service do not occur.

   **Exact fix:** Put classification inside dispatch’s protected block. Treat lookup failure as failed authorization, report it without executing the command, and keep the event loop alive. Provide retry/redelivery handling so the legitimate command is not silently lost.

8. **Medium — Redelivered benchmark requests rerun work and invalidate legitimate approvals.**

   **Evidence:** `agents/cutover_agent.py:258–289` performs every benchmark request and appends a fresh result. Although line 283 records `request_message_id`, no code checks it before running.

   **Scenario:** The original benchmark finishes and the lead approves its card. A redelivery of the fuzzer’s same benchmark message is queued ahead of that approval. The gate runs another benchmark, replaces the latest completion time, then rejects the lead’s approval as preceding this duplicate run. The faithful fake explicitly supports same-ID redelivery with a new sequence.

   **Exact fix:** Persistently deduplicate benchmark request IDs before starting work. For a completed duplicate, reuse the existing result without changing its completion time or invalidating approval. Track in-progress requests as well.

9. **High — Native stop can shut down Postgres while an untracked server remains alive.**

   **Evidence:** `scripts/bootstrap_switch.sh:259–263` runs `pgrep` only when the recorded PID is absent or fails validation. After successfully stopping a recorded PID, lines 269–274 proceed without searching for additional matching servers.

   **Scenario:** Two native server instances use the same Postgres cluster, but only one PID is recorded—for example, another instance was started with a different listening port. Stop successfully terminates the recorded instance, leaves `alive=0`, and stops Postgres underneath the other instance. The advertised untracked-server protection does not cover this case.

   **Exact fix:** After stopping recorded processes, scan for every remaining instance belonging to this installation, regardless of whether the PID files were valid. Refuse database shutdown if any remain. Serialize start/stop and perform the final scan immediately before stopping Postgres.

10. **High — PID verification can kill an unrelated process after PID reuse.**

    **Evidence:** `scripts/bootstrap_switch.sh:257–259` accepts a regex substring anywhere in the flattened command line: `switch_core.main` or `bin/mattermost`. Line 265 then signals that PID.

    **Scenario:** A server exits without removing its PID file. Its PID is later reused by an editor opening `notes/switch_core.main.txt`, or a shell whose arguments contain `bin/mattermost`. The substring check succeeds and stop terminates the unrelated process. The dots in `switch_core.main` are also regex wildcards.

    **Exact fix:** Validate NUL-separated arguments and executable identity. Require the exact `-m`, `switch_core.main` argument sequence and expected Python/cwd for core; require the expected resolved executable for Mattermost. Record and validate process start time, or use a process handle that prevents PID reuse between validation and signaling. Scope untracked-process discovery using the same identity checks.

11. **Medium — A legitimate approval outside the last 200 messages cannot be recovered by another approval command.**

    **Evidence:** `agents/translator_agent.py:822` reads only `limit=200`; subsequent attempts repeat the same tail. `agents/cutover_agent.py:326–332` handles a new approval for an existing cutover by resending `record-cutover` while retaining the original approval ID.

    **Scenario:** More than 200 room messages arrive while flip verification or the translator handoff is delayed. The correct lead approval falls outside the readable tail, so sealing fails. Posting a fresh `approve-cutover` still resends the old approval reference and fails again. An otherwise successful cutover requires a production rollback merely to obtain verifiable approval evidence.

    **Exact fix:** Retrieve the approval by ID, or paginate history until its position is covered. Continue refusing to seal when evidence is unavailable. If fresh reaffirmation is supported, validate it against current evidence and persist the new approval reference before resending the record.

12. **Medium — Router microseconds versus message milliseconds can reject a genuinely newer command.**

    **Evidence:** `tests/fakes.py`, in `_event`, truncates message timestamps with `int(msg.at * 1000)`. The router writes `flipped_at=datetime.now(timezone.utc).isoformat()` with microseconds. `agents/cutover_agent.py:487` compares the truncated message time directly against that higher-precision value.

    **Scenario:** The router flips at `…00.123100`, and a rollback is posted afterward at `…00.123900`. Its wire timestamp represents `…00.123000`, so `_predates_last_flip` incorrectly labels it stale. Fast fake-backed flows and closely timed automated commands can therefore fail despite sharing one clock.

    **Exact fix:** Compare timestamps at a common precision. Resolve equal-millisecond cases using a routing generation or causal message ordering; do not infer that a command predates a flip from sub-millisecond information discarded by the message protocol.

## Verified OK

- The epoch unit conversion itself is correct: Switch event/history timestamps are milliseconds, and the checks divide by 1000 before comparing with Python epoch seconds. Build/benchmark parsing explicitly uses UTC. Shared host clocks do not justify a generic container clock-skew finding.
- The ordinary fuzzer governance probe is refused before `_approve` runs. `_refuse` reads router status and records the refusal without calling `flip`; sequential dispatch also preserves the in-progress benchmark state.
- Creator checks on matrix, scorecard, and ledger reads enforce the supplied creator-only document model. A fuzzer using its own room credentials cannot simply overwrite the gate’s scorecard.
- Missing cutover records, missing approval IDs, non-lead approvers, and a `verified` value other than literal `True` prevent sealing. Approval lookup failure does not silently become verified approval.
- The native `exec nohup … &` launches the asynchronous child with the server PID preserved through `exec`. Server stdin, stdout, and stderr are redirected, so the long-running server does not retain a capturing caller’s pipes.
- The expected false results in native stop’s `&&` checks are protected from `set -e`; a nonmatching `pgrep` followed by `continue` does not itself abort the script. The stop defects above concern process identification and incomplete discovery, rather than an unconditional errexit failure.
