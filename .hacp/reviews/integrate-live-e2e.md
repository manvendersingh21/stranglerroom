## Findings

1. **High — stale target PID files can kill unrelated processes.** `scripts/targets.sh:36`–`38`.

   Scenario: a target exits without its PID file being removed; the OS reuses that PID for another service. The next `--fresh` sends TERM to that unrelated process and its children. Unlike `agents.sh`, this script checks neither process identity nor the expected module.

   Minimal fix: validate positive PIDs and verify the exact expected module before signalling either the process or its children.

2. **High — the governance probe can consume an earlier refusal.** `scripts/run_e2e_demo.py:565`–`568`.

   Scenario: an extra fuzzer `approve-cutover` after the decisions produces a refusal before the driver's probe. The driver retains the decisions timestamp as its anchor and discards the probe's returned event ID. If the actual probe subsequently fails to reach the handler—for example, addressing policy changes to prohibit the fuzzer after its benchmark handoff—the old refusal satisfies step 6b. The proxy remains legacy, and subsequent human approval can complete the run with exit 0 without this probe exercising the gate.

   Minimal fix: retain the probe event ID and require a persisted scorecard refusal whose `message_id`, sender and command match that exact probe.

3. **High — approval evidence is not bound to the driver's approval.** `scripts/run_e2e_demo.py:585`, `605`–`607`.

   Scenario: another `user` approval is queued during the benchmark, after the probe but before the benchmark report. After refusing the probe, the gate processes that earlier approval. Its flip occurs between the driver's final step-7 poll and creation of the driver's approval post; its 🚦 publication arrives afterward. Step 8 accepts that publication and its older approval ID. The driver's own approval takes the gate's idempotent path. The ledger checks pass although cutover preceded the driver's approval and used a command posted before the benchmark report.

   Minimal fix: resolve the driver's actual approval post to its Switch message ID and require that ID in both the 🚦 post and ledger. A unique per-run token in the approval command can make that resolution unambiguous.

4. **Medium — missing approval IDs disable the announcement comparison.** `scripts/run_e2e_demo.py:260`, `605`–`607`.

   Scenario: the gate publishes a correctly marked cutover post with the expected revision but omits `(message `sw_…`)`, or changes its formatting. Extraction returns `None`, which `check_ledger()` explicitly accepts as disabling the comparison. The driver exits 0 despite never establishing that the 🚦 post names the ledger's approval message.

   Minimal fix: require successful extraction of a nonempty approval ID before checking the ledger; make this argument mandatory for the live driver.

5. **Medium — rollback during the final settle period still produces PASS.** `scripts/run_e2e_demo.py:596`–`610`.

   Scenario: the driver checks modern routing and reads the sealed ledger, then a human rollback executes during the three-second settle. The gate successfully returns traffic to legacy and posts `↩️ Rollback complete`. The final scan checks only ⚠️ posts, so it accepts the rollback and exits 0 while the proxy serves legacy.

   Minimal fix: reject rollback outcomes during this run and repeat the routing assertions after the final settle.

6. **Medium — the required benchmark workload is unchecked.** `scripts/run_e2e_demo.py:245`–`247`, `576`–`581`.

   Scenario: a gate instantiated with `bench_total=1`, `bench_concurrency=1` and a single contract path performs a real benchmark, publishes p99 values and seals a valid ledger. Its constructor permits those values. The driver accepts the card and positive ledger p99s, then exits 0 although the required 500 requests per target, concurrency 50 and five endpoints were never exercised.

   Minimal fix: validate the ledger benchmark's total, concurrency, exact endpoint set and per-target request totals against the binding workload.

7. **Medium — `--fresh` can silently reuse a target that failed to stop.** `scripts/targets.sh:39`, `43`, `50`–`56`.

   Scenario: a target ignores TERM or retains its listener beyond the polling budget. `stop()` deletes its PID file and returns successfully without escalation. Fresh-start deletes generated state, then `svc()` accepts the surviving service's health response and launches nothing. The old process becomes untracked; a surviving modern-targeted proxy also causes the driver's next pre-flight to fail.

   Minimal fix: retain tracking until termination is confirmed, escalate verified survivors, and fail the fresh start if an expected listener remains occupied.

8. **Medium — concurrent target starts can lose ownership of the live process.** `scripts/targets.sh:16`, `23`–`27`.

   Scenario: two invocations both observe an unused port and launch servers. Process P binds successfully; Q fails to bind but overwrites the PID file. Both invocations then observe P's healthy endpoint and report success. A later stop targets dead Q and leaves P running, including across subsequent `--fresh` runs.

   Minimal fix: serialize target start/stop/fresh operations with a lock and confirm that the launched process remains alive before declaring startup successful.

9. **Medium — variation-selector tolerance breaks during v1 revision extraction.** `scripts/run_e2e_demo.py:130`, `503`.

   Scenario: the v1 post contains `🛠 v1 deployed` without U+FE0F. `has_mark()` deliberately normalizes this and accepts the post, but `find_rev(..., after=MARK["v1_deployed"])` searches raw text for `🛠️ v1 deployed`. It returns `None`, failing an otherwise valid run.

   Minimal fix: normalize both the body and the `after` string before locating the revision-search boundary.

10. **Medium — a transient history read permanently seals an unacceptable ledger.** `scripts/run_e2e_demo.py:259`.

    Scenario: the translator's `_check_approval()` encounters one transient `read_context` failure and returns `(None, None)`. `_record_cutover()` treats the missing problem as permission to seal, recording `approval_message_verified: null`. The driver's required `is True` check then fails despite the real human approval and successful cutover. Retrying `record-cutover` cannot repair it because the translator immediately returns for an already sealed ledger. A history window that excludes the approval similarly seals `False`.

    Minimal fix: retry or page the approval lookup before sealing, and require successful verification before closing the trail. Preserve the driver's strict assertion.

11. **Medium — the probe timeout includes a benchmark that can legitimately exceed it.** `scripts/run_e2e_demo.py:343`, `567`–`568`.

    Scenario: load increases after parity verification and each benchmark endpoint request takes nine seconds, remaining below the gate's ten-second HTTP timeout. The ten sequential identity-check requests take about 90 seconds; the two sequential 500-request benchmarks at concurrency 50 take about 180 more. Because the gate handles events sequentially, the probe waits behind roughly 270 seconds of successful work and the default 240-second wait fails before refusal.

    Minimal fix: budget the benchmark and queued probe according to the identity checks, both benchmark workloads and publication overhead; expose separate phase timeouts if necessary.

## Checked and fine

- The supplied transcript shows the intended normal-path ordering. The benchmark handoff is at `1793858213633`, the probe at `1793858213981`, and refusal at `1793858222569`. Waiting about nine seconds for that refusal is consistent with sequential handling.
- Step 7 correctly retains the decisions anchor: its benchmark card and approval request precede the probe refusal, so anchoring it on the refusal would incorrectly exclude both.
- Document notices cannot satisfy step expectations. Matching also checks the bot username, and emoji markers must start the message after leading mentions.
- The real v1 post includes U+FE0F, so the variation-selector defect did not affect this supplied run; console logging removes that selector.
- No unintended cross-agent mention is visible in the transcript. The four normal agent-to-agent mentions are deliberate handoffs. The additional fuzzer approval is the explicitly injected governance probe. Join greetings mention only their own agent, and human-facing examples contain no active agent mentions.
- The real 🚦 post carries `sw_ec03355df26b927e63d68227312c88a5` in the format the extractor expects.
- A slower modern benchmark is compatible with this contract: the verdict informs the human, who may approve.
- The repaired target launcher backgrounds the fully redirected server command, resolving the described captured-pipe wrapper problem.
- `agents.sh` serializes mutations with `flock`, closes the lock descriptor in launched agents, and verifies the exact `-m <module>` argument pair before signalling tracked PIDs.
