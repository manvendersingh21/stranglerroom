# Operator Re-audit: Per-component OS Users ("Isolation")

This audit evaluates the operator-facing components of StranglerRoom with isolation implemented (`docs/ISOLATION.md`, `agents/isolation.py`), reviewing the Makefile, operator scripts (`scripts/targets.sh`, `scripts/agents.sh`, `scripts/run_e2e_demo.py`, `scripts/validate_template.py`, `scripts/bootstrap_switch.sh`), provisioning (`agents/provision.py`), CI workflow (`.github/workflows/e2e.yml`), and documentation (`README.md`, `docs/ISOLATION.md`).

---

## Findings

### 1. Operator Steps That Break or Misbehave Under Isolation

#### Finding 1.1: `make clean-state` crashes for non-root users when isolation is off if `/srv/stranglerroom/state` exists
- **file:line**: `Makefile:63`, `agents/isolation.py:2537-2545`
- **What happens**: `Makefile:63` invokes `PYTHONPATH=. $(PY) -m agents.isolation clean` unconditionally whenever `make clean-state` is run, without checking if isolation is `on`. In `agents/isolation.py:2537-2545`, `clean` executes "whatever the mode" whenever `clean_target()` returns a path (which defaults to `/srv/stranglerroom/state` if that directory exists from a prior run or setup). It then enforces `_require_root("clean")` at line 2543. As a result, a non-root developer or CI runner executing `make clean-state` with isolation off encounters `IsolationError: clean must run as root` (exit code 2), causing the entire target to fail.
- **Minimal fix**: In `Makefile:63`, guard the invocation so it only runs when isolation is active:
  ```makefile
  @if [ "$$(PYTHONPATH=. $(PY) -m agents.isolation mode 2>/dev/null)" = "on" ]; then \
      PYTHONPATH=. $(PY) -m agents.isolation clean; \
  fi
  ```
  Alternatively, in `agents/isolation.py:main`, if `not isolation_on()` or if the caller is not root when mode is off, skip service area cleaning and return 0.

#### Finding 1.2: Asynchronous process termination in `stop` scripts races with `isolation clean` in `make clean-state`
- **file:line**: `Makefile:61-63`, `scripts/targets.sh:154`, `scripts/agents.sh:198`, `agents/isolation.py:1448-1453`
- **What happens**: When `make clean-state` is called with isolation on, it executes `scripts/agents.sh stop` and `scripts/targets.sh stop`, followed immediately by `python -m agents.isolation clean`. Both stop scripts send `pkill -KILL -u "sr-$name"` (`scripts/targets.sh:154`, `scripts/agents.sh:198`). Because `pkill` transmits signals asynchronously without awaiting process termination, child processes or workers may still appear in `/proc` when `clean` starts. At `agents/isolation.py:2544`, `_refuse_while_running("clean", None)` checks `/proc` for running component modules and service UIDs. If any process has not finished terminating, it aborts with `IsolationError: clean refuses while StranglerRoom components run: <pid> ...`, aborting `make clean-state`.
- **Minimal fix**: In `scripts/targets.sh:154` and `scripts/agents.sh:198`, wait for service UID processes to terminate before returning, e.g.:
  ```bash
  if [ -n "${ISOLATE:-}" ]; then
    pkill -KILL -u "sr-$name" 2>/dev/null || true
    while pgrep -u "sr-$name" >/dev/null 2>&1; do sleep 0.05; done
  fi
  ```

#### Finding 1.3: Provisioning after agents are started does not update the service area state copies
- **file:line**: `agents/provision.py:59-74`, `agents/switch_client.py:45, 181-209`
- **What happens**: `agents/provision.py` is unaware of isolation. It saves room data to `STATE_DIR / "room.json"` and agent credentials to `STATE_DIR / "agents/<name>.json"`, where `STATE_DIR` resolves to the operator checkout's `.state/`. In isolated mode, running agents execute with `STRANGLER_STATE_DIR=/srv/stranglerroom/state` and cannot read `.state/` (0700 root-owned). The copies in `/srv/stranglerroom/state/room.json` and `/srv/stranglerroom/state/agents/` are only created by `agents/isolation.py:prepare()`, which is called during `setup` or during launcher startup (`agents.isolation exec`). Consequently:
  1. If an operator runs `make provision` (or `python -m agents.provision --fresh-room`) while agents are running, the running agents continue using the old room and/or old credentials in `/srv/stranglerroom/state/`. If credentials were rotated, agents begin failing with HTTP 401.
  2. If an operator runs `make isolate` followed by `make switch-up` and `make provision`, but runs `make isolate-verify` before starting the agents with `make agents`, `isolate-verify` reports `FAIL` on "read own API key copy" and "read room.json" because the files exist only in `.state/` and were never copied to `/srv/stranglerroom/state/`.
- **Minimal fix**: In `agents/provision.py:ensure_room` (and `ensure_agents`), check `agents.isolation.isolation_on()`. If isolation is on and the current process is root (`os.geteuid() == 0`), call `prepare()` for each agent component (or copy the generated files to `paths.room` and `paths.key(comp)`). If components are already running, warn the operator to run `scripts/agents.sh restart`.

#### Finding 1.4: `make demo` ordering redundantly executes fresh resets and re-provisions while agents are live
- **file:line**: `Makefile:45-51`, `scripts/run_e2e_demo.py:807-814`
- **What happens**: `Makefile:45-51` defines `demo` as:
  ```makefile
  demo:
  	+$(MAKE) switch-up
  	PYTHON=$(PY) bash scripts/targets.sh start --fresh
  	+$(MAKE) provision
  	+$(MAKE) agents
  	+$(MAKE) e2e
  ```
  However, `make e2e` runs `$(PY) scripts/run_e2e_demo.py --fresh` (`Makefile:43`). In `scripts/run_e2e_demo.py:807-814`, `--fresh` executes:
  - `scripts/targets.sh start --fresh` (stops and restarts targets)
  - `agents.provision --fresh-room`
  - `scripts/agents.sh restart`
  Because `targets.sh` does not stop agents, the agents started by step 4 of `make demo` are still running when line 809 runs `agents.provision --fresh-room`. The translator agent from the provisioner script connects to Switch and archives the active room out from under the running agents, before `agents.sh restart` restarts them at line 813. Furthermore, targets, provisioning, and agents are started twice in succession during `make demo`.
- **Minimal fix**: In `Makefile:45-51`, either streamline `demo` to depend on `switch-up` and `e2e` directly (since `run_e2e_demo.py --fresh` performs all target/provision/agent setup):
  ```makefile
  demo: ## Full demo: Switch, fresh targets, provision, agents, e2e.
  	+$(MAKE) switch-up
  	+$(MAKE) e2e
  ```
  or run `$(PY) scripts/run_e2e_demo.py` in `make demo` without the redundant `--fresh` flag when preceded by explicit targets/provision/agents setup.

#### Finding 1.5: `targets.sh start --fresh` does not clean `pending_refusals.json` in service or repo state
- **file:line**: `scripts/targets.sh:189`, `agents/isolation.py:1097-1110`, `agents/translator_agent.py:79-84`
- **What happens**: `scripts/targets.sh:189` unlinks `MODERN_LISTS_PATH`, `ROUTER_STATE_FILE`, `divergence_registry.json`, and `.staging`. With isolation on, `agents.isolation reset` (`agents/isolation.py:1109`) unlinks `proxy/router.json`, `fuzzer/divergence_registry.json`, and `module/lists.py`. Neither script resets `translator/pending_refusals.json` (located at `/srv/stranglerroom/state/translator/pending_refusals.json` under isolation, and `.state/translator/pending_refusals.json` unisolated). If a previous run experienced governance refusals before the ledger was sealed, those pending refusals persist across `--fresh` resets into subsequent migrations.
- **Minimal fix**: In `agents/isolation.py:reset`, include `("translator", ("pending_refusals.json",))` in `targets`, and in `scripts/targets.sh:189`, add `"$STATE/translator/pending_refusals.json"` to the `rm -f` list.

---

### 2. Discrepancies Between Documentation and Code

#### Finding 2.1: `docs/ISOLATION.md` states `clean-state` runs `isolation clean` only with isolation on, but code runs it unconditionally
- **file:line**: `docs/ISOLATION.md:166`, `Makefile:63`, `agents/isolation.py:2537-2545`
- **What happens**: `docs/ISOLATION.md:166` states:
  > "`make clean-state` with isolation on also runs `python -m agents.isolation clean`, which empties `SERVICE_HOME/state`..."
  In contrast, `Makefile:63` unconditionally runs `PYTHONPATH=. $(PY) -m agents.isolation clean` regardless of isolation mode. In `agents/isolation.py:2538`, the implementation specifically notes:
  > `# whatever the mode: a service area that exists is emptied (no key copy outlives isolation)`
  The code executes `clean` whenever `/srv/stranglerroom/state` exists and enforces `_require_root("clean")`, directly contradicting the documentation's claim that it runs only when isolation is on.
- **Minimal fix**: Make the documentation and implementation consistent by checking if isolation is on before invoking `isolation clean` in `Makefile:63`, and updating `agents/isolation.py:2537` so `clean` is a no-op if isolation mode resolves to `off`.

#### Finding 2.2: `README.md` claims `make clean-state` wipes `.state/` except `switch.env`, omitting `connection.env` and service state
- **file:line**: `README.md:273, 497`, `Makefile:63-65`, `docs/ISOLATION.md:168`
- **What happens**: `README.md:273` states:
  > "`make clean-state` | stop agents and targets, then wipe `.state/` except `switch.env` (agent credentials included, so the next `make provision` re-registers them)"
  and `README.md:497` states:
  > "`make clean-state` (keeps `.state/switch.env`), then `make demo`"
  Both statements contradict `Makefile:65` and `docs/ISOLATION.md:168`, which explicitly preserve both `switch.env` and `connection.env` (`! -name switch.env ! -name connection.env`). Additionally, `README.md` fails to mention that `clean-state` also clears the service area (`/srv/stranglerroom/state`) when isolation is present.
- **Minimal fix**: Update `README.md` lines 273 and 497 to state that `make clean-state` keeps both `switch.env` and `connection.env`, and empties `/srv/stranglerroom/state` under isolation.

#### Finding 2.3: `docs/ISOLATION.md` claims `isolation_on()` errors if caller is not root, but `isolation_on()` returns `True`
- **file:line**: `docs/ISOLATION.md:99-100`, `agents/isolation.py:400-419`
- **What happens**: `docs/ISOLATION.md:99-100` specifies:
  > "`on` without a ready marker for this checkout, or isolation on while the caller is not root: error. Isolation never silently falls back to off."
  However, `agents/isolation.py:isolation_on()` (lines 400-419) only inspects the marker file and `STRANGLER_ISOLATION` environment variable; it never checks `os.geteuid() == 0`. If a non-root caller invokes `isolation_on()` while a ready marker exists for the repository, `isolation_on()` returns `True` rather than raising an `IsolationError`. The root check is only performed downstream in `main()` for the `mode` CLI command (`agents/isolation.py:2508`) and in `exec_component()` (`agents/isolation.py:1296`).
- **Minimal fix**: In `agents/isolation.py:isolation_on()`, add the root check when isolation mode resolves to `True`:
  ```python
  if on and os.geteuid() != 0:
      raise IsolationError(f"isolation is on ({where}) but caller runs as uid {os.geteuid()}, not root")
  ```

#### Finding 2.4: `README.md` troubleshooting step 492 does not instruct the operator to restart isolated agents
- **file:line**: `README.md:492`, `agents/provision.py:60-74`
- **What happens**: `README.md:492` recommends:
  > `"You're not permitted to direct messages to me in this room." | The addressing policies were not cleared: run make provision (it uses the Gateway admin from .state/switch.env)`
  When isolation is active, `make provision` modifies only the checkout's `.state/`. The running isolated agents read from `/srv/stranglerroom/state/` and do not receive updated addressing state or credentials. If `provision` re-registers agents or rotates keys, the running agents become non-functional (HTTP 401).
- **Minimal fix**: Update `README.md:492` to:
  > "The addressing policies were not cleared: run `make provision`, then `bash scripts/agents.sh restart` (under isolation, restart copies the refreshed credentials into `/srv/stranglerroom/state`)."

---

### 3. CI Isolation Policy

#### Finding 3.1: `.github/workflows/e2e.yml` does not explicitly set `STRANGLER_ISOLATION=off`
- **file:line**: `.github/workflows/e2e.yml:31-63`, `README.md:363-364`, `docs/ISOLATION.md:98`
- **What happens**: Both `README.md:363` and `docs/ISOLATION.md:98` note that CI runs unisolated as a non-root user without a marker. However, `.github/workflows/e2e.yml` does not set `STRANGLER_ISOLATION: off` in the environment of the `e2e` job. The scripts default to `STRANGLER_ISOLATION=auto`. If a host environment, container image, or self-hosted runner has an existing `/etc/stranglerroom/isolation.json` pointing to the checkout directory, `mode` resolves to `on`, and `scripts/targets.sh` / `scripts/agents.sh` immediately abort with exit code 2 because the GitHub Actions runner user (`runner`, uid 1001) is not root.
- **Minimal fix**: In `.github/workflows/e2e.yml`, explicitly set `STRANGLER_ISOLATION: off` at the workflow or job level:
  ```yaml
  jobs:
    e2e:
      env:
        STRANGLER_ISOLATION: off
  ```

---

## Verdict

The per-component OS user isolation implementation (`agents/isolation.py`, `docs/ISOLATION.md`) achieves its core security invariants: system user separation, root-owned mirror trees, descriptor-based access controls, and strict privilege drops. 

However, the operator workflow exhibits discrepancies and edge-case failure modes that must be addressed:
1. **Operator Step Breakages**: `make clean-state` fails for non-root users when isolation is off if `/srv/stranglerroom/state` exists, and risks racing with asynchronous `pkill` calls. `make demo` exhibits redundant target/provisioning restarts where agents are running while room archiving occurs.
2. **State Synchronization**: `agents/provision.py` operates exclusively on `.state/` and does not synchronize with `/srv/stranglerroom/state/`. Running `provision` while agents are active or before `make agents` leaves `/srv/stranglerroom/state/` out of sync, causing `make isolate-verify` or running agents to fail.
3. **Documentation & CI Alignment**: `README.md` and `docs/ISOLATION.md` contain minor text discrepancies regarding what `clean-state` preserves and when `clean` runs. In CI, `STRANGLER_ISOLATION: off` should be explicitly declared in `.github/workflows/e2e.yml` rather than relying on implicit marker absence.

Applying the minimal fixes detailed in Findings 1.1–1.5, 2.1–2.4, and 3.1 will ensure robust operator workflows in both isolated and unisolated environments.
