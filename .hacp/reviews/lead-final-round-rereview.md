## Findings

1. **P2 — Fallback skips a partially read timestamp bucket.** `agents/translator_agent.py:936`

   Scenario: omit `approval_timestamp_ms` and use the default 500-row page. In commit order, create 100 older messages at `T−1000`, 450 messages at `T` (the approval first), 100 at `T+100`, and 400 at `T+1100`. Every timestamp bucket fits within one page.

   The scan reads with cursors: unbounded, `T+5100`, `T+1350`, then `T+412`. That last page contains only the final 400 messages at `T` and the 100 at `T+100`, so the approval remains unread. Fresh messages increase the overlap to 624; the candidate `T+624` exceeds the current cursor, so line 936 replaces it with `T`. The exclusive cursor permanently excludes the unread approval. The next page contains only older messages, and verification returns false.

   Minimal fix: clamp the candidate to `before_ms−1` without also clamping it to `oldest`. Let repeated pages narrow the overlap before using an exclusive cursor at the page minimum.

2. **P2 — Opening a FIFO blocks before the regular-file check.** `targets/common/router_token.py:70`

   Scenario: configure `ROUTER_TOKEN_FILE` as an existing FIFO owned by the current uid with mode 0600, with no writer connected. `path.exists()` succeeds, but `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)` blocks indefinitely. Execution never reaches `fstat`, so proxy or cutover-agent startup hangs instead of rejecting the nonregular token file.

   Minimal fix: include `os.O_NONBLOCK` when opening the file, then retain the descriptor-based regular-file, ownership, and permission checks before reading.

## Verdict

Request changes. Two reproducible defects remain.
