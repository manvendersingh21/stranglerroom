# StranglerRoom — Agents & Room Contract (AGENTS_SPEC)

Binding contract for the three Switch agents, the demo driver, ops scripts, template and CI.
Companions: `docs/SPEC.md` (targets + divergence engine) and `docs/SWITCH_PROTOCOL.md` (Switch 0.31.0 API, file:line
verified). Where this file and SWITCH_PROTOCOL.md disagree about Switch, SWITCH_PROTOCOL.md wins.

Everything below marked **(live-verified)** was exercised against the running Switch 0.31.0 + Mattermost stack.

## 0. Ownership & swarm rules

* **Shared framework — read-only for every builder** (the lead owns them; if you need a change, report it):
  `agents/switch_client.py`, `agents/provision.py`, `agents/runtime.py`, `agents/commands.py`, `agents/ledger.py`,
  `tests/fakes.py`, `tests/procs.py`, `docs/*.md`, plus the finished targets/engines: `targets/**`,
  `agents/codegen/**`, `agents/divergence.py`, `agents/bench.py`.
* Each builder writes ONLY the files its task lists. Never edit another task's files.
* Never register/re-register Switch agents, create rooms, or post in `strangler-migration` from a unit test. Live
  Switch use is reserved for tests marked `@pytest.mark.integration` and for `scripts/run_e2e_demo.py`.
* Peers via HACP: `tools/hacp_delegate.py <contract.json>` (see its docstring). Peers work in an isolated workspace;
  only declared outputs that pass the acceptance checks are merged back. Models: codex `gpt-6.1-sol` (text mode — it
  cannot run commands; list every file it needs in `context`), agy `claude-opus-4-6-thinking` or
  `gemini-3.8-flash-high`, opencode `opencode/big-pickle`.

## 1. Framework (implemented)

### `agents/switch_client.py`
`Settings.load()` (env, then `.state/switch.env`): `switch_url, registration_token, admin_email, admin_password,
mattermost_url, mattermost_team, mattermost_user, mattermost_user_password` — for provisioning, template validation and
the demo driver. `Settings.load_agent()` is what a room agent uses: only `switch_url` (env `SWITCH_URL`, else
`switch.env` if readable — an isolated agent cannot read it, see docs/ISOLATION.md — else `http://localhost:18000`);
the other fields are empty strings, and no agent path reads them.
`register_agent(settings, name, description, display_name) -> AgentCredentials` (reuse stored creds if they still
authenticate, else `POST /agents` with `overwrite=true`; creds in `.state/agents/<name>.json`, mode 600, written
via a same-directory `mkstemp` file (0600 from creation) + fsync + `os.replace`).
`SwitchGateway(settings)`: `agents()`, `set_addressing_policy(agent_id, None)`, `room(room_id)`,
`room_documents(room_id)`, `room_document(room_id, doc_id)` (content included), `validate_template(text)`, `close()`.
`SwitchAgent(settings, creds)`: `connect([room_id])` (ONE stream `scope=single&filter=addressed&rooms=<room>`,
reader thread + 2 s heartbeat thread, self-healing reconnect, dedupe by sequence; `409 taken_over` terminal),
`next_event(timeout) -> Event|None`, `op(name, args, bound=False)`, `post(body)`, `send_targeted(body, [targets])`
(Switch prefixes `@target`; returns `target_statuses` for agents), `participants()`, `documents()`,
`create_document(...)`, `update_document(id, content=…)`, `load_documents([ids])`, `upsert_document(name=…, …)`,
`set_typing(room_id, bool)`, `read_context(room_id, limit, before=None)` (pages back with `before`), `close()`.
`Event`: `.name, .data, .sequence, .payload, .room_id, .is_message, .addressed, .body, .sender_name, .message_id`.

### `agents/provision.py` — `python -m agents.provision [--fresh-room]` (live-verified)
Registers/reuses `translator`, `fuzzer`, `cutover-gate`; as Gateway admin clears each addressing policy
(`{"policy": null}` — bootstrap-registered agents are owned by the non-admin "Agent Bootstrap" user, so without it the
human is refused); creates/reuses room `strangler-migration` with all three agents + Mattermost `user`
(`--fresh-room` archives the old one first); writes `.state/room.json` (`RoomInfo`: `room_id, name, channel_id,
channel_url, agents{name: id}, human`).

### `agents/runtime.py` — `RoomAgent`
Subclass, set `name`, implement `handle(event, sender)`. Provides `classify(name) -> Sender(name, kind)` where kind is
`human` iff the participant's `type == "user"` AND its name is the room's lead `RoomInfo.human` (any other `user` is a
`guest`; a name shared by two participants after case folding is `unknown`), `agent` iff `type == "agent"`
(**live-verified: `sender_kind` is null for both — never use it**); `say(body)`; `handoff(target, body)` (send_targeted; warns if the target agent is not live);
`ask_human(body)`; `working()` context manager (typing indicator); `dispatch(event)` (ignores unaddressed/foreign-room/
own events, catches every handler exception and posts `⚠️ …`); `run_forever()`; `main_for(cls)` entry point.
`STRANGLER_AGENT_PACE` (seconds, default 0) delays each post — `--cinema` uses it. Constants `LEGACY_URL`, `MODERN_URL`,
`PROXY_URL` (env-overridable). The proxy admin token comes from `targets/common/router_token.py` — one rule for the proxy and the gate: `ROUTER_ADMIN_TOKEN` (trimmed), else the per-deployment random token in `ROUTER_TOKEN_FILE` or `<STRANGLER_STATE_DIR>/router.token` (refused unless it is a regular, non-symlinked file owned by this user with mode 600 — use `ROUTER_ADMIN_TOKEN` for mounted secrets).

### `agents/commands.py` — the grammar and the markers (single source of truth)
Parsers `parse_translator/parse_fuzzer/parse_cutover(body) -> Command|None` (raise `CommandError` on malformed input),
`parse_decisions`, `format_decisions`, builders `shadow_test(rev, mig)`, `re_verify(rev, mig, decisions)`,
`benchmark(rev, mig)`, `record_cutover(rev, mig)`, `MIGRATE_EXAMPLE`, `APPROVE_EXAMPLE`, and `MARK` — the stable message
prefixes each agent MUST start the corresponding message with (the e2e driver matches on them).

### `agents/ledger.py` — three single-writer room documents (live-verified creator-only rule)
| Document | Writer | State helpers | Renderer |
|---|---|---|---|
| `Parity & Cutover Audit Ledger` (`LEDGER_DOC`) | translator | `new_ledger(...)`, `append_entry(trail, …)`, `verify_chain` | `render_ledger` |
| `Parity Audit Matrix` (`MATRIX_DOC`) | fuzzer | `new_matrix()`, `add_run(...)`, `latest_run` | `render_matrix` |
| `Cutover Scorecard` (`SCORECARD_DOC`) | cutover-gate | `new_scorecard()`, `latest_benchmark` | `render_scorecard` |

Each document = Markdown + a fenced `json stranglerroom-state` block (`embed_state` / `extract_state`). `DOC_META[name]`
gives `(description, instructions)` for `upsert_document`. Cross-agent reads go through `documents()` +
`load_documents()` (any room member may read; only the creator may write). `digest(state)` = sha256 evidence of a
mirrored document. Ledger statuses: `scaffolded → awaiting-decision → patching → parity-approved → cut-over → sealed`.

## 2. Agents (`python -m agents.<module>`, each a `RoomAgent`)

**Mention hygiene (mandatory):** an `@name` addresses that participant. Agents write `@<agent>` ONLY inside a deliberate
`handoff()`. Instructions to the human name agents without `@` (e.g. "reply to the translator with
`preserve D-1 … and deprecate D-3 …`"). Agents may address the human via `ask_human()`.
**Governance:** commands marked *human-only* from any sender other than the human lead → post
`MARK["governance_refusal"]` + reason, record the refusal (translator: in the ledger; others: in their doc), change
nothing. Stale/replayed human commands never act (`ℹ️`, nothing changed): a `migrate`/decision message sent before the
current migration's request (Switch timestamps), an `approve-cutover` sent before the latest benchmark finished or the
router's latest flip, a `rollback` sent before the router's latest flip.

**Production lock:** @translator holds `agents.locks.production_lock` for a whole `migrate` / decision (cutover check → write → reload → record build) and @cutover-gate for a whole `approve-cutover` / `rollback` (rev check → flip → verify → record); on a timeout each says so and changes nothing.

### `@translator` — `agents/translator_agent.py` (+ `agents/legacy_introspect.py`)
* Legacy module: `targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py`. Modern output:
  `targets/modern_fastapi/app/lists.py` (both overridable via constructor args for tests), published atomically with
  mode 0644 whatever the umask (the modern server may run as another OS user).
* Governance refusals seen while no ledger exists persist in `STRANGLER_TRANSLATOR_PENDING`, default
  `<STRANGLER_STATE_DIR>/translator/pending_refusals.json`, and are carried into the next ledger.
* `migrate <path> from <source> to <target>` — *human-only*. Validate source/target (case-insensitive
  `legacy_openlibrary` → `fastapi`), AST-introspect the legacy module (no import/exec), the path must match one of its
  route templates (else reply with the route table). New migration id `MIG-<UTC yyyymmdd-HHMMSS>`. Post
  `🧭 Migration <id> accepted from @<human> …` + `render_route_table`. Render v1 (`render_lists_module({})`, or LLM mode),
  write atomically, wait ≤ 30 s until `MODERN_URL/_meta` reports `rev == module_rev(src)` and no `import_error`, cross-check
  that every legacy route has a FastAPI route in `/_meta.routes`. Create the ledger (`new_ledger`, routes, build, trail:
  `migration-requested` (human), `routes-extracted`, `build-deployed`) and upsert it. Post `🛠️ v1 deployed to :9002 — rev
  `<rev>` …`, then `handoff("fuzzer", shadow_test(rev, mig))`.
* `preserve … deprecate …` — *human-only*, needs an active (unsealed) ledger. Read the fuzzer's matrix (latest run):
  every id must be a known cluster (else list the known ids). Unclassified clusters cannot be `preserve`d by deterministic
  codegen (say so). Policy = `{tag: decision}` merged over the previous build's policy. Record each decision
  (`decision, tag, by, message_id, at`), mirror the matrix's clusters into `divergences`, trail entry per decision
  (human, with matrix evidence digest), status `patching`. Render + write + wait reload (as above), add build, upsert
  ledger. Post `🔧 Applied human decisions from @<human>: …` explaining each preserve/deprecate in one line and
  `v2 deployed … rev `<rev>``, then `handoff("fuzzer", re_verify(rev, mig, decisions))`.
* `record-cutover rev=<rev> migration=<id>` — only from the agent `cutover-gate`. Read matrix (latest run must be approved
  for `rev`) and scorecard (benchmark for `rev`, cutover with `proxy_target == "modern"`, approver must be a human
  participant). Mirror `parity` and `benchmark` (+ evidence digests), set `cutover`, trail entries `parity-verified`
  (fuzzer), `benchmark-recorded` (cutover-gate), `cutover-approved` (human, approval message id), `proxy-flipped`
  (cutover-gate), then `ledger-sealed` (translator) and `sealed = {at, head_hash, entries}`, status `sealed`; upsert.
  Never seal unless the scorecard's cutover names an approval message, its flip is `verified`, and `read_context` shows
  that message as the lead's own `approve-cutover` posted after the build being cut over (`approval_message_verified`
  is therefore always `true` in a sealed ledger); otherwise `⚠️ cannot seal …`, nothing recorded. Post
  `🧾 Ledger sealed — Migration <id> complete …` (decisions, parity label, p99 legacy→modern, approver, head hash) via
  `ask_human`.
* `status` / `help` — anyone.
* LLM mode: `STRANGLER_TRANSLATOR_MODE=llm` runs `STRANGLER_LLM_CMD` (default
  `claude -p --model claude-sonnet-5-5 --output-format text`) with the legacy source, SPEC §2–§3 and the policy on stdin;
  extract the python module; accept only if it `ast.parse`s, defines `router`, compiles, and after hot reload `/_meta`
  shows no `import_error` and the expected routes; otherwise fall back to deterministic codegen and SAY SO in the room
  (`⚠️ LLM synthesis rejected: <reason> — used deterministic codegen`). Tests use a fake `STRANGLER_LLM_CMD`.

### `@fuzzer` — `agents/fuzzer_agent.py`
* On connect: rehydrate the matrix from `MATRIX_DOC` if present.
* `shadow-test` / `re-verify` (from the translator agent or a human): wait ≤ 30 s for `MODERN_URL/_meta rev == rev`
  (else `⚠️`); `rev` must be the ledger's LATEST build (an older one could never be sealed). New `migration` id →
  fresh matrix and `Registry().reset()` (default registry file `<STRANGLER_STATE_DIR>/fuzzer/divergence_registry.json`). Decisions come from the translator's ledger
  state (`decisions`); if the handoff carries decisions they must equal the ledger's (else `⚠️` and stop). Run
  `run_campaign(LEGACY_URL, MODERN_URL, {id: {"decision": d}}, n_random=120, registry=Registry())` inside
  `working()`, `add_run`, upsert `MATRIX_DOC`. Post `🔬 Shadow campaign #<n> on rev `<rev>`: <N> requests …` +
  `report.to_markdown()`.
  * not approved → `ask_human("decision needed on D-…: preserve = …; deprecate = …. Reply to the translator with e.g.
    `preserve D-1 D-2 D-4 and deprecate D-3 D-5`.")` (no `@translator`!).
  * approved → post `✅ 100% Approved Parity (k/k) on rev `<rev>` …` then `handoff("cutover-gate", benchmark(rev, mig))`.
* `status`/`help`; anything else from an agent → ignore.

### `@cutover-gate` — `agents/cutover_agent.py`
* On connect: rehydrate the scorecard.
* `benchmark rev=… migration=…` (from the fuzzer agent or a human): preconditions — matrix latest run approved for `rev`
  and `MODERN_URL/_meta rev == rev`; else explain. Benchmark legacy then modern **sequentially** with
  `agents.bench.run_benchmark(url, PATHS, total=500, concurrency=50)` where `PATHS` = GET contract endpoints that are
  identical on both after the human's decisions (lists, lists.json?details=1, seeds, editions, quote?quantity=3).
  Verdict line (faster/slower, error counts). Record in scorecard, upsert. Post `📊 Benchmark — rev `<rev>`` +
  `render_card(legacy, modern)` + verdict, then `ask_human("approval needed: reply to the cutover gate with
  `approve-cutover` to flip :9000 from legacy :9001 to modern :9002 (or `rollback` later).")`.
* `approve-cutover` — *human-only*. Preconditions: benchmark exists for the current modern rev and the matrix is approved
  for it. `POST PROXY_URL/_router/flip` (`X-Router-Token`) `{target: "modern", approved_by: <human>, evidence: <parity
  label, rev, p99s, approval message id>}`; verify `GET PROXY_URL/people/george08/lists.json` has
  `X-Strangler-Upstream: modern` and equals the modern response, and `/_router/status` target is modern. Record cutover in
  the scorecard, upsert. Post `🚦 Cutover complete: :9000 now serves the modern FastAPI service (:9002) — approved by
  @<human> (message `<approval message id>`) …` (the e2e driver requires the id), then
  `handoff("translator", record_cutover(rev, mig))`.
* `rollback` — *human-only*: flip back to legacy, record, post `↩️ Rollback complete …`.
* Replays: an `approve-cutover`/`rollback` whose message id was already handled (redelivered event) never moves
  the proxy again (`ℹ️ … already handled`). A failed cutover whose automatic revert also fails records
  `cutover.proxy_target: "unknown"`; the translator treats any cutover not on `legacy` as active (no new builds)
  until a human `rollback`/`approve-cutover` settles it.
* `status`/`help`.

## 3. Demo driver — `scripts/run_e2e_demo.py [--cinema] [--fresh] [--step-timeout 240] [--no-start]`
Logs into Mattermost REST as `user` (token from the `Token` response header), resolves the channel from
`.state/room.json` (`channel_id`), and plays the 8 steps by posting as `user` and waiting for bot posts (bot username ==
agent name; only posts created after the step started; skip `📄` document notices) whose text starts with/contains the
`MARK` prefixes:
1. post `@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI`
2. wait translator `🧭 Migration` and `🛠️ v1 deployed` and its `@fuzzer shadow-test rev=` handoff
3. wait fuzzer `🔬 Shadow campaign` listing exactly D-1..D-5 and the `@user … decision needed` request
4. post `@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5`
5. wait translator `🔧 Applied human decisions` and `@fuzzer re-verify rev=`
6. wait fuzzer `✅ 100% Approved Parity (5/5)` and `@cutover-gate benchmark rev=`; then a **governance probe**: post
   `@cutover-gate approve-cutover` *as the fuzzer agent* via `POST /agents/{id}/message` (simulated compromised agent,
   clearly labelled in the driver output) and wait for `🛑 Governance gate`; assert the proxy still targets legacy.
7. wait cutover-gate `📊 Benchmark` (p99) and `@user … approval needed`; post `@cutover-gate approve-cutover`
8. wait `🚦 Cutover complete` and translator `🧾 Ledger sealed`; assert `/_router/status` target `modern`,
   `X-Strangler-Upstream: modern` on a proxied request, and — reading the ledger independently through the Gateway as
   admin — 5 decisions by `user`, approved parity, benchmark p99s, cutover approved by `user` with
   `approval_message_verified: true` for the approval message the `🚦` post names (and the trail's `cutover-approved`
   entry attributing that message to `user`), the benchmark workload (500 requests × concurrency 50 per target over the
   5 contract endpoints), `verify_chain` ok, sealed; the `Cutover Scorecard` (creator = cutover-gate) records the
   refusal of exactly the probe message (the probe POST's `event_id`) with the proxy on legacy and the same approval;
   after a 3 s settle the routing is re-checked and any `↩️ Rollback complete` after the approval fails the run.
   **(live-verified: the event `message_id`, the `read_context` message ids and the `event_id` returned by
   `POST /agents/{id}/message` are the same `sw_<hex>` id.)**
Prints a timestamped step timeline and exits 0 only if every assertion held. Pre-flight (unless `--no-start`): Switch
health, `scripts/targets.sh start --fresh`, `python -m agents.provision --fresh-room` (if `--fresh`), `scripts/agents.sh
restart` (with `STRANGLER_AGENT_PACE=2` under `--cinema`), wait until all three agents are `live`. `--cinema`: 3 s pause
between steps, prints `http://localhost:18065/switch/channels/<slug>` to open first, and a 10 s countdown.
**Live-verified bridge behaviour** (Switch 0.31.0 + Mattermost 11.9): one Switch message = one Mattermost post (long
campaign tables are not split), `@mentions` are not rewritten, a `send_targeted` body arrives as `@<target> <body>`,
document changes appear as `📄 created/updated room document “…”.` posts by the writing agent's bot, and room creation
posts one greeting per agent that @-mentions only that agent itself (posted before the run window opens).

## 4. Operations
* `scripts/agents.sh start|stop|restart|status` — per `tests/test_ops_scripts.py` docstring (logs/pids in
  `$STRANGLER_RUN_DIR`, default `.state/run/`).
* `scripts/targets.sh start [--fresh]|stop|status` — per `tests/test_targets_sh.py` docstring: each target is started
  as a fully redirected background `nohup python -m <module>` (never a wrapper subshell that would hold a capturing
  caller's pipes — the e2e pre-flight captures its output), pid in `.state/run/<name>.pid` = the server itself; `stop`
  signals only a pid whose `/proc/<pid>/cmdline` still holds `-m <module>` (TERM, then KILL after 5 s); `--fresh`
  fails loud if a port stays occupied by an untracked process instead of reusing it, then removes the generated
  `app/lists.py`, `.state/proxy/router.json` and `.state/fuzzer/divergence_registry.json`; start/stop are `flock`ed.
* `Makefile`: `install switch-up switch-down targets provision agents agents-stop test test-integration e2e demo cinema
  stop clean-state` (`demo` = switch-up targets(fresh) provision agents e2e). `requirements.txt` pins the venv deps.

## 5. Switch room template — `strangler-room.template.yaml`
Switch v2 template (`version: 2`, `resources:`) with ONE `room` resource `strangler-migration` whose `agents:` names the
three already-registered custom agents (`translator`, `fuzzer`, `cutover-gate` — strings that are not resource keys pass
through as names; agent *resources* would require Switch agent management + a controller), `users` conditional on the
bridge (`[{if: "{bridge}", value: ["{$creator}"]}, {value: []}]`), `bridge` param (`type: bridge`, `default: [$first]`,
`required: false`), instructions describing the pipeline, a `kickoff` that addresses NO agent, and NO documents named
like the three agent-owned documents. Must lint with 0 errors/0 warnings offline (`switch_core.template_lint`) and via
`POST /gateway/templates/validate`, and must run via `POST /gateway/rooms/from-template-document` (integration test
archives the created room). `scripts/validate_template.py` does all three.

## 6. CI — `.github/workflows/e2e.yml`
On push/PR: ubuntu-latest, Python 3.12 + uv, `make install`, `make test`, `SWITCH_MODE=docker scripts/bootstrap_switch.sh`
(upstream compose `--profile collab --profile gateway`, `SWITCH_VERSION=0.31.0`, Mattermost on 18065, API on 8000), then
`make e2e`; upload `.state/run/*.log` and `.native`/compose logs on failure.
