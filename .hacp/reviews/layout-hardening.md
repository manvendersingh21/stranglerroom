## Findings

### 1. High — Native bootstrap still creates a readable database password file

**Location:** `scripts/bootstrap_switch.sh:171`

On a fresh native installation with umask `022`, `echo "$DB_OWNER_PASSWORD" >"$pw"` creates `/var/lib/stranglerroom/.pw` as `0644`. Changing its owner to `postgres` preserves those permissions. The parent directory created immediately beforehand is normally traversable, so another Unix user can read the database owner password while `initdb` runs. Removing the file afterward does not prevent this exposure.

**Minimal fix:** Create the password file with `mktemp` so it is `0600` from creation, write the password, transfer ownership to `postgres`, and arrange cleanup on failure as well as success.

### 2. Medium — Fresh target startup resets the wrong state directory

**Location:** `scripts/targets.sh:114`

With `STRANGLER_STATE_DIR=/some/state`, the proxy and fuzzer use `/some/state/proxy/router.json` and `/some/state/fuzzer/divergence_registry.json`, but `start --fresh` deletes files under the checkout’s `.state`. A previously cut-over proxy consequently restarts targeting modern, and divergence numbering survives a requested fresh run.

The same command also ignores `MODERN_LISTS_PATH`, so a configured generated module survives the reset.

**Minimal fix:** Resolve the effective state directory and explicit file overrides before deleting anything. Reset the configured proxy state, fuzzer registry, and generated module.

### 3. Medium — Shared settings, credentials, and room paths remain frozen at import

**Location:** `agents/switch_client.py:45`; `agents/provision.py:60`

`STATE_DIR` captures `STRANGLER_STATE_DIR` at import, and provisioning imports its own reference to that constant. If the environment changes after imports, the new component path helpers follow the new directory while settings, API keys, and `room.json` still use the old one. An agent can therefore fail to find its provisioned room/key or select credentials from an unintended state tree.

The new `state` fixture masks this defect by patching `sc.STATE_DIR` directly; the room publication test separately patches `pv.STATE_DIR`. These tests do not establish call-time environment handling for those paths.

**Minimal fix:** Introduce a shared call-time state-directory resolver, with the same blank-value and `expanduser()` handling as the new helpers, and use it for settings, credentials, and room paths. Test changing only the environment after import.

### 4. Medium — Bootstrap ignores the configured state directory

**Location:** `scripts/bootstrap_switch.sh:24`

`STATE="$ROOT/.state"` ignores `STRANGLER_STATE_DIR`. Running bootstrap with a custom state directory writes `switch.env` into the checkout, while a subsequently started Python process reads it from the custom directory. Provisioning then reports missing settings despite successful bootstrap, and credentials are left outside the selected state tree.

**Minimal fix:** Derive `STATE` from `STRANGLER_STATE_DIR`, using normalization consistent with the Python resolver.

### 5. Medium — The agent settings loader still reads and parses privileged secrets

**Location:** `agents/switch_client.py:165`

When `SWITCH_URL` is unset and `switch.env` is readable, `load_agent()` calls `read_env_file()`. That helper reads the entire file and constructs a dictionary containing the registration token and admin/human passwords before selecting `SWITCH_URL`. Empty returned settings fields do not establish the claimed property that these secrets are never loaded.

`test_load_agent_reads_only_switch_url_from_the_file` passes despite this behavior because it checks only the returned object. Isolated file permissions prevent this read when access is denied, but the normal readable-file fallback still loads every secret.

**Minimal fix:** Supply the agent URL through the environment or a separate public connection-settings file, and remove the agent loader’s access to the privileged file. Add a test that fails if the agent path reads `switch.env`.

## Verdict

Request changes. The native bootstrap password exposure and inconsistent state paths must be fixed. The agent loader also does not satisfy its stated secret-loading guarantee.

Review based on the supplied sources; no commands or tests were run.
