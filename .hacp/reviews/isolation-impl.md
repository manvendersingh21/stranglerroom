## Findings

### 1. High — a mutable marker ancestor permits silent fallback to root execution

**Location:** `agents/isolation.py:321` (`load_marker`), `agents/isolation.py:1251` (`write_marker`), `agents/isolation.py:1090`.

**Scenario:** Configure the supported marker override as `/tmp/control/markers/isolation.json`, where `control` belongs to the compromised service and `markers` belongs to root. Setup accepts this: marker operations check the immediate parent, but not its ancestors.

The service can rename the root-owned `markers` directory because it controls its non-sticky parent. Subsequent marker opens return `ENOENT`; `auto` resolves to off. Scripts then start components without isolation. If the rename occurs between the script's mode check and `exec_component`, that function independently resolves off and executes the component as root. Subsequent startup checks cannot undo code already executed with root privileges.

This requires the described marker override; the default `/etc/stranglerroom` ancestry is protected.

**Minimal fix:** Validate the entire marker-parent chain, including before treating absence as off, and reject service-controlled ancestors. Use checked directory descriptors for marker operations. Once a script selects isolation, require `on` for its child launchers so a disappearing marker produces an error.

### 2. Medium — run-directory creation follows unchecked ancestors before rejecting them

**Location:** `agents/isolation.py:269` (`ensure_run_dir`), particularly lines 275–281.

**Scenario:** Set `STRANGLER_RUN_DIR=/tmp/sr-run/slot/run`. The initial trusted-path check returns successfully when `sr-run` is missing. Before the creation loop reaches it, the attacker creates an ordinary service-owned `/tmp/sr-run` directory containing `slot -> /etc`.

The loop skips existing ancestors and performs `mkdir("/tmp/sr-run/slot/run", 0700)` as root, creating `/etc/run`. Only afterward does the final trusted-path check reject the service-owned ancestor. `fs.protected_symlinks=1` does not prevent this traversal: the symlink's immediate parent is the attacker's non-sticky directory.

This is a privileged filesystem side effect through an attacker-controlled path, although this sequence alone demonstrates directory creation rather than arbitrary file-content writes.

**Minimal fix:** Create and open each missing component relative to a checked parent descriptor. Immediately validate each opened directory's ownership and permissions before descending, using `O_DIRECTORY|O_NOFOLLOW`. Do not postpone validation until after creating the complete path.

### 3. High — absolute mirror links can retain attacker-controlled indirection

**Location:** `agents/isolation.py:748` (`_check_mirrored_link`), particularly lines 760–769; `agents/isolation.py:805`.

**Scenario:** A root-owned checkout entry such as `agents/cutover_agent.py` is an absolute symlink to `/tmp/linkbox/gate.py`. The compromised service owns the non-sticky `linkbox` directory and initially makes `gate.py` a symlink to a genuine root-owned module under `/opt`.

The checker calls `realpath`, validates the final `/opt` location, and copies the original absolute link verbatim. It never establishes that the original indirection is immutable. After validation, the attacker replaces `gate.py` with malicious Python. The cutover gate subsequently imports it under `sr-cutover-gate`, exposing its API key and router token to the attacker.

This requires an accepted source-link layout containing mutable indirection; planting entries solely inside a default private directory does not create that layout.

Verification also misses the changed target: `_not_root_only` at `agents/isolation.py:1961` checks symlink ownership without checking the destination's trust.

**Minimal fix:** Require every directory and symlink in an absolute link's original resolution chain to be protected against service modification. Reject mutable indirection rather than trusting its current `realpath`. Apply the same destination validation during verification.

### 4. Medium — target startup accepts health from a different process

**Location:** `scripts/targets.sh:71` (`isolated_listener`), `scripts/targets.sh:118`–125.

**Scenario:** A compromised `sr-modern` daemon holds port 9002 but initially returns an unhealthy response, causing `svc` to launch a new tracked modern process. The attacker discovers the new process through `/proc`, stops it after its isolated exec and before it binds, and makes the old daemon return successful health responses.

The tracked process is alive and has the expected uid. The listener also has that uid. Both checks pass, so the script reports successful startup even though the tracked process serves no requests and the answer came from the attacker's unrelated daemon.

The listener check establishes account ownership, not the claimed association between the listener and the tracked process.

**Minimal fix:** Associate the listening socket with the tracked process or its permitted reload descendants, including process start identity. Under the stated capability restrictions, inspect socket descriptors through a helper dropped to the component uid. Reject a listener belonging to another session or unrelated process.

### 5. High — verification can pass with an untracked component running as root

**Location:** `agents/isolation.py:1989`, `agents/isolation.py:1793` (`process_checks`).

**Scenario:** With an otherwise valid installation, run a component directly as root without its pid file, or leave a stale pid file pointing elsewhere. `live_pid` excludes that component from `pids`. Its running check becomes `SKIP`, which does not prevent verification from exiting zero.

The subsequent global process scan checks command lines for secret values, but does not reject component modules running under root or another unexpected uid. Environment inspection covers only service uids, so the root process is omitted there too. Thus static configuration, access probes and smoke imports can all pass while the live component has full root authority.

**Minimal fix:** Enumerate all processes running component entry points independently of pid files, and validate every discovered instance. Fail on unexpected uids, untracked instances and duplicates. Reserve `SKIP` for a component actually absent from the process inventory.

### 6. Medium — secret scanning ignores readable symlink aliases

**Location:** `agents/isolation.py:1621` (`scan_candidates`), `agents/isolation.py:1648` (`search_readable`).

**Scenario:** A readable backup containing another component's API key exists outside the scan roots, for example `/mnt/archive/key.json`. A symlink `/tmp/exposed-key` points to it.

A service can read the key through that path under `/tmp`, but candidate collection discards the symlink because its `lstat` result is not a regular file. Directory symlink aliases are likewise not traversed. The scan reports zero hits despite a readable secret alias inside its advertised search area.

**Minimal fix:** Include symlink candidates and resolve them only in the dropped-user helper. Follow them with `O_PATH`, validate the resulting descriptor as a regular file, then reopen and read through that descriptor. Handle directory aliases in the unprivileged traversal with cycle detection and resource limits.

## Verdict

**Reject pending fixes.** Supported marker and mirror-link configurations can break isolation, and verification can falsely certify a running root component.

No direct escape through the default protected paths, descriptor-based reset, or Python 3.12's fd-based `rmtree` is established by the supplied code. The uid, group and capability drop has explicit checks appropriate to the stated host.

This review is static. The translator's publication code and modern server's reload implementation were not supplied, so generated-module readability, reload and live agent connectivity cannot be confirmed. In particular, the translator receives `umask 077`; its writer must explicitly publish the shared module with mode `0644`. Import-only smoke checks do not establish those runtime properties.
