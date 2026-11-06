# Review — isolation integration of the launch scripts

Scope of the review: `docs/ISOLATION.md` §4 "Scripts"; `scripts/agents.sh`; `scripts/targets.sh`; the launch half of `tests/test_targets_sh.py`.

Checklist under test:

1. the mode is decided before any file is touched;
2. a component is accepted as running only when it really runs isolated (isolated_pid, isolated_listener);
3. a launch that does not come up isolated is killed with its children;
4. stop never signals a process that is not ours except the service users' strays;
5. `--fresh` resets every file the processes actually use;
6. quoting, `set -euo pipefail` pitfalls (unset variables, failing command substitutions, pipelines), pid reuse, races between concurrent invocations (flock), and that with isolation off the behaviour is exactly the old one.

## Findings

### 1. Medium — `status` accepts a component as running without the isolation checks

**Severity:** Medium.

**Location:** `scripts/agents.sh:203-211` (`status_one`), `scripts/agents.sh:238-242` (dispatch), `scripts/targets.sh:198-202` (`status`).

**Checklist item:** "a component is accepted as running only when it really runs isolated (isolated_pid, isolated_listener)".

**Scenario:** Isolation is on (`marker` `ready`). A component runs unisolated — e.g. it was started before isolation was enabled and survived, or was started by hand (`python -m agents.translator_agent`). The launch paths refuse exactly this state and fail loud:

* `scripts/agents.sh start_one` → `scripts/agents.sh:125-129`: "already running, but not as sr-<name> — run 'scripts/agents.sh restart'";
* `scripts/targets.sh svc` → `scripts/targets.sh:87-95`: "answers, but not as a tracked … running as sr-<name>".

but the read paths never apply the isolation acceptance:

* `scripts/agents.sh status_one` resolves the pid through `agent_pid` → `is_agent_pid` (module argv check only, `scripts/agents.sh:79-89`) and prints `running pid=N`; the `isolated_pid` check (`scripts/agents.sh:95-104`) is only used in `start_one` (`scripts/agents.sh:125`) and in the start wait/kill path (`scripts/agents.sh:154-168`), never in the status path;
* `scripts/targets.sh status` (`scripts/targets.sh:198-202`) is a plain `curl` port probe — no tracked pid, no uid, no `isolated_listener`.

**Concrete impact:** with isolation on, an untracked or unisolated component is reported as the component running/up (`agents.sh status` → `translator running pid=N`; `targets.sh status` → `9001 {"status": "ok", …}` as `up`). The launch paths refuse it, so enforcement and reporting disagree; anything reading `status` to decide "the isolated stack is healthy" gets a false positive.

**Minimal fix:** have `status_one` run the same tracked + isolated validation as `start_one` when isolation is on (falling back to the module check only when isolation is off), and have the targets `status` branch, when isolation is on, validate each answering port against the tracked isolated process (the same `is_target_pid` + `isolated_pid` + `isolated_listener` chain `svc` uses) before printing `up`.

## Verified clean (no defect found in these areas)

* **Mode before any file is touched:** `scripts/agents.sh:39-47` (mode resolution, then `umask 077`) precedes `lock`/`mkdir`; `scripts/targets.sh:29-38` precedes `mkdir -p "$RUN"` and the lock. Misconfigured mode → exit 2 before the run dir is created (`tests/test_isolation.py::test_scripts_stop_before_touching_the_run_dir_when_the_mode_is_misconfigured`).
* **Launch that does not come up isolated is killed with its children:** `scripts/agents.sh:160-168` and `scripts/targets.sh:121-126` (KILL the unisolated child and its children, drop the pid file, fail loud). Live-probed with the real launcher: a refused module made the launch fail and the script exit 1 with the pid file cleaned.
* **Stop never signals a process that is not ours except the service users' strays:** `scripts/agents.sh:179-201` (TERM/KILL the verified tracked pid, then `pkill -KILL -u "sr-$name"`) and `scripts/targets.sh:137-156` plus the stray branch (`scripts/targets.sh:153-154`).
* **`--fresh` resets every file the processes actually use:** `scripts/targets.sh:185-191` — `stop`, `ports_free 9000 9001 9002`, rm of `${MODERN_LISTS_PATH:-…}`, `${ROUTER_STATE_FILE:-…}`, registry (+`.staging`), then `python -m agents.isolation reset` when isolated.
* **Quoting / `set -euo pipefail` / pid reuse / flock / isolation-off parity:** the `${ISOLATE:+"$PY" -m agents.isolation exec "$name" --}` prefix keeps quoted values with spaces as one word (`tests/test_isolation.py::test_isolate_prefix_keeps_a_quoted_value_with_spaces_as_one_word`); stale/garbage/reused pids are refused (`tests/test_targets_sh.py::test_stop_one_never_signals_a_stale_or_foreign_pid`); `flock -w 60` on fd 9 with `9>&-`; with isolation off the launch lines are byte-identical to the pre-isolation ones.

Regression suites: `tests/test_targets_sh.py` → **9 passed**; the isolation/script subset of `tests/test_isolation.py` → **52 passed**; `tests/test_ops_scripts.py` → **3 passed**.

## Verdict

**Verdict: accept, with finding 1 (Medium) to be fixed before this isolation integration is settled.** The launch half of the scripts is sound — the mode is read before anything is touched, a component counts as running only through the isolated exec checks, a misfired launch is killed with its children, stops signal only tracked pids plus the service users' strays, and `--fresh` resets the paths the processes use; the unisolated behaviour is unchanged. The remaining defect is that `status` does not apply the isolated-runtime acceptance checks, so an untracked or unisolated component can be reported as the running component.

## Deliverable

`.hacp/reviews/isolation-scripts.md`
