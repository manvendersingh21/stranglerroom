# Isolation impact audit — contract `isolation-impact-audit-nemotron` (revision 6ade0531…)

**Workspace root:** /root/another_hackathon
**Checkout:** /root/another_hackathon (named in the isolation marker)
**Service area:** /srv/stranglerroom (marker `state: ready`, `service_home: /srv/stranglerroom`, `version: 2`)
**Mode:** isolation ON (`STRANGLER_ISOLATION` unset → `auto`; marker exists, is `ready`, names this checkout) — verified by `python -m agents.isolation mode` semantics of `agents/isolation.py:400-418`.
**Components audited:** translator, fuzzer, cutover-gate, proxy, legacy, modern (one Unix user each: `sr-*`, from `agents/isolation.py:89-103` `COMPONENTS`).

## Method

Read `docs/ISOLATION.md` (§1-§7) and `agents/isolation.py` (revision 2): `COMPONENTS` schema (lines 89-103), `child_env` (lines 463-499), `service_paths` / §2.2 service-area layout (lines 106-177), launcher rules (§4, lines 1287-1332). Traced each component's code (the named module plus everything it imports at runtime) for:

* every file/directory read or written at runtime, with `file:line`;
* every environment variable read at runtime;
* every subprocess started;
* every network address connected to (host resolved by the launcher's `check_ip_url`, `isolation.py:502-514`, which requires IP-literal hosts).

Each item was compared with (a) the component's `COMPONENTS` schema (`agent`, `router_token`, `production_lock`, `module_writer`, `env_keys`) and `child_env`, and (b) the service-area layout (§2.2, `isolation.py:160-162` `ServicePaths.private`, `module`, `lock`, `key`, `room`), then marked:

* **OK** — the item is available to the isolated component exactly as the schema/layout provide (private dir, designated write target such as `state/module` for the translator, read-only root-owned mirror `app/`, launcher-set or schema-listed env var, IP-literal address).
* **BREAKS** — the isolated component would lose something it needs (write path outside its private dir, env var the schema drops, host name the launcher refuses).
* **EXPOSED** — a secret another service user could read.

## Reference: environment schema (`agents/isolation.py`)

`COMPONENTS` (`isolation.py:89-103`):

| component | user | module | agent | router_token | production_lock | module_writer | env_keys |
|---|---|---|---|---|---|---|---|
| translator | sr-translator | agents.translator_agent | yes | no | yes | yes | STRANGLER_AGENT_PACE, LEGACY_URL, MODERN_URL, PROXY_URL, STRANGLER_TRANSLATOR_MODE, STRANGLER_LLM_CMD |
| fuzzer | sr-fuzzer | agents.fuzzer_agent | yes | no | no | no | STRANGLER_AGENT_PACE, LEGACY_URL, MODERN_URL, PROXY_URL |
| cutover-gate | sr-cutover-gate | agents.cutover_agent | yes | yes | yes | no | STRANGLER_AGENT_PACE, LEGACY_URL, MODERN_URL, PROXY_URL |
| proxy | sr-proxy | targets.proxy.router | no | yes | no | no | LEGACY_URL, MODERN_URL |
| legacy | sr-legacy | targets.legacy_openlibrary.server | no | no | no | no | (none) |
| modern | sr-modern | targets.modern_fastapi.server | no | no | no | no | MODERN_NO_RELOAD |

`child_env` (`isolation.py:463-499`) keeps only `_COMMON_KEYS` (`PATH`, `LANG`, `LANGUAGE`, `TZ`, `PYTHONUNBUFFERED`, `STRANGLER_LOG_LEVEL`, `LC_*`) plus the component's `env_keys` from the parent env; then sets last (overriding): `STRANGLER_STATE_DIR=SERVICE_HOME/state`, `PYTHONPATH=SERVICE_HOME/app`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONSAFEPATH=1`, `HOME` (private dir), `USER`/`LOGNAME` (component user), `MODERN_LISTS_PATH=SERVICE_HOME/state/module/lists.py` (translator, modern), `SWITCH_URL` (agents), `ROUTER_ADMIN_TOKEN` (proxy, cutover-gate only); always removes `ROUTER_ADMIN_TOKEN`/`ROUTER_TOKEN_FILE` from others. URL keys are validated as IP literals by `check_ip_url` (`isolation.py:502-514`). `STRANGLER_TRANSLATOR_PASS_ENV` names may not be forbidden/launcher-set/`LD_*`/`PYTHON*`/`STRANGLER_ISOLATION*` (`isolation.py:441-460`).

Service area (`docs/ISOLATION.md` §2.2, `isolation.py:164-162`): `SERVICE_HOME/state/` is root-owned 0755; each component's private dir `state/<name>` is `<user>:<user> 0700` (its HOME); `state/room.json` root 0644 (all agents); `state/agents/<name>.json` `<user> 0600` (API key copies); `state/production.lock` root:sr-prodlock 0660 (translator, cutover-gate); `state/module` sr-translator 0755 with `lists.py` 0644 (translator writes, modern serves). The checkout, `.state/`, and `/root` are unreachable (0700, outside the service area); cwd is `SERVICE_HOME/app` with `PYTHONPATH=SERVICE_HOME/app`.

Observed service area at audit time matches §2.2 exactly (`find /srv/stranglerroom -maxdepth 3`): private dirs `translator/ fuzzer/ cutover-gate/ proxy/ legacy/ modern/` at 0700 owned by their users; `state/module/lists.py` sr-translator 0644; `state/production.lock` root:sr-prodlock 0660; `state/agents/*.json` 0600 per-user; `state/room.json` root 0644; `app/` mirror root-owned 0644/0755.

---

## ## Inventory

### Component: translator

Module `agents.translator_agent`; runtime imports: `agents.llm_synth`, `agents.codegen` (`agents/codegen/fastapi_lists.py`), `agents.legacy_introspect`, `agents.ledger`, `agents.commands`, `agents.locks`, `agents.runtime`, `agents.switch_client` (and via `agents.runtime`: `agents.provision`, `agents.switch_client`).

| # | Item | file:line | Access | Isolated target | Verdict | Note |
|---|---|---|---|---|---|---|
| T-1 | Legacy module source (AST-introspected, never imported/executed) `targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py` | `agents/translator_agent.py:68` (`DEFAULT_LEGACY_MODULE`), read via `agents/legacy_introspect.py:258` (`Path.read_text`) at `agents/translator_agent.py:502` (`introspect_file`); also `agents/translator_agent.py:568` (`read_bytes` digest) | Read | `SERVICE_HOME/app/targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py` — root-owned mirror, 0644 (`ROOT = Path(__file__).parents[1]` resolves to `SERVICE_HOME/app` under the isolated interpreter) | OK | Read-only; mirror is readable by all service users. |
| T-2 | Generated module `targets/modern_fastapi/app/lists.py` (snapshot for rollback) | `agents/translator_agent.py:70` (`DEFAULT_MODERN_LISTS`), overridden by env `MODERN_LISTS_PATH` at `:203`; read at `:418` (`read_bytes` in `_deployment`) | Read | `SERVICE_HOME/state/module/lists.py` (launcher-set `MODERN_LISTS_PATH`) — sr-translator 0644 in `state/module` 0755 owned by sr-translator | OK | Translator owns `state/module` (`module_writer=True`); serves as its `MODERN_LISTS_PATH`. |
| T-3 | Generated module write target `targets/modern_fastapi/app/lists.py` (atomic publish) | `agents/translator_agent.py:397-411` (`_write_atomic`: `mkstemp(dir=target.parent)` at :399, `os.replace(tmp, target)` at :408) | Write | `SERVICE_HOME/state/module/lists.py` — parent `state/module` owned by sr-translator (0755) | OK | The translator's designated write target (`module_writer=True`); this is its job (the one file it may write outside its private dir, per `docs/ISOLATION.md` §1). |
| T-4 | Pending refusals ledger `.state/translator/pending_refusals.json` | `agents/translator_agent.py:79-83` (`default_pending_refusals_path`), `:216` (`STRANGLER_TRANSLATOR_PENDING` override); read `:343` (`read_text` in `_read_pending`); write `:352-360` (`_write_pending`: `mkdir`, `mkstemp`, `os.replace`) | Read + write | `SERVICE_HOME/state/translator/pending_refusals.json` — private dir 0700 owned by sr-translator | OK | Inside the component's private dir. `STRANGLER_TRANSLATOR_PENDING` is not in `env_keys` so it is dropped under isolation; the default private path still works. |
| T-5 | `docs/SPEC.md` excerpt (LLM mode only) | `agents/llm_synth.py:36-38` (`build_prompt` reads `docs/SPEC.md`) | Read | `SERVICE_HOME/app/docs/SPEC.md` — root-owned mirror 0644 | OK | Read-only mirror; only reached in `STRANGLER_TRANSLATOR_MODE=llm`. |
| T-6 | All component code modules (`agents/*.py`, `targets/*.py`) | Python imports resolved via `PYTHONPATH=SERVICE_HOME/app` (`isolation.py:476`) | Read | root-owned `SERVICE_HOME/app/` mirror (0644 files, 0755 dirs) | OK | Read-only; `PYTHONSAFEPATH=1` (`isolation.py:478`) prevents implicit cwd/script-dir entries. |
| T-7 | `STRANGLER_STATE_DIR` | `agents/translator_agent.py:82` | Read (env) | Launcher-set: `SERVICE_HOME/state` (`isolation.py:475`) | OK | Launcher-set key, applied last. |
| T-8 | `STRANGLER_TRANSLATOR_MODE` | `agents/translator_agent.py:205` | Read (env) | In `env_keys` → passed through | OK | Schema-listed (`isolation.py:92`). |
| T-9 | `STRANGLER_TRANSLATOR_PENDING` | `agents/translator_agent.py:216` | Read (env) | NOT in `env_keys` → dropped under isolation | OK | Default private path (T-4) used when absent; nothing needed is lost. |
| T-10 | `STRANGLER_LLM_CMD` | `agents/translator_agent.py:208` (constructor), `agents/llm_synth.py:189` (`os.environ.get`) | Read (env) | Only via `STRANGLER_TRANSLATOR_PASS_ENV` pass-through; schema permits naming it (`isolation.py:441-460`, not forbidden) | OK | Falls back to `DEFAULT_LLM_CMD` (`llm_synth.py:23`); LLM failure falls back to deterministic codegen (`translator_agent.py:489-495`), so the component still runs. |
| T-11 | `MODERN_LISTS_PATH` | `agents/translator_agent.py:203` | Read (env) | Launcher-set: `SERVICE_HOME/state/module/lists.py` (`isolation.py:482`) | OK | Schema-listed via `uses_module` (`isolation.py:82-84`). |
| T-12 | `MODERN_URL`, `LEGACY_URL`, `PROXY_URL` | `agents/runtime.py:35-37` (module-level reads), `agents/translator_agent.py:204` (`MODERN_URL`) | Read (env) | In `env_keys` → passed through; defaults `http://127.0.0.1:9002/9001/9000` | OK | `PROXY_URL` is read by `runtime.py` import but unused by the translator itself. |
| T-13 | `SWITCH_URL` | `agents/switch_client.py:165` (`Settings.load_agent`); defaults `http://127.0.0.1:18000` (`switch_client.py:50`) | Read (env) | Launcher-set for agents (`isolation.py:483-487`); validated as IP literal by `check_ip_url` (`isolation.py:496-498`) | OK | Agents never open `switch.env` (`switch_client.py:158-164`). |
| T-14 | `STRANGLER_AGENT_PACE`, `STRANGLER_LOG_LEVEL` | `agents/runtime.py:77`, `:199` (`main_for`) | Read (env) | In `_COMMON_KEYS`/`env_keys` → passed through | OK | |
| T-15 | Subprocess: LLM command (`STRANGLER_LLM_CMD` or `claude -p …`, `shlex` split, no shell) | `agents/llm_synth.py:189-206` (`subprocess.run`) | Starts | LLM mode only; runs as `sr-translator` with the component's env, cwd `SERVICE_HOME/app`, fds 0-2 only (`isolation.py:140-141`) | OK | Same user, no new privileges (`no_new_privs` set at `isolation.py:141`); cannot reach `/root`, the checkout, or another service's files. |
| T-16 | Network: Switch Agent Bridge/Gateway | `agents/switch_client.py:216` (ops verify), `:383` (ops), `:544` (SSE stream), `:628` (heartbeat) | Connects | `SWITCH_URL` = `http://127.0.0.1:18000` (IP literal) | OK | `check_ip_url` enforced; `localhost` (`::1` risk, `docs/ISOLATION.md` §1) never used. |
| T-17 | Network: modern target `/_meta` polling | `agents/translator_agent.py:455` (`httpx.get(f"{self.modern_url}/_meta")` in `_wait_meta`) | Connects | `MODERN_URL` = `http://127.0.0.1:9002` (IP literal) | OK | `check_ip_url` enforced. |

**Translator verdict: all OK.** Writes are confined to `state/module/lists.py` (its designated write target) and its private dir `state/translator/`; reads are the read-only mirror or its own private dir; every env var read is launcher-set or schema-listed; every address is an IP literal; the only subprocess runs as `sr-translator`.

### Component: fuzzer

Module `agents.fuzzer_agent`; runtime imports: `agents.divergence`, `agents.ledger`, `agents.commands`, `agents.runtime`, `agents.switch_client`.

| # | Item | file:line | Access | Isolated target | Verdict | Note |
|---|---|---|---|---|---|---|
| F-1 | Divergence registry `.state/fuzzer/divergence_registry.json` | `agents/divergence.py:69-73` (`default_registry_path` → `STRANGLER_STATE_DIR/fuzzer/divergence_registry.json`); read `:465` (`Registry._load`, `read_text`) | Read | `SERVICE_HOME/state/fuzzer/divergence_registry.json` — private dir 0700 owned by sr-fuzzer | OK | Inside the component's private dir. |
| F-2 | Divergence registry (atomic rewrite) | `agents/divergence.py:472-481` (`Registry._save`: `mkdir parents`, `mkstemp(dir=self.path.parent)`, `os.replace`) | Write | same private file | OK | Private dir. |
| F-3 | Staging registry (new migration) | `agents/fuzzer_agent.py:291` (`registry.path.with_name(name + ".staging")`); written via `Registry._save`/`:324` (`_write_json_atomic(self.registry.path, …)`) | Write | `SERVICE_HOME/state/fuzzer/divergence_registry.json.staging` — private dir | OK | Private dir; staging removed in `:328` (`registry.reset()` → `unlink`). |
| F-4 | All code modules | imports via `PYTHONPATH=SERVICE_HOME/app` | Read | root-owned `SERVICE_HOME/app/` mirror | OK | Read-only. |
| F-5 | `STRANGLER_STATE_DIR` | `agents/divergence.py:72` | Read (env) | Launcher-set: `SERVICE_HOME/state` | OK | |
| F-6 | `LEGACY_URL`, `MODERN_URL`, `PROXY_URL` | `agents/fuzzer_agent.py:144-145`; `agents/runtime.py:35-37` | Read (env) | In `env_keys` → passed through; defaults `http://127.0.0.1:9001/9002/9000` | OK | `PROXY_URL` unused by the fuzzer itself (only read by the `runtime` import). |
| F-7 | `SWITCH_URL` | `agents/switch_client.py:165` | Read (env) | Launcher-set for agents (`isolation.py:483-487`); IP-literal validated | OK | |
| F-8 | `STRANGLER_AGENT_PACE`, `STRANGLER_LOG_LEVEL` | `agents/runtime.py:77`, `:199` | Read (env) | Passed through | OK | |
| F-9 | Subprocess | — | — | none | OK | The fuzzer starts no subprocess (all HTTP via `httpx` in-process). |
| F-10 | Network: Switch Agent Bridge/Gateway | `agents/switch_client.py` (room documents: ledger `agents/fuzzer_agent.py:400`, matrix `:207`, `:321` upsert) | Connects | `http://127.0.0.1:18000` (IP literal) | OK | |
| F-11 | Network: legacy target (shadow probes + reset) | `agents/divergence.py:242` (`observe`), `:847` (`_reset` POST `/_admin/reset`), `:919` (`run_campaign`) | Connects | `http://127.0.0.1:9001` (IP literal) | OK | |
| F-12 | Network: modern target (shadow probes + reset + `/_meta`) | `agents/divergence.py:242`, `:847`; `agents/fuzzer_agent.py:378` (`_wait_for_rev`) | Connects | `http://127.0.0.1:9002` (IP literal) | OK | |

**Fuzzer verdict: all OK.** All disk access is inside `state/fuzzer/` (private dir); all env vars read are launcher-set or schema-listed; all addresses are IP literals; no subprocesses.

### Component: cutover-gate

Module `agents.cutover_agent`; runtime imports: `agents.bench`, `agents.proxy_control`, `targets.common.router_token`, `agents.ledger`, `agents.commands`, `agents.locks`, `agents.runtime`, `agents.switch_client`.

| # | Item | file:line | Access | Isolated target | Verdict | Note |
|---|---|---|---|---|---|---|
| C-1 | Router token | `targets/common/router_token.py:42` (`resolve_router_token`: `ROUTER_ADMIN_TOKEN` from env first) | Read (env + file fallback) | `ROUTER_ADMIN_TOKEN` is launcher-set for cutover-gate (`isolation.py:488-492`) → returns immediately; **file fallback never reached** under isolation | OK | The fallback (`state/router.token`) does not exist in the service area (§2.2) and `state/` (root 0755) is not writable by `sr-cutover-gate` — but it is only reached when `ROUTER_ADMIN_TOKEN` is absent, which the launcher guarantees for `router_token=True` components. |
| C-2 | Production lock | `agents/locks.py:25-30` (`production_lock_path` → `STRANGLER_PRODUCTION_LOCK` or `STRANGLER_STATE_DIR/production.lock`); `:37` `mkdir(parents=True)`; `:38` `os.open(O_RDWR|O_CREAT)`; `:43` `flock` | Read/write | `SERVICE_HOME/state/production.lock` — root:sr-prodlock 0660; `sr-cutover-gate` is in `sr-prodlock` (`isolation.py:95`, `LOCK_GROUP`) | OK | `state/` exists (root-owned) so `mkdir` is a no-op; lock file is rw for the group. |
| C-3 | All code modules | imports via `PYTHONPATH=SERVICE_HOME/app` | Read | root-owned mirror | OK | Read-only. |
| C-4 | `ROUTER_ADMIN_TOKEN` | `agents/cutover_agent.py:110` (`router_token or resolve_router_token()`) | Read (env) | Launcher-set (`isolation.py:488-492`); `child_env` sets it for `router_token=True` only | OK | Schema-listed (`isolation.py:95`). |
| C-5 | `STRANGLER_STATE_DIR` | `targets/common/router_token.py:35-36` (fallback path); `agents/locks.py:29` (lock fallback) | Read (env) | Launcher-set: `SERVICE_HOME/state` | OK | Not used at runtime when `ROUTER_ADMIN_TOKEN` is set. |
| C-6 | `LEGACY_URL`, `MODERN_URL`, `PROXY_URL` | `agents/runtime.py:35-37`; `agents/cutover_agent.py:106-108` | Read (env) | In `env_keys` → passed through | OK | |
| C-7 | `STRANGLER_AGENT_PACE`, `STRANGLER_LOG_LEVEL` | `agents/runtime.py:77`, `:199` | Read (env) | Passed through | OK | |
| C-8 | Subprocess | — | — | none | OK | |
| C-9 | Network: Switch Agent Bridge/Gateway | `agents/switch_client.py` (scorecard/matrix/ledger documents; `cutover_agent.py:588-596` `_approved_run`) | Connects | `http://127.0.0.1:18000` | OK | |
| C-10 | Network: legacy target (benchmark) | `agents/bench.py:160` (`run_benchmark` httpx clients); `agents/cutover_agent.py:300`, `:630` (`_identical_paths`) | Connects | `http://127.0.0.1:9001` | OK | |
| C-11 | Network: modern target (benchmark + `/_meta`) | `agents/bench.py:160`; `agents/cutover_agent.py:303`, `:616` (`_require_modern_rev`) | Connects | `http://127.0.0.1:9002` | OK | |
| C-12 | Network: strangler proxy (status/flip/verify) | `agents/proxy_control.py:44` (`status`), `:87` (`flip`), `:130` (`verify_routing`); `agents/cutover_agent.py:381` (`proxy_control.flip`) | Connects | `http://127.0.0.1:9000` (`PROXY_URL`) | OK | IP-literal validated by launcher. Token sent only in `X-Router-Token` header (`proxy_control.py:88`), redacted in errors (`:90-93`). |

**Cutover-gate verdict: all OK.** No disk writes at runtime (all state lives in Switch room documents over the network); the production lock is shared per design (`production_lock=True`, `sr-prodlock`); `ROUTER_ADMIN_TOKEN` is delivered via env only and held by design for this component.

### Component: proxy

Module `targets.proxy.router`; runtime imports: `targets.common.router_token`, `httpx`, `uvicorn`.

| # | Item | file:line | Access | Isolated target | Verdict | Note |
|---|---|---|---|---|---|---|
| P-1 | Router state file `.state/proxy/router.json` | `targets/proxy/router.py:50-55` (`default_state_file` → `STRANGLER_STATE_DIR/proxy/router.json`); read `StateStore.load` `:179` (`read_text`); `:177` `mkdir(parents=True, exist_ok=True)` on parent | Read | `SERVICE_HOME/state/proxy/router.json` — private dir 0700 owned by sr-proxy | OK | `state/proxy/` exists (root hands it over at prepare); `mkdir` is a no-op. |
| P-2 | Router state file (atomic rewrite on flip) | `targets/proxy/router.py:205-220` (`StateStore.save`: `mkdir`, `mkstemp(dir=self.path.parent)` at :208, `os.replace` at :220) | Write | `SERVICE_HOME/state/proxy/router.json` — private dir | OK | Private dir; atomic same-directory rename. |
| P-3 | Admin token | `targets/proxy/router.py:156` (`resolve_router_token(values)` in `RouterConfig.from_env`); `targets/common/router_token.py:42-45` | Read (env + file fallback) | `ROUTER_ADMIN_TOKEN` launcher-set for proxy → returns immediately; file fallback (`state/router.token`) never reached under isolation (and not writable by `sr-proxy` in root-owned `state/`) | OK | `ROUTER_TOKEN_FILE` is popped by `child_env` for every component (`isolation.py:495`) and is only the fallback when `ROUTER_ADMIN_TOKEN` is absent. |
| P-4 | `LEGACY_URL`, `MODERN_URL` | `targets/proxy/router.py:152-153` (`RouterConfig.from_env`); defaults `http://127.0.0.1:9001/9002` | Read (env) | In `env_keys` → passed through; IP-literal validated | OK | |
| P-5 | `ROUTER_STATE_FILE` | `targets/proxy/router.py:154` | Read (env) | Passed through (in `env_keys`) | OK | |
| P-6 | Subprocess | — | — | none | OK | `uvicorn.run(get_app(), …)` (`targets/proxy/router.py:589`) runs the ASGI server in-process; no child processes are started. |
| P-7 | Network: upstream connections (forwarded requests) | `targets/proxy/router.py:510-523` (`client.send`, `UPSTREAM_TIMEOUT`); per request to the selected upstream | Connects | `LEGACY_URL`/`MODERN_URL` = `http://127.0.0.1:9001` / `:9002` (IP literals) | OK | `trust_env=False` (`router.py:358-362`); credential cookies stripped before forwarding (`router.py:244-270`). |
| P-8 | Network: listening socket | `targets/proxy/router.py:589` (`uvicorn.run host 127.0.0.1 port 9000`) | Binds/listens | `127.0.0.1:9000` | OK | Listening is out of isolation scope per `docs/ISOLATION.md` §1 ("a service binding a port that happens to be free"). |

**Proxy verdict: all OK.** State file confined to its private dir; token via env only; upstreams are IP literals; no subprocesses.

### Component: legacy

Module `targets.legacy_openlibrary.server`; runtime imports: `targets.legacy_openlibrary.infogami_shim`, `targets.common.dataset`, `cheroot`.

| # | Item | file:line | Access | Isolated target | Verdict | Note |
|---|---|---|---|---|---|---|
| L-1 | Legacy plugin module (imported to register pages) | `targets/legacy_openlibrary/infogami_shim.py:41` (`PLUGINS`), `:105` (`importlib.import_module(plugin)` in `build_app`) | Read (code) | `SERVICE_HOME/app/targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py` — root-owned mirror | OK | Read-only code; infogami `delegate.page` registration, never written by this component. |
| L-2 | Error log `.state/legacy/legacy_openlibrary.log` | `targets/legacy_openlibrary/server.py:34-38` (`default_log_file` → `STRANGLER_STATE_DIR/legacy/legacy_openlibrary.log`); `:80-83` (`configure_logging`: `path.parent.mkdir(parents=True, exist_ok=True)`, `FileHandler`) | Write | `SERVICE_HOME/state/legacy/legacy_openlibrary.log` — private dir 0700 owned by sr-legacy | OK | `state/legacy/` exists; `wsgi.errors` tracebacks (incl. D-5 crash) go here instead of the launcher's stderr pipe. |
| L-3 | All code modules | imports via `PYTHONPATH=SERVICE_HOME/app` | Read | root-owned mirror | OK | Read-only. |
| L-4 | `STRANGLER_STATE_DIR` | `targets/legacy_openlibrary/server.py:37` | Read (env) | Launcher-set: `SERVICE_HOME/state` | OK | |
| L-5 | Subprocess | — | — | none | OK | |
| L-6 | Network: listening socket | `targets/legacy_openlibrary/server.py:99` (`cheroot wsgi.Server`) | Binds/listens | `127.0.0.1:9001` (CLI `--host`, default) | OK | No outbound connections; out of isolation scope (§1). |

**Legacy verdict: all OK.** The only runtime write is its log in its private dir; no outbound network.

### Component: modern

Module `targets.modern_fastapi.server`; runtime imports: `targets.modern_fastapi.app.main`, `targets.modern_fastapi.app.store`, `targets.common.dataset`, `uvicorn`, plus the generated `app/lists.py`.

| # | Item | file:line | Access | Isolated target | Verdict | Note |
|---|---|---|---|---|---|---|
| M-1 | Generated module `lists.py` (served) | `targets/modern_fastapi/app/main.py:31` (`LISTS_PATH` = `MODERN_LISTS_PATH` env or `APP_DIR/lists.py`); `:49` (`read_bytes`); `:58` (`exec(compile(source, …))`); `:37-57` (`_load_lists_module` at import, line 67) | Read | `SERVICE_HOME/state/module/lists.py` (launcher-set `MODERN_LISTS_PATH`) — sr-translator 0644 in `state/module` 0755 (traversable/readable by `sr-modern`) | OK | `module/` is owned by the translator but mode 0755; `lists.py` 0644 — readable by all service users by design (it is the public served module). |
| M-2 | Working directory / reloader watch root | `targets/modern_fastapi/server.py:35-46` (`watch_dir` = `MODERN_LISTS_PATH` parent when overridden; `os.chdir(watch_dir)` at :46); uvicorn WatchFiles reloader watches `watch_dir` + cwd | Read (traverse/list) | `SERVICE_HOME/state/module` — 0755 sr-translator, others may traverse+list | OK | `chdir` needs only execute permission; the reloader only reads directories. |
| M-3 | All code modules | imports via `PYTHONPATH=SERVICE_HOME/app` (`sys.path.insert(0, str(ROOT))` at `:44` is a no-op when already present) | Read | root-owned mirror | OK | Read-only. |
| M-4 | `MODERN_LISTS_PATH` | `targets/modern_fastapi/app/main.py:31` | Read (env) | Launcher-set: `SERVICE_HOME/state/module/lists.py` (`isolation.py:482`) | OK | `uses_module=True` for modern (`isolation.py:82-84`). |
| M-5 | `MODERN_NO_RELOAD` | `targets/modern_fastapi/server.py:33` | Read (env) | In `env_keys` → passed through from parent env **only** (not force-set by `child_env`; `targets.sh` does not export it) | OK | Unset → `reload=True`, which is the intended hot-deploy behaviour (translator writes `lists.py` → modern reloads); `MODERN_NO_RELOAD=1` is the test-only setting per `server.py` docstring. Not a break: the reloader is read-only and runs as `sr-modern`. |
| M-6 | Subprocess | — | — | none | OK | uvicorn in-process; reload restarts in-process as the same user. |
| M-7 | Network: listening socket | `targets/modern_fastapi/server.py:47` (`uvicorn.run`) | Binds/listens | `127.0.0.1:9002` | OK | No outbound connections; out of isolation scope (§1). |

**Modern verdict: all OK.** It only reads the translator-owned module (readable by design) and listens; it writes nothing.

---

## ## Problems

**None.**

Every inventoried item is OK:

* **Writes:** translator writes only `state/module/lists.py` (its designated module-write target, owned by `sr-translator`) and `state/translator/pending_refusals.json` (private dir); fuzzer writes only `state/fuzzer/` (registry + staging); proxy writes only `state/proxy/router.json` (private dir); legacy writes only `state/legacy/legacy_openlibrary.log` (private dir); cutover-gate and modern write nothing on disk (state via Switch documents / in-memory store). No component writes outside its private dir or designated target.
* **Env:** every variable read at runtime is either launcher-set (`STRANGLER_STATE_DIR`, `PYTHONPATH`, `HOME`, `USER`/`LOGNAME`, `MODERN_LISTS_PATH` for translator/modern, `ROUTER_ADMIN_TOKEN` for proxy/cutover-gate) or in the component's `env_keys` (`isolation.py:89-103`). Dropped names (`STRANGLER_TRANSLATOR_PENDING` for the translator; `MODERN_NO_RELOAD` force-set for modern) fall back to correct private paths or intended behaviour (`ROUTER_TOKEN_FILE` is never reached because `ROUTER_ADMIN_TOKEN` is always set for token components). No `switch.env`/Switch secret is read by any agent (`switch_client.py:158-164`).
* **Network:** every address is an IP literal (`127.0.0.1:18000 Switch`, `127.0.0.1:9000/9001/9002 targets`), validated by `check_ip_url` (`isolation.py:502-514`); no `localhost`/`::1` exposure (`docs/ISOLATION.md` §1).
* **Subprocesses:** only the translator's LLM command (`llm_synth.py:199`), run as `sr-translator` with `no_new_privs`, closed inherited fds, and no access to `/root`, the checkout, or other services' files.
* **Exposure:** no secret is written where another service user can read it — API key copies are `state/agents/<name>.json` at `<user> 0600` (owner-only); `state/room.json` holds no secrets; `state/module/lists.py` is generated code, not a secret; the router token travels in `ROUTER_ADMIN_TOKEN` env for proxy/cutover-gate only, as the schema allows.

No minimal fixes are required; the isolation schema in `agents/isolation.py` (revision 2) matches the runtime behaviour of all six components under the observed service-area layout (`/srv/stranglerroom`, marker `ready`).
