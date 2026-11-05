## Findings

1. **High — Nested relative symlinks bypass mirror confinement.** `agents/isolation.py:868`, `_check_relative_link()`.

   When recursive resolution encounters an absolute symlink, it returns `None`; callers then stop without checking their remaining path components. For example, inside one mirrored tree, let `a -> b`, `b -> /usr`, and `evil.py -> a/../tmp/payload.py`. All three links pass validation, but the mirrored `evil.py` resolves to `/tmp/payload.py`. A compromised service can supply that file and influence code another component imports.

   **Minimal fix:** Resolve one complete component queue, preserving every unconsumed suffix across symlink expansion. Once resolution becomes absolute, validate the entire remaining resolution with `check_link_target()`. Add a regression case with a relative link, an intermediate absolute link, and an escaping outer suffix.

2. **High — URL validation permits an authority parsing discrepancy.** `agents/isolation.py:509`, `check_ip_url()`.

   `http://localhost:18000\@127.0.0.1:18000/` passes: `urllib.parse.urlsplit(...).hostname` is `127.0.0.1`. Requests/urllib3 treats the backslash as an authority delimiter and instead requests `localhost:18000`. Such a consumer can resolve `localhost` to `::1` and send credentials to a service squatting there. The validator forwards the original ambiguous string.

   **Minimal fix:** Reject backslashes, control characters, and user information; require HTTP or HTTPS and a valid port. Construct and forward a canonical URL from the validated IP literal. Test the destination chosen by the actual HTTP transport, including this input, rather than testing only whether the validator raises.

3. **Medium — Directory identities in the secret scanner are disconnected from the directories actually searched.** `agents/isolation.py:1925` and `agents/isolation.py:1981`, `search_readable()`.

   The initial visited set follows current pathname resolutions, although candidate filenames came from an earlier root listing. An attacker can let root enumerate an empty `/tmp/decoy`, then replace it with a symlink to a readable directory outside the scan roots. The child seeds that target inode as visited despite having no candidate filenames for its contents. A second, previously enumerated symlink to that target is then skipped, hiding a readable secret without setting `truncated`.

   The link walker has a related race: it records the inode obtained through `O_PATH`, but lists and descends through the mutable pathname. Switching that pathname to an empty directory can mark the original directory visited without searching it.

   **Minimal fix:** Hold directory descriptors through enumeration and descent, and associate visited identities with those descriptors. Remove pathname-based pre-seeding, or retain and validate the identity and completeness of each original listing. An identity change must trigger a rescan or an incomplete-scan failure.

4. **Medium — The root scanner silently discards deep directories.** `agents/isolation.py:1858`, `scan_candidates()`.

   `depth > 256` returns without recording incomplete coverage. A readable secret in a stable directory 257 levels below `/tmp` is omitted, even with short names that keep the pathname below the kernel pathname limit. The later child scan receives no candidate for it, and verification can report zero hits and succeed.

   **Minimal fix:** Propagate depth-limit exhaustion to `secret_scan()` and emit a failing incomplete-scan row. Prefer an iterative descriptor walker with explicit resource limits whose exhaustion always fails verification.

5. **Medium — Process inventory remains evadable through argv changes or alternative Python invocation.** `agents/isolation.py:2103`, `component_instances()`.

   Inventory recognizes only exact `-m <component module>` argument pairs. A component can overwrite its argv or run a duplicate through `python -c` and `runpy.run_module()`. It then disappears from the instance checks; `live_pid()` also rejects its tracked PID because it applies the same argv test. Service-UID processes receive environment checks, but their tracking, credentials, groups, and session are not independently checked. An unisolated root-run agent invoked through `-c` is omitted from both inventories.

   **Minimal fix:** Validate every live tracked PID independently of its current argv, and inventory all service-UID processes and descendants with explicit handling of permitted workers. Fail on unexplained processes instead of treating them as absent. Use supervisor-maintained process membership where identity must survive argv changes; argv alone cannot establish complete component inventory.

6. **Medium — Listener verification ignores configured target endpoint ports.** `agents/isolation.py:2364`, `_expected_listeners()`.

   The Switch port is configurable, but proxy, legacy, and modern ports are hard-coded. With `PROXY_URL=http://127.0.0.1:19000`, a compromised service can listen on 19000 and receive requests intended for the proxy while verification checks only 9000. Those requests can include the cutover gate’s router token. A port collision also silently replaces an earlier dictionary entry: a Switch URL on port 9000 loses its expected root owner when the proxy entry is assigned.

   **Minimal fix:** Derive checks from every effective local endpoint, applying the proper HTTP/HTTPS default port. Reject conflicting owner expectations instead of overwriting them. Keep explicit checks for the documented default ports where required, and distinguish local endpoints from remote ones.

7. **High — Credential changes can reopen access to root’s inherited environment and memory.** `agents/isolation.py:1209`, `drop_privileges()`.

   Setting dumpability before the drop does not survive the credential change on a host configured with `fs.suid_dumpable=1`. After `setresuid()`, the process belongs to the service UID and is dumpable until the later `prctl()`, across several Python operations. Another process of that UID can race reads of `/proc/<pid>/environ`, which still contains the launcher’s original environment, or access inherited memory where ptrace policy permits. This also affects verification children holding the complete secret set.

   **Minimal fix:** Check and reject the unsafe dumpability configuration before changing credentials unless the transition is redesigned to avoid exposing root secrets. Moving the second `prctl()` immediately after `setresuid()` only narrows the race.

8. **Medium — Session-keyring separation fails open.** `agents/isolation.py:1220`, `_KEYCTL_SYSCALL` and `_join_session_keyring()`.

   Architectures outside the two-entry syscall table silently skip separation. On a supported Linux architecture such as riscv64, a child can therefore retain the launcher’s session keyring and read inherited keys granting possessor access. Similarly, tolerating `EPERM` does not establish that the old keyring is inaccessible: an operation-specific restriction can deny joining while permitting reads.

   **Minimal fix:** Use a portable keyutils interface or complete supported-platform handling, and fail closed when detachment cannot be established. Tolerate an unavailable facility only when inherited keyring access is demonstrably unavailable.

9. **High — The LLM executable check does not secure subsequent PATH resolution.** `agents/isolation.py:2401`, `llm_cli_check()`.

   With `PATH=/tmp:/usr/local/bin:/usr/bin`, suppose the configured CLI exists only as a trusted `/usr/local/bin/tool`. The check passes because it validates that selected file. A compromised service can subsequently create executable `/tmp/tool`; the translator’s later name-based invocation selects it and gives it the translator’s credentials and authority. Earlier writable PATH directories remain unchecked, and `launch()` does not pin the checked executable.

   **Minimal fix:** Resolve the CLI during launch, validate its resolution, and replace the command’s first word with the trusted absolute executable path that will actually be executed. Alternatively, reject mutable search directories preceding the executable and enforce that policy at every launch.

## Verdict

**Request changes.** Descriptor-based marker/run-directory handling and explicit isolation mode improve the implementation, but mirror confinement, URL validation, privilege transitions, and verification still have security gaps.

This was a static review of the supplied contents. Test sources and results were not supplied, so the correctness of the tests could not be verified.
