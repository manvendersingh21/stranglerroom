# StranglerRoom Isolation Audit: Operator-Side Tools

## Overview

This audit evaluates the operator-side tooling—`scripts/run_e2e_demo.py`, `scripts/validate_template.py`, `scripts/bootstrap_switch.sh`, `agents/provision.py`, `Makefile`, and `.github/workflows/e2e.yml` (as well as helper scripts `scripts/targets.sh` and `scripts/agents.sh` invoked by them)—against the per-component Unix user isolation design specified in [docs/ISOLATION.md](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/docs/ISOLATION.md).

Under the isolation architecture:
- StranglerRoom components run as individual unprivileged system users (`sr-translator`, `sr-fuzzer`, `sr-cutover-gate`, `sr-proxy`, `sr-legacy`, `sr-modern`) with supplementary group memberships (`stranglerroom`, `sr-prodlock`).
- The operator stays `root` (trusted launcher, provisioner, and demo driver).
- Runtime state is partitioned under per-component subdirectories in `.state/` (e.g. `.state/proxy/router.json`, `.state/fuzzer/divergence_registry.json`, `.state/translator/pending_refusals.json`, `.state/legacy/legacy_openlibrary.log`).
- Process logs live in `.state/run/` (mode 0700 root; processes inherit descriptors).
- Secrets (`.state/switch.env` mode 0600 root, `.state/router.token` mode 0600 root) are hidden from components, while `.state/room.json` is public (mode 0644 root).
- CI runners execute as a non-root user without the isolation marker, so isolation must remain `off`.

---

## Findings

### 1. Paths Read or Deleted That Moved

#### 1.1 `scripts/targets.sh:113` (Invoked by `scripts/run_e2e_demo.py:807` and `Makefile:47`)
- **Location:** [scripts/targets.sh:113](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/targets.sh#L113)
- **Invocation:** Called directly by the operator via `scripts/targets.sh start --fresh`, by [scripts/run_e2e_demo.py:807](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/run_e2e_demo.py#L807) during pre-flight, and by [Makefile:47](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile#L47) in `make demo`.
- **Current Behavior:**
  ```bash
  rm -f "$ROOT/targets/modern_fastapi/app/lists.py" "$ROOT/.state/router.json" "$ROOT/.state/divergence_registry.json"
  ```
  Line 113 attempts to delete `.state/router.json` and `.state/divergence_registry.json`.
- **Breakage Under Section 2 Layout:**
  Under Section 2 of `docs/ISOLATION.md`, these state files moved into per-component directories:
  - `.state/router.json` $\rightarrow$ `.state/proxy/router.json` (owned by `sr-proxy`, mode 0700)
  - `.state/divergence_registry.json` $\rightarrow$ `.state/fuzzer/divergence_registry.json` (owned by `sr-fuzzer`, mode 0700)
  Because `targets.sh` attempts to delete the old top-level paths, the actual state files in `.state/proxy/router.json` and `.state/fuzzer/divergence_registry.json` are not removed.
  Consequently:
  - The proxy is not reset to legacy on a fresh start. If the proxy was previously flipped to `modern`, [scripts/run_e2e_demo.py:831-835](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/run_e2e_demo.py#L831-L835) fails pre-flight (`proxy must start on legacy`).
  - Stale divergences from previous fuzzing runs remain in `.state/fuzzer/divergence_registry.json`.
- **Minimal Fix:**
  Update [scripts/targets.sh:113](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/targets.sh#L113) to delete the moved paths (while keeping the legacy paths for backwards compatibility during transition):
  ```bash
  rm -f "$ROOT/targets/modern_fastapi/app/lists.py" \
        "$ROOT/.state/router.json" "$ROOT/.state/proxy/router.json" \
        "$ROOT/.state/divergence_registry.json" "$ROOT/.state/fuzzer/divergence_registry.json"
  ```

---

### 2. Files Written That Components Must Read (Ownership & Permissions)

#### 2.1 `agents/provision.py:60-62` — Missing Explicit Permissions on `.state/room.json`
- **Location:** [agents/provision.py:60-62](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/agents/provision.py#L60-L62)
- **Current Behavior:**
  ```python
  def save(self) -> None:
      self.path().parent.mkdir(parents=True, exist_ok=True)
      self.path().write_text(json.dumps(asdict(self), indent=2))
  ```
  `RoomInfo.save()` creates `.state/room.json` using the operator's current umask.
- **Breakage Under Isolation:**
  `docs/ISOLATION.md` §2 explicitly requires:
  ```
  room.json             0644 root (room id, channel, names: not secret)
  ```
  All three agent components (`translator`, `fuzzer`, `cutover-gate`), running as `sr-translator`, `sr-fuzzer`, and `sr-cutover-gate`, must read `.state/room.json` at startup ([agents/runtime.py:91](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/agents/runtime.py#L91)).
  When the operator runs provisioning as root under a restrictive umask (such as `0077`, as configured during dropped privileges in the launcher, §4), `room.json` is created with mode `0600` owned by `root:root`. The unprivileged service users receive `PermissionError: [Errno 13] Permission denied` when attempting to load `room.json`. Note that the launcher's preparation step (§4) does not touch `room.json`.
- **Minimal Fix:**
  Explicitly set mode `0644` in `RoomInfo.save()`:
  ```python
  def save(self) -> None:
      self.path().parent.mkdir(parents=True, exist_ok=True)
      self.path().write_text(json.dumps(asdict(self), indent=2))
      self.path().chmod(0o644)
  ```

#### 2.2 `agents/provision.py:71` / `agents/switch_client.py:165-171` — Traversal Mode for `.state/agents`
- **Location:** [agents/provision.py:71](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/agents/provision.py#L71) and [agents/switch_client.py:165-171](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/agents/switch_client.py#L165-L171)
- **Current Behavior:**
  `ensure_agents()` calls `register_agent()`, which invokes `creds.save()`:
  ```python
  path = self.path_for(self.name)
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(".tmp")
  tmp.write_text(json.dumps({"name": self.name, "agent_id": self.agent_id, "api_key": self.api_key}))
  tmp.chmod(0o600)
  tmp.replace(path)
  ```
- **Breakage Under Isolation:**
  When provisioning runs as root under umask `0077`, `path.parent.mkdir(parents=True, exist_ok=True)` creates `.state/agents/` with mode `0700` owned by root.
  `docs/ISOLATION.md` §2 mandates:
  ```
  agents/               0711 root when isolated (traverse only)
    translator.json     sr-translator 0600         (and fuzzer.json, cutover-gate.json likewise)
  ```
  If `.state/agents/` is created as `0700`, service users cannot traverse the directory until the launcher's `prepare` step runs and sets mode `0711`. Furthermore, `creds.save()` leaves `<name>.json` owned by `root:root` mode `0600`. While the launcher (§4 step 1) explicitly `lchown`s `<name>.json` to `sr-<name>` before starting each agent, if `provision.py` is executed to re-register an agent while agents are running, the file becomes temporarily unreadable by the service user.
- **Minimal Fix:**
  Ensure `.state/agents/` is created with mode `0711` in `AgentCredentials.save()`:
  ```python
  path.parent.mkdir(parents=True, exist_ok=True)
  path.parent.chmod(0o711)
  ```

#### 2.3 `scripts/bootstrap_switch.sh:92-104` — Operator-Only State File (`switch.env`)
- **Location:** [scripts/bootstrap_switch.sh:92-104](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/bootstrap_switch.sh#L92-L104)
- **Analysis:**
  `bootstrap_switch.sh` writes `$STATE/switch.env` and sets `chmod 600 "$STATE/switch.env"`.
  According to `docs/ISOLATION.md` §2:
  `switch.env operator only (0600 root). Agents never read it; they get SWITCH_URL from the launcher.`
- **Result:**
  **Compliant.** No change needed. Components never read `switch.env`; the root launcher extracts `SWITCH_URL` and passes it in the component's environment.

---

### 3. Process Signaling Across Different User IDs

#### 3.1 `Makefile:34, 56, 61-62` (and `scripts/agents.sh:114-134`, `scripts/targets.sh:64-81`)
- **Location:** [Makefile:34, 56, 61-62](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile#L34), [scripts/agents.sh:114-134](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/agents.sh#L114-L134), [scripts/targets.sh:64-81](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/targets.sh#L64-L81)
- **Current Behavior:**
  - `make agents-stop` calls `scripts/agents.sh stop`.
  - `make stop` calls `make agents-stop` and `scripts/targets.sh stop`.
  - `make clean-state` calls `scripts/agents.sh stop` and `scripts/targets.sh stop`.
  Both scripts read PIDs from `.state/run/<name>.pid` and signal them (`kill -TERM "$pid"`, `pkill -TERM -P "$pid"`, escalating to `kill -KILL`).
- **Behavior Under Isolation:**
  - When isolation is active, target and agent processes run under distinct UIDs (`sr-translator`, `sr-fuzzer`, `sr-cutover-gate`, `sr-proxy`, `sr-legacy`, `sr-modern`).
  - As noted in `docs/ISOLATION.md` §1 line 29: *"None can signal or ptrace another component's process (different uids)."*
  - The operator running these commands as `root` (trusted operator) has `CAP_KILL` and can signal all processes without issue.
  - However, if an unprivileged user (or a non-root test harness) invokes `make agents-stop` or `make stop` while processes run as `sr-*`, `kill -0 "$pid"` in [scripts/agents.sh:57](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/agents.sh#L57) and `kill -TERM "$pid"` fail with `EPERM` (Operation not permitted).
- **Minimal Fix:**
  No code change is required in the scripts as root signaling works as designed. However, documentation in `Makefile` and `scripts/agents.sh` / `scripts/targets.sh` should note that stopping isolated components requires root privileges (or sudo).

#### 3.2 `scripts/bootstrap_switch.sh:276-289` (`native_down`)
- **Location:** [scripts/bootstrap_switch.sh:276-289](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/bootstrap_switch.sh#L276-L289)
- **Analysis:**
  `native_down` signals `switch-core` and `mattermost` processes recorded in `.native/*.pid`.
  Per `docs/ISOLATION.md` §1 line 34, Switch, Mattermost, and Postgres are out of scope and run as host/native services, not under component service users.
- **Result:**
  **Compliant.** No change needed.

---

### 4. Log File Reading

#### 4.1 Process Logs in `.state/run/`
- **Location:** [scripts/run_e2e_demo.py:740](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/run_e2e_demo.py#L740), [scripts/targets.sh:54, 59](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/targets.sh#L54), [.github/workflows/e2e.yml:74](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/.github/workflows/e2e.yml#L74)
- **Analysis:**
  - `docs/ISOLATION.md` §2 specifies: `run/ 0700 root when isolated: pid files and logs (each process inherits its log descriptor)`.
  - [scripts/run_e2e_demo.py:740](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/run_e2e_demo.py#L740) refers to `.state/run/<name>.log` in an error message string when agents fail to reach `live` status; it does not read the file directly.
  - [scripts/targets.sh:54, 59](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/targets.sh#L54) reads `tail -20 "$RUN/$name.log"` if a target crashes on startup. Because `targets.sh` is invoked by the root operator, reading the log descriptor succeeds.
- **Result:**
  **Compliant.** Root has access to `.state/run/` logs.

---

### 5. CI Workflow (`.github/workflows/e2e.yml`) and Non-Root Environment

#### 5.1 CI Invariant: Isolation Must Stay Off
- **Location:** [.github/workflows/e2e.yml](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/.github/workflows/e2e.yml)
- **Current Behavior:**
  The workflow executes on `ubuntu-latest` as the standard GitHub Actions user (`runner`, UID 1001), not `root`. No `/etc/stranglerroom/isolation.json` marker file exists.
- **Analysis Under Isolation Design:**
  - According to `docs/ISOLATION.md` §3:
    `STRANGLER_ISOLATION` defaults to `auto`.
    In `auto` mode, isolation turns `on` if and only if the marker `/etc/stranglerroom/isolation.json` exists and its `"repo"` matches the checkout root. Otherwise, it is `off`.
    *"A CI runner or another checkout never picks it up."*
  - This design is critical for CI:
    1. The runner user is not root and cannot run `setuid(0)`, `useradd`, `groupadd`, or `setgroups()`.
    2. [.github/workflows/e2e.yml:69-78](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/.github/workflows/e2e.yml#L69-L78) uploads `.state/run/*.log` as a failure artifact. If isolation were accidentally on, `.state/run/` would be mode 0700 owned by `root`, causing `actions/upload-artifact@v4` to fail due to lack of read permissions.
    3. [.github/workflows/e2e.yml:61](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/.github/workflows/e2e.yml#L61) runs `validate_template.py --run`, which loads `.state/agents/translator.json` ([scripts/validate_template.py:406](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/validate_template.py#L406)). In CI, this file is written by the runner user during `make e2e` and remains readable.
- **Result:**
  **Compliant.** `.github/workflows/e2e.yml` does not set `STRANGLER_ISOLATION` or attempt to invoke isolation commands. Isolation remains `off` by default in CI as designed.

---

### 6. Missing Makefile Lifecycle Targets from Specification

#### 6.1 `Makefile:8-9` — Missing `isolate`, `isolate-off`, `isolate-verify`
- **Location:** [Makefile:8-9](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile#L8-L9)
- **Current Behavior:**
  `Makefile` defines standard targets (`switch-up`, `switch-down`, `targets`, `provision`, `agents`, `e2e`, `demo`, `cinema`, `stop`, `clean-state`), but contains no isolation management targets.
- **Specification in docs/ISOLATION.md:**
  - §5 line 110: `python -m agents.isolation setup (make isolate)`
  - §5 line 123: `python -m agents.isolation disable (make isolate-off)`
  - §6 line 126: `python -m agents.isolation verify (make isolate-verify)`
  An operator attempting to set up or verify isolation using the documented make commands receives `make: *** No rule to make target 'isolate'. Stop.`
- **Minimal Fix:**
  Add the missing targets to `.PHONY` and declare them in [Makefile](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile):
  ```makefile
  .PHONY: ... isolate isolate-off isolate-verify

  isolate: ## Set up isolation users, groups, and permissions.
  	$(PY) -m agents.isolation setup

  isolate-off: ## Tear down isolation permissions and users.
  	$(PY) -m agents.isolation disable

  isolate-verify: ## Verify isolation configuration, access matrix, and live processes.
  	$(PY) -m agents.isolation verify
  ```

#### 6.2 `Makefile:63-64` — State Cleanup under Isolation (`make clean-state`)
- **Location:** [Makefile:63-64](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile#L63-L64)
- **Current Behavior:**
  ```makefile
  clean-state: ## Stop agents+targets, then remove runtime state (keeps .state/switch.env).
  	bash scripts/agents.sh stop
  	bash scripts/targets.sh stop
  	mkdir -p .state
  	find .state -mindepth 1 -maxdepth 1 ! -name switch.env -exec rm -rf {} +
  ```
- **Behavior Under Isolation:**
  - When isolation is active, `.state/` contains per-user directories (`proxy/`, `fuzzer/`, `translator/`, `legacy/`, `cutover-gate/`, `modern/`, `run/`, `agents/`) owned by individual system users or root with mode 0700 / 0711.
  - When run by the root operator, `find ... -exec rm -rf {} +` successfully wipes runtime state. The launchers and provisioner recreate necessary directories on the next run (§4).
  - When run by a non-root user, `rm -rf` fails with permission errors on the root/service-user directories.
- **Minimal Fix:**
  Ensure `clean-state` documentation specifies that root privileges are required when isolation is active.

---

## Verdict

The per-component Unix user isolation design presented in [docs/ISOLATION.md](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/docs/ISOLATION.md) is sound and properly addresses privilege separation, credential protection, and unprivileged execution for all six StranglerRoom services.

To ensure all operator-side tools behave consistently and reliably with the new state layout and isolation mechanics, the following minimal fixes must be applied:

1. **State Reset Path Fix ([scripts/targets.sh:113](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/targets.sh#L113)):**
   Update `--fresh` cleanup to remove the relocated state paths `.state/proxy/router.json` and `.state/fuzzer/divergence_registry.json`. This prevents pre-flight failures in [scripts/run_e2e_demo.py:831-835](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/scripts/run_e2e_demo.py#L831-L835) and [Makefile:47](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile#L47).
2. **Explicit Public Mode for Room Info ([agents/provision.py:60-62](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/agents/provision.py#L60-L62)):**
   Add `self.path().chmod(0o644)` to `RoomInfo.save()` so `.state/room.json` is always world-readable regardless of root's umask, allowing `sr-translator`, `sr-fuzzer`, and `sr-cutover-gate` to read room metadata on startup.
3. **Traversal Permissions for Agent Credentials ([agents/switch_client.py:167](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/agents/switch_client.py#L167)):**
   Ensure `.state/agents/` is created with mode `0711` (`chmod(0o711)`) so service users can traverse into it to access their respective key files.
4. **Lifecycle Make Targets ([Makefile:8-9](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/Makefile#L8-L9)):**
   Add `make isolate`, `make isolate-off`, and `make isolate-verify` targets to expose the setup, teardown, and verification commands defined in `docs/ISOLATION.md` §5-6.
5. **CI Workflow ([.github/workflows/e2e.yml](file:///root/another_hackathon/.hacp/work/s-isolation-operator-audit-9d97b7/.github/workflows/e2e.yml)):**
   Maintain the existing invariant where `STRANGLER_ISOLATION` defaults to `auto` (resolving to `off` in CI due to the absence of the marker file), ensuring unprivileged CI operations and artifact log collection continue to succeed without modification.
