## Findings

1. **medium — agents/bench.py:160–161:** Transport failures discard elapsed request time, so latency statistics omit the slowest requests when they time out. If all 500 modern requests time out, p50/p95/p99 become `0.0`, and `compare()` reports `modern_faster_p99=True` against a healthy legacy service. Minimal fix: retain elapsed time for transport failures and include it in latency aggregation independently of status. Add a test mixing successful requests and timeouts that verifies failures contribute to percentiles.

2. **medium — agents/bench.py:160:** Catching every `Exception` hides configuration and implementation failures as ordinary transport errors. For example, an invalid absolute URL in `paths` raises `httpx.InvalidURL`, but the benchmark completes and the CLI exits successfully with an error-count card instead of exposing the malformed input. Minimal fix: catch `httpx.RequestError` for expected transport failures and let unexpected exceptions propagate. Add a test asserting malformed URLs raise while connection failures remain counted.

## Verdict

Request changes: retain failed-request latency and expose unexpected failures before using these results as cutover evidence.
