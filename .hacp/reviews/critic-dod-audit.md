## Findings

Static audit of the supplied excerpts only; no commands were run. The promised `.hacp/contracts/critic-dod-facts.txt` is explicitly absent. Actual test counts, recorded timings, live governance results, template results and upstream compose port mappings therefore remain unverified. The driver, command parsers, ledger constants, target launcher, template and upstream compose file were not inlined.

1. **high — Human-only authorization also admits humans other than the configured lead.**

   **Evidence:** `agents/runtime.py:84` classifies every participant whose type is `user` as human, without comparing its name with `self.room.human`. The translator authorizes migration and decisions using that classification at `agents/translator_agent.py:226`; the cutover gate uses it at `agents/cutover_agent.py:212`. Nevertheless, the refusal text at `agents/cutover_agent.py:244` says only the configured human can approve or roll back.

   **Failing scenario:** Add another human account, `alice`, to the room. Its participant record is `{name: "alice", type: "user"}`. Alice can send `rollback`, approve a prepared cutover, start a migration or apply divergence decisions despite the configured lead remaining `user`.

   **Exact fix:** For privileged commands, require a current participant record with both `type == "user"` and the configured human's canonical name. Refresh this authorization information before execution, and reject ambiguous case-folded participant names instead of silently overwriting dictionary entries. Apply the same restriction when validating a recorded approver.

2. **high — The cutover-gate document writer can forge human approval and obtain a sealed ledger.**

   **Evidence:** `agents/translator_agent.py:687` reads the gate-owned scorecard and checks its claimed approver. At `agents/translator_agent.py:703`, sealing is blocked only when `_check_approval` returns an error string. Missing approval IDs return `(False, None)` at `agents/translator_agent.py:771`; unavailable or missing history also returns no error at `agents/translator_agent.py:790`. The code copies `cut["verified"]` into the ledger at `agents/translator_agent.py:724` without requiring it to be true, and seals at `agents/translator_agent.py:753`.

   **Failing scenario:** Approved parity and a real benchmark exist, but the human has not approved. A compromised cutover-gate updates its own scorecard with the current revision/migration, `approved_by: "user"`, `proxy_target: "modern"`, `verified: false`, and no approval message ID. It sends a legitimate `record-cutover` handoff. Creator checks pass, the missing approval produces no problem, and the translator seals the ledger while the proxy can still be on legacy.

   This demonstrates forged approval and cutover evidence; it does not demonstrate an ordinary agent message passing the gate's `_approve` authorization check.

   **Exact fix:** Require a nonempty approval message ID and `_check_approval(...)` to return verified `True`; defer sealing when history cannot establish approval. Validate the source message's authorized human identity and intended cutover-gate recipient, and bind it to the current approval request. Require successful routing verification for the current revision before sealing; do not accept a scorecard's unverified routing claim as sufficient evidence.

3. **high — Replayed rollback messages can change routing after previously doing nothing.**

   **Evidence:** `agents/cutover_agent.py:416` deduplicates rollback messages only against recorded rollbacks. When the proxy already serves legacy and no cutover record exists, `agents/cutover_agent.py:437` announces a no-op and returns without recording the message ID. Additionally, rollback and approval histories are capped at 50 entries (`agents/cutover_agent.py:63`, `agents/cutover_agent.py:448`, `agents/cutover_agent.py:475`).

   **Failing scenario:** A human posts rollback while the proxy is already on legacy. Later, a new human approval moves it to modern. Redelivery of the earlier rollback reaches the handler, finds no matching rollback record, and moves traffic back to legacy without a new human instruction. Even recorded commands lose replay protection after their history entries are evicted.

   **Exact fix:** Maintain durable processed-message IDs separately from the capped display history. Record no-op commands as handled, check those IDs before executing routing operations, and retain protection across restarts and history pruning. Persist operation intent/outcome so a crash between a flip and scorecard persistence cannot silently remove replay protection.

4. **medium — Translator replay protection does not survive migration replacement.**

   **Evidence:** `agents/translator_agent.py:468` checks migration request IDs only against the current migration. The history saved at `agents/translator_agent.py:493` omits those request IDs. Decision deduplication at `agents/translator_agent.py:560` searches only the current ledger's trail.

   **Failing scenario:** Migration A receives a human decision message. After rollback, the human starts migration B and its fuzzer publishes clusters with the same D-n identifiers. Redelivering A's decision message now passes B's deduplication check and applies the old decisions to B's current clusters. Similarly, redelivering A's migration request after B becomes current starts another migration instead of being recognized as already handled.

   **Exact fix:** Persist processed human-command message IDs across all migrations in the room, rather than retaining them only in the current migration's fields or trail. Record the original migration association and reject a previously processed message before interpreting its D-n identifiers against a different migration.

5. **medium — Native stop records and kills background wrapper PIDs instead of explicitly exec'd services.**

   **Evidence:** `scripts/bootstrap_switch.sh:203` backgrounds `cd ... && nohup ./bin/mattermost ...`, then records `$!`. The core launch uses the same structure at `scripts/bootstrap_switch.sh:219`. These background AND-lists introduce shell wrappers. `native_down` sends a signal only to the recorded PID at `scripts/bootstrap_switch.sh:253`, then stops PostgreSQL without checking that either service exited. `Makefile:59` exposes this through `make stop`.

   **Failing scenario:** Start the native stack, then run `make switch-down` or `make stop`. Killing a recorded wrapper leaves its Mattermost or switch-core child running and holding its listening port, while PostgreSQL is stopped. The documented stop operation consequently leaves part of the stack alive.

   **Exact fix:** Add `exec` before each backgrounded `nohup` service command so the asynchronous shell becomes the service and the saved PID identifies it. During shutdown, verify the PID's command, wait for termination, and confirm the service exited before stopping its database.

6. **low — Remote recording instructions omit the Docker Gateway UI port.**

   **Evidence:** `README.md:247` instructs remote users to forward ports 18065 and 18000. The optional Gateway UI URL at `README.md:252`, and the mode table at `README.md:291`, use port 3000 for Docker.

   **Failing scenario:** A remote Docker-mode user follows the forwarding instructions and opens the documented `http://localhost:3000`. That port has not been forwarded, so the UI is inaccessible through the described setup.

   **Exact fix:** Make the forwarding instruction mode-specific: forward 18065 for Mattermost, plus 18000 for the native Gateway UI or 3000 for the Docker Gateway UI. Include Docker API port 8000 when remote API access is needed.

## Verified OK

- **Make targets and flags:** The documented Make targets exist. `e2e` supplies `--fresh`; `cinema` supplies `--cinema --fresh`; `demo` invokes Switch startup, fresh targets, provisioning, agents and e2e in the documented order. Bootstrap accepts the documented default startup, `up`, `stop`, `down` and mode environment variable.

- **CI ordering and interpreters:** The e2e job depends on unit tests. Python and uv are installed before use; `make install` creates the application venv before `make e2e`. The workflow explicitly prepares switch-core's separate venv before template validation. The driver provisioning order described in `docs/AGENTS_SPEC.md` supports calling `make e2e` without separate workflow provisioning steps.

- **CI artifacts:** Compose log collection writes the same `.state/ci/compose.log` path uploaded afterward. Agent log paths agree with the documented `.state/run/` location. Both collection and upload include failure/cancellation conditions, and hidden-file inclusion covers `.state/`. Runtime sufficiency of the timeouts cannot be established without the missing live facts and driver implementation.

- **Bootstrap configuration:** Both startup modes check out `switch-v0.31.0`. Native defaults select API port 18000; Docker defaults select 8000; Mattermost defaults to 18065. Bootstrap writes those selected URLs to `.state/switch.env`, and `Settings.load()` reads that file with process-environment precedence. Whether upstream compose consumes the port variables as expected remains unverified.

- **Registration and policy unlock:** New registration requests include `connection_model: "always_on"` and `message_exchange: true`. Provisioning clears all three agents' addressing policies through the authenticated Gateway with `{"policy": null}` and checks the returned policy.

- **SSE and heartbeats:** Runtime connects with the provisioned room. Stream parameters explicitly include `scope=single`, `filter=addressed` and `rooms`; the heartbeat interval is 2.0 seconds. Reader and heartbeat threads operate independently of sequential command handling. Sequence deduplication is implemented in memory.

- **Room and document operations:** Fresh-room creation passes all three agent names and `user_names=["user"]`, checks attachment failures and obtains bridge metadata. Document operations use `create_room_document` and `update_room_document` with the connection header. Upsert and cross-agent evidence reads check creator IDs. The required ledger title is specified exactly as `Parity & Cutover Audit Ledger`; its actual imported constant cannot be confirmed without `agents/ledger.py`.

- **Ordinary non-human commands:** With accurate participant metadata, agent and unknown senders fail the human-only checks before migration, decisions, approval or rollback handlers execute. Message text is not used to assign sender type. `record-cutover` additionally requires an agent classified as `cutover-gate`. No supplied code establishes that an agent can arbitrarily forge server-generated `sender_name`, acquire a colliding human name, or reuse a removed human name; those bypasses are therefore not asserted. Known-name cache entries are not refreshed automatically.

- **Cutover safeguards:** Approval rechecks modern revision and approved parity, verifies routing after the flip, and attempts automatic reversion on failure. Unknown routing blocks translator deployments. Evidence documents are checked against their expected agent creators.

- **README arithmetic:** The stated campaign totals are internally consistent: 31 + 131 = 162 and 97 + 65 = 162; the five listed divergent-hit counts total 131. The benchmark defaults match the stated 500 requests, concurrency 50 and five paths, with legacy and modern measured sequentially. Actual recorded results and the 739/737/2 test counts remain unverified.
