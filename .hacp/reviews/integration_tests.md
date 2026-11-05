## Findings

1. **Medium — `tests/test_parity_integration.py:66`: unchanged deployments can race the reloader.** The fixture already serves the naive module, and the first test rewrites identical bytes. `wait_for_rev()` immediately accepts the existing worker's matching revision, although that write can trigger a pending reload. The campaign can then encounter a restarting worker, producing transport-error clusters or failing a reset despite correct target behavior. **Minimal fix:** skip writing when the existing bytes equal the generated source; otherwise write and wait for the new revision.

2. **Medium — `agents/switch_client.py:466`: failed connection setup breaks shutdown and skips remote cleanup.** `connect()` records both threads but starts the heartbeat thread only after the stream becomes live. If stream setup fails or times out, its call to `close()` joins the unstarted heartbeat thread and raises `RuntimeError: cannot join thread before it is started`, replacing the connection error. The integration test's unguarded `client.close()` at `tests/test_switch_integration.py:254` repeats that exception and exits the cleanup block before deleting either registered agent. **Minimal fix:** make `close()` join only threads that have started and reliably close `_ops`. Also guard each client shutdown in the test so one shutdown error cannot prevent the remaining cleanup.

## Verdict

Request changes. Fix the reload race and failed-connect shutdown path before relying on these integration tests.
