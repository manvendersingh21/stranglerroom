## Findings

### 1. High — Failed keyring queries are treated as proof that no keyring exists

**Location:** `agents/isolation.py:1440`, `agents/isolation.py:1454`

**Scenario:** `_no_inherited_keyring()` accepts any negative GET_KEYRING_ID result, including EPERM, EACCES and the synthetic ENOSYS returned for unknown architectures. An empty `/proc/keys` does not establish absence: it can be masked by the container or filtered by permissions. For example, a sandbox can hide that file and deny JOIN/GET while permitting other key operations. The launcher then proceeds with an inherited root session keyring still attached and potentially usable through possession permissions.

**Minimal fix:** Fail closed on denied or unsupported queries. Require successful separation, or a trustworthy, specific proof that no inherited keyring/key facility exists; do not interpret arbitrary syscall failure or an empty proc listing as that proof.

### 2. Medium — Enumeration bypasses the advertised resource caps

**Location:** `agents/isolation.py:931`, `agents/isolation.py:2156`, `agents/isolation.py:2311`

**Scenario:** The root scan builds unlimited lists of directory entries, candidates and visited path strings before starting a timed child. A service can create a wide tree of empty directories: neither the readable-file cap nor the byte cap counts these directories. Directory churn can further prolong enumeration. The 1,800-second child timeout does not cover this root phase. The remover likewise eagerly lists directories without a work budget. In the dropped symlink walker, reaching a file/byte cap records truncation but does not stop directory traversal.

**Minimal fix:** Enumerate incrementally and impose directory, entry, memory and elapsed-time budgets. Stop traversal when a budget is exhausted and report incomplete scanning/removal as failure. Apply the limits before materializing directory listings.

### 3. Medium — Descriptor exhaustion silently produces a “complete” secret scan

**Location:** `agents/isolation.py:2164`, `agents/isolation.py:2199`

**Scenario:** The root walker holds one descriptor per directory level, but its depth allowance is 1,024. With an ordinary 1,024-descriptor limit, a service-created nested tree exhausts descriptors before reaching the explicit depth check. The child-directory open raises EMFILE and is silently skipped. No problem is recorded, so files beneath that directory are omitted while the secret-scan summary can report PASS with complete coverage. Other listing/open failures are similarly discarded.

**Minimal fix:** Record unexpected enumeration failures, including EMFILE/ENFILE, as incomplete coverage and FAIL verification. Bound descriptor usage below the available limit and close the stack on exceptional exits. Distinguish optional missing roots from failed traversal of an existing tree.

### 4. Medium — Unknown-mode fallback touches the run directory whose validation failed

**Location:** `scripts/agents.sh:52`, `scripts/targets.sh:47`, `agents/isolation.py:2568`

**Scenario:** Set `STRANGLER_RUN_DIR` to a directory inside the translator’s private state. Mode validation correctly rejects the service-owned path, but `agents.sh stop` enters unknown mode and still reads and removes PID files through that path. A service-created `translator.pid` FIFO blocks root’s `cat` before the service-user sweep runs. Swapped ancestor symlinks also redirect these pathname operations. Targets have the same fallback problem when STATE/run is untrusted. Python status/verify also read PID files without requiring successful run-directory validation.

**Minimal fix:** In unknown mode, sweep the fixed service accounts without touching PID files unless the run directory is independently validated. Require trusted, descriptor-relative, bounded regular-file reads for Python PID helpers too.

### 5. Medium — IPv4-mapped loopback endpoints escape listener verification

**Location:** `agents/isolation.py:2781`

**Scenario:** `SWITCH_URL=http://[::ffff:127.0.0.1]:18000` passes canonicalization and connects to the local IPv4 loopback listener. However, the IPv6 address’s `is_loopback` property is false, so `_expected_listeners()` omits port 18000. When Switch is absent, a service-owned listener can receive agent credentials without failing this listener check. Local addresses assigned to non-loopback interfaces are also incorrectly treated as remote.

**Minimal fix:** Classify mapped IPv4 addresses using their underlying address, and identify other addresses assigned to this host when deciding whether an endpoint is local. Preserve listener ownership checks for every local destination.

### 6. Medium — Exhausted kill sweeps still allow successful stop/restart

**Location:** `scripts/agents.sh:131`, `scripts/targets.sh:107`

**Scenario:** After 50 rounds, `kill_service_user()` prints a warning and returns success even if processes remain. A compromised service maintaining processes across successive snapshots can survive the bounded sweep. The callers then discard PID files and, for agent restart or target `--fresh`, proceed with new launches or state reset while the previous service processes remain.

**Minimal fix:** Perform a final inventory and return failure if survivors remain. Aggregate failures while attempting every service, then make stop return nonzero and prevent restart/reset/start from proceeding after an unsuccessful sweep. Keep the bounded loop.

### 7. Medium — Path resolution still disagrees with Python for named-user home expansion

**Location:** `scripts/agents.sh:25`, `scripts/targets.sh:30`

**Scenario:** Python’s `repo_state_dir()` expands inputs such as `STRANGLER_STATE_DIR=~root/custom-state`. Both shell resolvers expand only bare `~` and `~/`, so they instead use `<checkout>/~root/custom-state`. PID files therefore go to a different run directory from the one Python status/verify reads. `--fresh` also removes the wrong state files; named-user expansion in ROUTER_STATE_FILE has the same mismatch.

**Minimal fix:** Share Python’s path-resolution rules, including full `expanduser` behavior and normalization, instead of maintaining incomplete shell substitutes.

## Verdict

**Request changes.** Keyring separation remains fail-open in the fallback, verification can omit coverage or exceed its budgets, and stop/reset handling still has operational and trust-boundary gaps.

The fd-relative remover’s check-to-unlink/rmdir races do not themselves create a symlink-following deletion primitive: final-component unlink and rmdir do not follow a replacement symlink. The fixed PATH, command pinning, component-queue resolution, dumpability handling, tracked-PID inventory, and normal isolated status checks appear sound within the stated trust model. The service-user prefix comes from the operator’s launcher environment; the supplied code gives service processes no way to change that root environment.

This was a source-only review of the supplied material; no commands or runtime checks were performed.
