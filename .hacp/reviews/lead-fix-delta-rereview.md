## Findings

1. **P1 — Timestamp pagination permanently skips approvals inside a full timestamp bucket.** `agents/translator_agent.py:910–911`

   **Failing scenario:** Commit a valid approval at timestamp T, then 250 other messages also stamped T, with older history available. Switch repeatedly returns the highest-seq 200 messages; the approval is outside that page. Every positive overlap keeps those same 200 eligible. When the overlap reaches zero, `before=T` excludes the approval along with the already-seen messages. The scan subsequently reports it absent.

   The 200-read cap guarantees termination, but strict timestamp progress does not guarantee coverage. `test_a_full_page_within_one_millisecond_does_not_hide_an_older_approval` passes because its approval is **before** the burst; it does not test an approval hidden **within** it.

   **Minimal fix:** Look up the named approval directly, or provide sequence-based pagination that can traverse equal-timestamp rows. A timestamp-only exclusive cursor with a fixed page limit cannot enumerate this case. Add a regression with the approval outside the last 200 rows sharing T.

2. **P2 — The floor stop assumes an undocumented five-second bound on timestamp inversions.** `agents/translator_agent.py:903–904`

   **Failing scenario:** Let the approval have timestamp F+100 ms, where F is `not_before_ms`. A transaction that started at F−6000 ms commits after that approval, followed by 199 newer messages. The first page contains that delayed transaction and the 199 newer messages, excluding the approval by sequence. Its `oldest_timestamp` is below F−5000, so the scan immediately stops despite the omitted approval being newer than F.

   Switch's stated semantics impose no maximum transaction duration or timestamp inversion. A page's minimum timestamp therefore cannot prove that earlier-sequence rows all predate the floor. The added inversion test covers only a three-millisecond inversion.

   **Minimal fix:** Remove this floor-based early exit unless Switch supplies an enforceable ordering bound. Use a sequence cursor or direct approval lookup for coverage, then validate the approval's own timestamp against F. Add the delayed-transaction counterexample.

3. **P2 — Existing token files bypass the advertised permission protection.** `targets/common/router_token.py:44–58`

   **Failing scenario:** Provision `ROUTER_TOKEN_FILE` as an existing nonempty file with mode 0644, for example through a deployment script using its default umask. Both processes accept it without checking permissions. Another local user can read the token and authenticate arbitrary router flips. A symlink to a publicly readable file is accepted similarly. The 0600 protection applies only to newly created temporary files.

   The fresh-file permission test and successful cleanup test do not exercise either case.

   **Minimal fix:** Open the existing token securely and inspect the opened descriptor with `fstat`; reject nonregular files and group/other-accessible permissions. Define and enforce a symlink policy, using `O_NOFOLLOW` if symlinks are unsupported. Validate the published winner through the same path. Add tests for an existing 0644 token and a symlink.

4. **P3 — The card still misstates the actual user count for accepted nonpositive concurrency.** `agents/bench.py:311`

   **Failing scenario:** `run_benchmark(..., total=8, concurrency=0)` runs one virtual user because `effective_concurrency = max(1, concurrency)`, but its result retains concurrency zero. The comparison card reports “from 0 virtual users”; negative concurrency produces a negative count. Both the library and CLI accept these inputs. The new test covers only positive concurrency.

   **Minimal fix:** Either reject nonpositive concurrency consistently or render the effective count, `min(max(1, modern.concurrency), modern.total)`, for positive totals. Add a zero-concurrency case.

## Verdict

**Request changes.** Approval lookup remains incomplete under the supplied Switch semantics, and existing token files lack permission validation.

The shared resolver correctly unifies configuration precedence, and hard-link publication prevents competing ordinary creators from exposing partially written tokens. Abrupt termination can leave private temporary files; the resolver does not reuse them. Client construction inside `try` correctly enables cleanup of previously constructed clients. The benchmark guard permits benchmarking after a normal rollback because rollback clears the active cutover record.

Assessment is based solely on the supplied source and diff; no commands or tests were run.
