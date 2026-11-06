# StranglerRoom

**Three Switch agents migrate Open Library's web.py lists endpoints to FastAPI, behind a strangler proxy.
A human in Mattermost makes every decision, and Switch refuses those commands from anyone else.**

The `translator` writes the FastAPI port. The `fuzzer` sends the same shadow probes to legacy and modern and diffs
the responses. The
`cutover-gate` benchmarks both and flips the proxy, but only after the human approves. Every decision, verdict and
flip is written to single-writer Switch room documents and sealed in a hash-chained audit ledger.

| | |
|---|---|
| Live result | **100% Approved Parity (5/5)** · 162 paired shadow probes per campaign (each sent to both targets) · p99 at concurrency 50: legacy 1283 ms → modern 71 ms · cutover approved by `user` · ledger sealed with 14 hash-chained entries |
| Loop time | about 23 s for all 8 steps with `make e2e` (about 34 s wall clock with pre-flight); about 83 s with `make cinema` |
| Stack | Switch 0.31.0 (core + Gateway) · Mattermost 11.9.0 · web.py 0.62 · FastAPI 0.115.12 · Python 3.12 |
| Tests | 1,025 pytest tests (1,023 unit + 2 live integration), a 35-check live end-to-end driver, and `make isolate-verify` (315 live checks when isolated) |

---

## 1. The problem

Open Library is moving from web.py to FastAPI, endpoint by endpoint. The lists endpoints were
[internetarchive/openlibrary#12487](https://github.com/internetarchive/openlibrary/issues/12487), *"Migrate lists
endpoints to fastapi"*, now closed upstream. StranglerRoom replays that migration on a faithful local reconstruction:
`targets/legacy_openlibrary/` reproduces the shape of Open Library's `plugins/openlibrary/lists.py` on web.py with an
infogami shim and fixed seed data. The [migration guide](https://docs.openlibrary.org/projects/fastapi-migration.html) asks each
contributor to verify parity by hand. You write a temporary bash script (`test_<endpoint>_compare.sh`) that `curl`s
the old and new endpoints, compare the outputs, then delete the script before merging.

A throwaway comparison script leaves three gaps:

- **Coverage:** a few hand-picked URLs miss the edge cases where the stacks really differ (`.json` aliases, loose
  query coercion, error bodies).
- **Decisions:** when a legacy quirk turns up, the script does not record whether the port should **keep** it or
  **fix** it.
- **Evidence:** once the script is deleted, nothing in it shows which behaviour was approved, by whom, or that the
  cutover happened only after parity and a benchmark.

StranglerRoom replaces that script with a room. Agents do the porting, differential fuzzing and benchmarking. The
human makes the two calls that matter, keep or fix each quirk and go or no-go on the cutover, and Switch records
both with attribution.

## 2. Architecture

```
   Mattermost :18065  (team "switch", channel strangler-migration-<suffix>)
   ┌──────────────────────────────────────────────────────────────┐
   │  human "user"   ── @translator migrate … / preserve … /      │
   │                    @cutover-gate approve-cutover             │
   └───────────────▲──────────────────────────────────────────────┘
                   │  Mattermost bridge (1 Switch message = 1 post)
   ┌───────────────┴──────────────────────────────────────────────┐
   │  Switch 0.31.0 core + Gateway   :18000   (docker/CI: :8000)  │
   │  room "strangler-migration"                                  │
   │                                                              │
   │   @translator ──handoff──▶ @fuzzer ──handoff──▶ @cutover-gate│
   │        ▲                                            │        │
   │        └──────────── record-cutover ────────────────┘        │
   │                                                              │
   │  room documents (single writer = creator; others read-only)  │
   │   • Parity & Cutover Audit Ledger  ← translator (hash chain) │
   │   • Parity Audit Matrix            ← fuzzer                  │
   │   • Cutover Scorecard              ← cutover-gate            │
   └───────┬──────────────────┬───────────────────────┬───────────┘
           │ SSE (addressed)  │ + 2 s heartbeats      │
   writes app/lists.py   shadow traffic         benchmark + POST /_router/flip
           │                  │                       │
           ▼                  ▼                       ▼
   ┌───────────────┐   ┌─────────────────┐   ┌───────────────────────────┐
   │ modern :9002  │◀──│  diff engine    │──▶│ legacy :9001              │
   │ FastAPI,      │   │ (agents/        │   │ web.py + infogami shim    │
   │ uvicorn       │   │  divergence.py) │   │ (Open Library lists.py)   │
   │ --reload      │   └─────────────────┘   └───────────────────────────┘
   └───────▲───────┘                                   ▲
           │       ┌────────────────────────────┐      │
           └───────│ strangler proxy :9000      │──────┘
                   │ X-Strangler-Upstream:      │
                   │ legacy | modern            │
                   └────────────────────────────┘
```

All targets bind `127.0.0.1`. The proxy's routing state is kept in `.state/proxy/router.json`; every component writes only inside its own `.state/<component>/` directory.

## 3. Why Switch is central (and why this is not a chatbot)

| Switch feature | What StranglerRoom uses it for |
|---|---|
| **Shared room** `strangler-migration` | Three independent agent processes and one human share a single conversation. Agents hand work to each other with targeted `@mentions` (`send_targeted`), with no orchestrator between them. In `make demo` the driver plays the human: it posts the human's three commands as `user` and checks the results. |
| **Creator-only room documents** | Each document has exactly one *agent* writer: Switch answers 403 when an agent other than the creator calls `update_room_document`. Other agents can only read it (the `load_internal_documents` op) and mirror it with a sha256 evidence digest. So nothing authenticated as the fuzzer can rewrite the translator's ledger. The proxy's flip endpoint needs a token that has no public default: `ROUTER_ADMIN_TOKEN` if you set one, otherwise a random per-deployment token generated on first use in `.state/router.token` (mode 600; `ROUTER_TOKEN_FILE` or `STRANGLER_STATE_DIR` move it) and resolved by one shared rule in both the proxy and the cutover gate. The governance gate protects the Switch command path. By default every process runs as the operator, so a local process that can read `.state/` could still call `/_router/flip` directly or read the agents' credentials; `make isolate` runs each component as its own Unix user, so that none of them can read `.state/` or another agent's key, and only the proxy and the cutover gate (which receive the router token in their environment) can call `/_router/flip` (§8). |
| **Hash-chained ledger** | Every trail entry stores `prev` and `hash = sha256(canonical(entry))` (`agents/ledger.py`, `verify_chain`). Sealing writes `sealed = {at, head_hash, entries}`. The driver re-verifies the chain through the Gateway, as admin and independently of the agents. The hashes are unkeyed, so tamper evidence comes from the head hash also being posted in the channel: a later rewrite of the document, for example by a Gateway admin, would no longer match it. |
| **Addressing-policy unlock** | Bootstrap-registered agents are owned by a non-admin user, so by default Switch refuses messages from the human. `agents/provision.py` clears each agent's policy as Gateway admin (`PUT /gateway/agents/{id}/addressing-policy {"policy": null}`). |
| **Human-only governance gate** | `migrate`, `preserve/deprecate`, `approve-cutover` and `rollback` are refused unless the sender is the room's human lead: a participant whose `type`, as Switch reports it, is `user` and whose name is `RoomInfo.human` (`RoomAgent.classify`; other people who join the bridged channel are `guest`s, and names that collide after case folding are refused). Nothing in the message text can change that. Build changes and routing changes are mutually exclusive: the translator holds a cross-process production lock (`agents/locks.py`, an `flock`) for a whole `migrate`/decision and the cutover gate for a whole `approve-cutover`/`rollback`, so production can never be flipped onto a build that is being replaced. Stale or replayed commands are refused too: an approval older than the latest benchmark or router flip, a rollback older than the latest flip, and a migrate/decision message older than the current migration's request never act. The demo proves this live: it posts `@cutover-gate approve-cutover` *as the fuzzer agent* (`POST /agents/{id}/message`) and asserts `🛑 Governance gate` with the proxy still on legacy. |
| **Attributed approvals** | The translator cross-checks the approval's `message_id` against the room history before it seals. The cutover gate records the approval's own Switch timestamp, so ONE `read_context` of the window ending at that moment finds it (the timestamp only narrows the search — the message found must still be the lead's own `approve-cutover`); records without it fall back to a paged, id-deduplicated scan that fails closed (seal refused, never forged) in the corner cases Switch's timestamp-only paging cannot traverse. Either way: the message must be the lead's own `approve-cutover`, posted after the build being cut over, and the flip must be verified. Otherwise it refuses to seal (a cutover-gate scorecard alone is never taken as the human's word), so a sealed ledger always records `approval_message_verified: true`. Switch event ids, `read_context` ids and the probe's `event_id` are the same `sw_<32 hex>` id (verified live). |
| **SSE + 2 s heartbeats** | Each agent holds ONE stream (`scope=single&filter=addressed&rooms=<room>`), sends heartbeats every 2 s (`HEARTBEAT_INTERVAL_S = 2.0`), reconnects on its own and dedupes events by sequence number. |
| **Rehydration** | A restarted agent rebuilds its state from the room documents and normally posts nothing. It speaks only when something disagrees: the fuzzer posts an `ℹ️` when it restores its divergence registry from the matrix, and the cutover gate posts a `⚠️` when the scorecard and the live router disagree. This was checked live (restarting all three agents produced 0 new posts) and is covered by `tests/test_agents_loop.py`. |
| **Mattermost bridge** | The human uses an ordinary chat client. Agent posts, document-change notices (`📄 updated room document …`) and long Markdown tables all arrive unchanged. |

## 4. The five legacy quirks

The fuzzer classifies divergences from the observed responses with fixed rules in `agents/divergence.py`. It ignores
probe labels, and IDs come from a persistent first-seen registry. A new kind of divergence would show up as
`unclassified:…` with the ID D-6 or higher.

| ID | Tag | Legacy web.py | Naive FastAPI v1 | Demo decision |
|---|---|---|---|---|
| D-1 | `json_suffix` | `/….json` aliases served (infogami implicit alias) | routing `404 {"detail":"Not Found"}` | **preserve** |
| D-2 | `loose_query` | `limit=none`, `limit=`, `details=maybe`, `quantity=abc` coerced | strict `int`/`bool` → `422` | **preserve** |
| D-3 | `missing_resource_200` | unknown list → `200 {"error":"not_found","entries":[]}` | `404` with the same body | **deprecate** (404 becomes the contract) |
| D-4 | `non_positive_quantity` | quote `quantity` −10..0 accepted (warehouse credit reversals) | `PositiveInt` → `422` | **preserve** |
| D-5 | `empty_body_500` | POST seeds with empty add+remove → `ZeroDivisionError` → `500 text/html` | pydantic validator → `422` | **deprecate** (422 becomes the contract) |

**Live LLM-mode check.** The demo uses deterministic codegen so that v1 shows exactly these five quirks. With
`STRANGLER_TRANSLATOR_MODE=llm` the translator asks Claude (`claude -p --model claude-sonnet-5-5`) for the port
instead. In a live smoke run (`agents.llm_synth.synthesize` → `validate_module` → isolated modern server → a
102-probe campaign) the model returned a valid module in 19 s. The fuzzer found four of the five quirks (the model
happened to keep legacy's −10..0 quote quantities, so there was no non-positive cluster) **plus one divergence the fixed
rules do not know**, reported as `unclassified`: for an unknown user with an empty body, legacy checks the list
first (`200 {"error":"not_found",…}`) while the LLM's port validates the body first (`422`). That is the point of the
room: a subtle check-order change a hand-picked `curl` script would likely miss is put in front of the human, who
can preserve or deprecate it like any other ID.

## 5. The 8-step flow, with real posts from the live run

Excerpts below are copied verbatim from the Mattermost channel of the final recorded `make cinema` run
(`--cinema --fresh`, migration `MIG-20261105-081156`, channel `strangler-migration-98f74858`): 58 s from the first
human post to the seal, 35/35 driver checks. Every `--fresh` run (`make e2e`, `make cinema`, `make demo`) opens a new
room and channel with a new migration id, so revs, message ids and benchmark numbers differ from run to run. Long
posts are trimmed (`…`).

| # | Who | What happens |
|---|---|---|
| 1 | human | `@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI` |
| 2 | translator | AST-extracts the 5 legacy routes (never imports the module), generates v1, waits for hot reload, opens the ledger, hands off to the fuzzer |
| 3 | fuzzer | 162-request shadow campaign → D-1..D-5, asks the human |
| 4 | human | `@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5` |
| 5 | translator | records each decision (with matrix evidence), generates v2, hands off for re-verification |
| 6 | fuzzer | `✅ 100% Approved Parity (5/5)` → hands off to the cutover gate. **Governance probe:** the driver, acting as a "compromised" fuzzer, sends `approve-cutover`. The gate refuses it after the benchmark, because it handles events one at a time |
| 7 | cutover-gate | benchmarks legacy then modern (500 requests at concurrency 50 each), asks the human |
| 8 | human → cutover-gate → translator | `@cutover-gate approve-cutover` → proxy flip verified → `record-cutover` → ledger sealed |

> **translator** · 🧭 Migration MIG-20261105-081156 accepted from @user: `/people/{user}/lists` · legacy_openlibrary
> (web.py) → FastAPI. AST-extracted 5 legacy routes from `targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py`
> (parsed, never imported or executed) …
>
> **translator** · 🛠️ v1 deployed to :9002 — rev `4d5e8da49539` (deterministic codegen): a naive, textbook FastAPI
> port with no legacy quirk preserved yet. …
>
> **translator** · @fuzzer shadow-test rev=4d5e8da49539 migration=MIG-20261105-081156

> **fuzzer** · 🔬 Shadow campaign #1 on rev `4d5e8da49539`: 162 requests … **Parity 0% (0/5 clusters resolved)** ·
> 162 probes · 31 identical · 131 divergent · 1.90s
>
> | ID | Divergence | Legacy | Modern | Hits | Sample | Decision/Status |
> |----|------------|--------|--------|------|--------|-----------------|
> | D-1 | Implicit .json suffix routing | 200 | 404 | 62 | `GET /people/george08/lists.json` | — / open |
> | D-2 | Loose query-param coercion | 200 | 422 | 31 | `GET /people/george08/lists?limit=none` | — / open |
> | D-3 | Missing list answers 200 not_found | 200 | 404 | 29 | `GET /people/george08/lists/OL99L/seeds` | — / open |
> | D-4 | Non-positive quote quantity accepted | 200 | 422 | 4 | `GET /books/OL1M/quote?quantity=-1` | — / open |
> | D-5 | Empty add/remove POST crashes with 500 | 500 | 422 | 5 | `POST /people/george08/lists/OL1L/seeds {}` | — / open |
>
> **fuzzer** · @user decision needed on D-1 D-2 D-3 D-4 D-5 … Reply to the translator with e.g.
> `preserve D-1 D-2 D-4 and deprecate D-3 D-5`.

> **translator** · 🔧 Applied human decisions from @user: preserve D-1 D-2 D-4 and deprecate D-3 D-5 …
> - …
> - **D-3** `missing_resource_200` → **deprecate**: an unknown list now answers `404` — the legacy 200 was a bug and the 404 is the new contract.
> - **D-4** `non_positive_quantity` → **preserve**: quote quantities −10..0 are accepted again (warehouse credit reversals).
> - …
>
> v2 deployed to :9002 — rev `d61c585b923b` (deterministic codegen) …

> **fuzzer** · ✅ 100% Approved Parity (5/5) on rev `d61c585b923b` — 162 shadow requests; every divergence cluster is
> resolved by the human decisions …

> **cutover-gate** · 📊 Benchmark — rev `d61c585b923b` (migration MIG-20261105-081156) · 500 requests per target at
> concurrency 50, sequential (legacy first), round-robin over 5 contract endpoints · parity: 100% Approved Parity (5/5)
>
> | Metric | Legacy | Modern | Delta |
> | --- | --- | --- | --- |
> | p50 latency (ms) | 32.3 | 67.8 | +109.9% |
> | p95 latency (ms) | 1037.3 | 71.0 | -93.2% |
> | p99 latency (ms) | 1282.9 | 71.3 | -94.4% |
> | throughput (req/s) | 328.8 | 732.4 | +122.7% |
> | errors | 0 | 0 | 0 |
>
> **Verdict:** modern is faster: p99 71.3 ms vs 1282.9 ms (-94.4%); throughput 732.4 vs 328.8 req/s (+122.7%); …

> **fuzzer** (governance probe sent by the driver as the fuzzer agent) · @cutover-gate approve-cutover
>
> **cutover-gate** · 🛑 Governance gate: refused — `approve-cutover` is a human-only command; the sender `fuzzer` is a
> room agent, not the human lead. Nothing changed: the proxy :9000 stays on `legacy`. … The refusal is recorded in the
> Cutover Scorecard.

> **cutover-gate** · 🚦 Cutover complete: :9000 now serves the modern FastAPI service (:9002) — approved by @user
> (message `sw_a3acb221e8a7bffa910a511407b0993b`). Evidence: 100% Approved Parity (5/5) on rev `d61c585b923b`;
> p99 1282.9 ms → 71.3 ms. Verified through the proxy …
>
> **translator** · @user 🧾 Ledger sealed — Migration MIG-20261105-081156 complete. … audit trail 14 entries, head
> b3374e23bf3d — head `sha256:b3374e23bf3ddd345b63419623838115d2b051c4965016c6d963f508fb0b9500`. …

Apart from Switch's own join greetings when the room is created, the agents write `@<agent>` only in deliberate handoffs. Instructions addressed to the human name agents without the `@`
("reply to the translator …"), so the human's copy of an instruction does not wake an agent. Quoted user text in agents' error posts is defused with
`agents.commands.defuse_mentions`.

## 6. Results (sealed ledger, read back through the Switch Gateway as admin)

Values from the final recorded `make cinema` run of §5. Each run's own values are printed by the driver and live in its
room's ledger (the current room is the one in `.state/room.json`).

| Item | Value |
|---|---|
| Room · migration | `5660faa1-b0c8-498e-bd50-2b9c4796a635` · `MIG-20261105-081156` · status `sealed` |
| Decisions | D-1/D-2/D-4 preserve, D-3/D-5 deprecate — all by `user`, message `sw_c23459edb6bff3ef7e34d95b5404f83f` |
| Campaign #1 (v1 `4d5e8da49539`) | 162 requests: 31 identical, 131 divergent, **0/5** resolved |
| Campaign #2 (v2 `d61c585b923b`) | 162 requests: 97 identical, 65 divergent, all approved deviations → **100% Approved Parity (5/5)** |
| Benchmark (c=50, 500 req/target) | legacy p50/p95/p99 **32.3 / 1037.3 / 1282.9 ms**, 328.8 req/s · modern **67.8 / 71.0 / 71.3 ms**, **732.4 req/s** · 0 errors on both |
| Cutover | approved by `user` (`sw_a3acb221e8a7bffa910a511407b0993b`), `approval_message_verified: true`, proxy `modern`, verified |
| Trail | 14 entries, `verify_chain` ok, head `sha256:b3374e23bf3d…08fb0b9500` |

### How the benchmark measures (and a bug we found in it)

`agents/bench.py` is a closed-loop load generator: `concurrency` virtual users, each on **one keep-alive connection
of its own**, issue requests back to back, round-robin over the 5 contract endpoints; every user opens its connection
in an unmeasured warm-up, and the two targets are benchmarked one after the other. That mirrors production behind the
strangler proxy, which talks to its upstreams over persistent connections.

The first live runs used one shared `httpx` pool for all 50 requests in flight and reported *"modern is slower"*
(e.g. `MIG-20261105-064537`: p99 1912 ms legacy vs 2194 ms modern, 181 vs 97 req/s). Profiling showed that verdict
measured the **load generator**, not the servers: httpcore re-scans every queued request against every pooled
connection on each event (~4,250 `is_idle()` calls per request at concurrency 50), so the client saturated a CPU — and
penalised precisely the server that keeps all 50 connections alive. The fix is the per-user connection model above,
with a regression test that transport errors still count toward latency. An isolated sweep on this box (500
requests per target, both servers fresh, same paths):

| Concurrency | Legacy web.py (cheroot, 30 threads) | Modern FastAPI (1 uvicorn worker) |
|---|---|---|
| 1 | p50 3.1–3.2 ms · p99 3.9–4.4 ms · ~322 req/s | p50 **2.7 ms** · p99 **3.3 ms** · **~365 req/s** |
| 10 | p50 14.7–14.9 ms · p99 28–31 ms · ~642 req/s | p50 14.5–14.6 ms · p99 **16–18 ms** · **~679 req/s** |
| 50 | p50 32–38 ms · p99 ~1.31–1.33 s · ~320 req/s | p50 68–69 ms · p99 **73–214 ms** · **607–726 req/s** |

Reading it honestly: at concurrency 50, legacy's *median* is lower, but cheroot serves each keep-alive connection on
one of its 30 worker threads, so 20 of the 50 users queue for a thread (its accept backlog is also only 5) — hence
the ~1.3 s tail. Modern's single event loop interleaves all 50 connections: a higher median, a tight tail and about
twice the throughput. Caveats: the modern target runs one uvicorn worker under the `--reload` dev supervisor, the load
generator and both servers share one 4-CPU cgroup quota that the kernel throttles, and numbers vary between runs. The
gate exists to put a measured number in front of the human before the flip, not to settle web.py vs FastAPI.

## 7. Quickstart

Prerequisites: Python 3.12, [`uv`](https://docs.astral.sh/uv/), `curl`, `git`, and either a Docker daemon or, for the
native stack, a Debian/Ubuntu x86-64 host with root (it `apt-get install`s `postgresql-16`), Node.js/`npm` (it builds
the Gateway UI) and internet access (it clones Switch and downloads Mattermost). `postgresql-16` is in the default apt repositories of
Ubuntu 24.04; other releases need the PGDG repository. Both modes need `openssl`, which is used to generate the
Switch secrets. See §8.

```bash
make install   # creates .venv with uv and installs requirements.txt (pinned)
make demo      # switch-up → targets.sh start --fresh → provision → agents → e2e (driver --fresh)
```

`make demo` exits 0 only if all 35 checks pass. Other targets (`make help`):

| Target | Does |
|---|---|
| `make switch-up` / `switch-down` | `scripts/bootstrap_switch.sh up` / `stop` |
| `make targets` | `scripts/targets.sh start` (legacy :9001, modern :9002, proxy :9000) |
| `make provision` | `python -m agents.provision` (register agents, clear addressing policies, create/reuse the room, write `.state/room.json`) |
| `make agents` / `agents-stop` | `scripts/agents.sh start` / `stop` (also `restart`, `status`) |
| `make e2e` | `.venv/bin/python scripts/run_e2e_demo.py --fresh` |
| `make cinema` | `.venv/bin/python scripts/run_e2e_demo.py --cinema --fresh` |
| `make test` / `test-integration` | `pytest -m 'not integration'` / `pytest -m integration` |
| `make stop` | stop agents, targets and Switch (native mode stops only the servers `switch-up` started and recorded in `.native/*.pid`; it leaves Postgres up rather than pull it from under an untracked server) |
| `make clean-state` | stop agents and targets, then wipe `.state/` except `switch.env` and `connection.env` (agent credentials included, so the next `make provision` re-registers them), and, when run as root, empty the service area's `state/` (`/srv/stranglerroom/state` by default, or the marker's service home) if it exists; as a normal user it leaves the service area alone |
| `make isolate` / `isolate-verify` / `isolate-off` | run every component as its own Unix user from `/srv/stranglerroom` / prove it / turn it off (§8) |

### Recording mode (`make cinema`)

`--cinema` runs the agents with `STRANGLER_AGENT_PACE=2` (a 2 s delay before each post), pauses 3 s between steps,
and gives you a 10 s countdown. The whole run takes about 83 s.

1. Forward ports **18065** (Mattermost) and **18000** (Switch Gateway UI; docker mode: **3000** for the UI and **8000**
   for the API) when the box is remote (VS Code:
   *Ports → Forward a Port*).
2. Run `make cinema`. Because of `--fresh`, it archives the old room and creates a new channel, then prints
   `http://localhost:18065/switch/channels/<slug>`. Open that URL during the countdown.
3. Log in to Mattermost as **`user`** with `MATTERMOST_USER_PASSWORD` from `.state/switch.env`.
4. Optional: open the Switch Gateway UI at `http://127.0.0.1:18000` (docker mode: `http://localhost:3000`) and sign in as `GATEWAY_ADMIN_EMAIL` /
   `GATEWAY_ADMIN_PASSWORD` from `.state/switch.env` to watch the room and its three documents.

To print the current channel's browser URL:
`.venv/bin/python -c "import json;from agents.switch_client import Settings;s=Settings.load();print(f\"{s.mattermost_url}/{s.mattermost_team}/channels/{json.load(open('.state/room.json'))['channel_url'].rsplit('/',1)[1]}\")"`
(run it from the repo root; it uses the browser-facing `MATTERMOST_URL`, which also works in docker mode where the
bridge records Mattermost's in-network address).

### Driving it by hand

After a demo the ledger is sealed and the proxy is on modern, so start from a fresh state (Switch stays up):

```bash
make agents-stop
PYTHON=.venv/bin/python bash scripts/targets.sh start --fresh   # resets generated lists.py, router state, registry
PYTHONPATH=. .venv/bin/python -m agents.provision --fresh-room   # archives the old room, new channel
make agents
```

Then post in the new channel as `user`, one command at a time, each after the agents have asked for it:

```
@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI
@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5     # after the fuzzer's "decision needed"
@cutover-gate approve-cutover                              # after the cutover gate's "approval needed"
@cutover-gate rollback            # optional: flips :9000 back to legacy (human only)
@translator status                # any time; also @fuzzer status / @cutover-gate status, and help
```

Watch the routing with `curl -s 127.0.0.1:9000/_router/status` or
`curl -si 127.0.0.1:9000/people/george08/lists.json | grep -i x-strangler-upstream`.

## 8. Native vs Docker

`scripts/bootstrap_switch.sh` uses docker compose when a Docker daemon is reachable and the native stack
otherwise. Force a mode with `SWITCH_MODE=docker|native`.

| | Native (this dev box: an unprivileged k8s pod, no Docker) | Docker (CI) |
|---|---|---|
| How | Builds Switch 0.31.0 from the `switch-v0.31.0` tag into `.switch-infra/`, plus Postgres 16, the Mattermost 11.9.0 tarball, and switch-core via `uv` serving the built Gateway UI | Upstream `deploy/local/standalone-docker-compose.yml --profile collab --profile gateway` |
| Switch API (`SWITCH_URL`) | `http://127.0.0.1:18000` | `http://127.0.0.1:8000` |
| Gateway UI (Switch Console) | `http://localhost:18000` (served by switch-core) | `http://localhost:3000` (`gateway` container) |
| Mattermost | `http://localhost:18065` | `http://localhost:18065` |

Both modes run the upstream seeder and write `.state/switch.env` (URLs and credentials, mode 0600) and its public part
`.state/connection.env` (URLs only, 0644). Provisioning and the driver read `switch.env` through `Settings.load()`. An
agent reads only `SWITCH_URL`, from the environment or `connection.env` (`Settings.load_agent()`): it never opens
`switch.env`, so it never holds the registration token, the admin password or the human's password. Environment
variables override the files.

CI (`.github/workflows/e2e.yml`) has two jobs. `unit` runs `make install` and `make test`, then the isolation tests as
root (`sudo -E`). `e2e` runs
`SWITCH_MODE=docker bash scripts/bootstrap_switch.sh`, `make install` and `make e2e`. It then creates switch-core's venv
(`uv sync --frozen --no-dev --project .switch-infra/core`) and runs `validate_template.py --run`. On failure or
cancellation it uploads `.state/run/*.log` and the compose logs.

### Per-component OS users (`make isolate`)

By default every StranglerRoom process runs as the operator, so a component — or code it runs, like the
translator-generated module inside the modern server — could read every secret in `.state/`: the other agents' API keys,
the router token, and `switch.env`, which holds the human lead's Mattermost password. `make isolate` (root, once) turns
on isolation for this checkout ([docs/ISOLATION.md](docs/ISOLATION.md)):

* six system users (`sr-translator`, `sr-fuzzer`, `sr-cutover-gate`, `sr-proxy`, `sr-legacy`, `sr-modern`) and a group
  `sr-prodlock` shared by the translator and the gate for the production lock;
* a root-owned service area `/srv/stranglerroom` holds:
  * a mirror of the code and the virtualenv;
  * a state directory in which each component owns only its private directory (0700). The translator also owns
    `module/` (0755), which holds the generated module the modern server serves;
  * a copy of each agent's own API key (`state/agents/<name>.json`, 0600, owned by it) and one shared, read-only
    copy of `room.json` (root, 0644).

  The router token reaches only the proxy and the gate, through their environment;
* `/root`, the checkout and its `.state/` keep their permissions (setup changes none of them). `/root` is 0700, so no
  service user can reach the checkout below it: `switch.env`, the router token, the master copies of the keys and
  the logs are all out of reach.

From then on `scripts/agents.sh` and `scripts/targets.sh` (and therefore `make demo`) launch each process through
`python -m agents.isolation exec`. It refreshes the mirrors, gives the component a new session, closes every inherited
descriptor, drops to the component's user and `exec`s it, with an environment built from that component's own
allow-list. `make isolate-verify` proves it: as each service user it attempts every access in the matrix of
docs/ISOLATION.md §6 (the kernel decides), imports the component's module, and checks the live processes' uids,
sessions and environments. It also scans every file that user can reach under the service area and the scan roots
(`/srv`, `/tmp`, `/var/tmp`, `/dev/shm`, `/etc`, `/home`, `/opt`, `/var/log`, `/var/lib/stranglerroom`, `/root`) for
every secret value of at least 8 characters.

`make isolate-off` turns isolation off: it removes the marker and the agents' API-key copies.
`python -m agents.isolation disable --purge` also removes `/srv/stranglerroom`, the six users, their groups and
`sr-prodlock`. CI forces `STRANGLER_ISOLATION=off`, and the runner has no marker, so isolation stays off there. That
includes the unit job's step that runs the isolation tests as root against temporary fixtures.

On the dev box (Switch 0.31.0 native), after `make isolate`, the live e2e ran with all six components as their own users:
`run_e2e_demo.py --fresh` passed 35/35 in three runs, on the round-2 code right after `make isolate`, then after review round 3, then after rounds 3b/3c. The final `make isolate-verify` with the
stack running: 315 checks, 313 PASS and 0 FAIL. The 2 SKIPs are LLM mode being off and Mattermost's built-in
placeholder database password, which is too short to search for. The 313 cover:
* 195 access probes run as each user, and 30 refused cross-component signals;
* every component process's uids, groups, session and environment, and the owner of every listener on the Switch and
  target ports in both address families;
* six smoke imports;
* a secret scan that covered all its roots, with no budget hit: 4,312 world-readable files (6.5 GB, including other
  projects' tarballs and CUDA libraries on this VM) searched once, plus each user's own reachable files.

The live negative checks also held:
* `setup` and `disable` refuse while components run;
* agents started unisolated are refused by `agents.sh start` and fail `isolate-verify`;
* a `nobody`-owned `[::1]:18000` listener fails `isolate-verify`;
* a foreign server on `:9001` is refused by `targets.sh start`.

Turning isolation on or off switches each component to the other layout's state (the service area's or the checkout's
`.state/`). Routing, the divergence registry and the generated module do not carry over, so start fresh afterwards
(`make demo`, or `scripts/targets.sh start --fresh`). `setup` and `disable` say so.

What it does not do: a component keeps its own authority. A compromised translator can still publish any module, a
compromised gate can still flip the proxy, and a compromised proxy can route anywhere. Switch, Mattermost and Postgres
are unchanged, and they authenticate their clients.

Two host-level rules come with it, because service users can open sockets:

* **IP literals for servers that listen on one address family.** Switch and the targets listen on `127.0.0.1` only. A
  service user could bind the same port on `::1`, which `localhost` resolves to first on many hosts, and receive whatever
  a client sends there: API keys, or the admin password. So `SWITCH_URL` is `http://127.0.0.1:18000`, the launcher
  refuses a `localhost` Switch/target URL, and `isolate-verify` checks who owns every listener on those ports, in both
  address families. Mattermost listens on all addresses (`[::]`), so `localhost:18065` cannot be squatted.
* **Cookies are host-scoped, not port-scoped.** Keep Mattermost at `localhost:18065` in the browser. Use the proxy
  (`127.0.0.1:9000`), not the targets' own ports, in a browser that holds a Mattermost or Gateway session. The proxy
  drops those session cookies before forwarding; a direct request to `:9001` or `:9002` would hand them to code that
  the translator generated.

## 9. Switch Console template

`strangler-room.template.yaml` is a Switch v2 room template. It defines one `room` resource that lists the three
registered agents by name; you are added only when the bridge param is set. It adds no agent-owned documents and
has a kickoff that addresses no agent. To create the room from the Switch Console instead of
`agents.provision`, import it under **Templates** and run it. Its header lists the prerequisites: the agents must
already be registered, and the room name must not clash with the provisioned room. The running agents serve only the
room recorded in `.state/room.json`, which the template does not update, so the template is a way to create the room
from Switch Console, not a replacement for `make provision` in the demo.

```bash
.venv/bin/python scripts/validate_template.py                 # Switch's own engine offline + POST /gateway/templates/validate
.venv/bin/python scripts/validate_template.py --offline-only  # engine only
.venv/bin/python scripts/validate_template.py --run           # + create strangler-migration-template-check live, check, archive
```

The offline stage runs Switch's own engine, so it needs switch-core's venv `.switch-infra/core/.venv`. The native
bootstrap creates it; elsewhere run `uv sync --frozen --no-dev --project .switch-infra/core`.

Current result: `strangler-room.template.yaml: PASS (offline, live)`, 0 errors and 0 warnings.

## 10. Repo layout

```
agents/
  switch_client.py     Switch 0.31.0 client: Settings, SwitchGateway (admin), SwitchAgent (SSE, heartbeats, ops)
  provision.py         register agents, clear addressing policies, create/reuse the room → .state/room.json
  runtime.py           RoomAgent base: sender classification, say/handoff/ask_human, dispatch, pacing
  commands.py          command grammar + MARK message prefixes (single source of truth)
  ledger.py            the 3 room documents: state embed/extract, hash chain, renderers
  translator_agent.py  @translator (+ legacy_introspect.py AST extraction, llm_synth.py LLM mode, codegen/)
  fuzzer_agent.py      @fuzzer (+ divergence.py diff/classify/campaign)
  cutover_agent.py     @cutover-gate (+ bench.py load benchmark, proxy_control.py flip/verify)
  mattermost.py        Mattermost REST client for the demo driver
targets/
  legacy_openlibrary/  web.py + infogami shim + Open Library-shaped plugins/openlibrary/lists.py  (:9001)
  modern_fastapi/      FastAPI app; app/lists.py is generated by @translator (gitignored)        (:9002)
  proxy/router.py      strangler proxy, X-Strangler-Upstream, /_router/status|flip               (:9000)
  common/dataset.py    shared seed data (george08, openlibrary, mekBot; editions with prices)
scripts/
  bootstrap_switch.sh  Switch + Mattermost stack (docker | native)
  targets.sh           start [--fresh] | stop | status for the 3 targets (flock, pid-verified)
  agents.sh            start | stop | restart | status for the 3 agents (logs/pids in .state/run/)
  run_e2e_demo.py      the 8-step live driver (--cinema --fresh --no-start --step-timeout)
  validate_template.py template checks (offline engine, live Gateway, live run)
docs/                  SPEC.md (targets + divergence engine) · AGENTS_SPEC.md (room contract) · SWITCH_PROTOCOL.md (API, file:line cited) · REVIEW_LOG.md
tools/hacp_delegate.py HACP/2.0 peer-delegation harness (dev process only)
tests/                 pytest suite + FakeRoom (tests/fakes.py) with faithful Switch semantics
.hacp/                 contracts/, results/, reviews/, session ledgers of the peer swarm
```

## 11. Tests

```bash
make test               # .venv/bin/python -m pytest -m 'not integration'   → 1021 passed, 2 skipped (~5.5 min on 4 CPUs)
make test-integration   # live Switch + Mattermost                           → 2 passed
make e2e                # live 8-step driver                                 → 35/35 checks
make isolate-verify     # (isolated hosts) live isolation proof              → 313 passed, 0 failed, 2 skipped
```

| Layer | Count | What |
|---|---|---|
| Unit (no live Switch) | **1,023** in 28 files: the template-engine tests skip without `.switch-infra/core/.venv`; two isolation tests need a host without the `sr-*` users, so they skip on the isolated dev box. CI's `unit` job runs `make test` as its non-root user, then the isolation tests again as root on a clean runner, which covers the launcher's root-only paths | per-component isolation (212: launcher, privilege drop, mirrors, setup/disable, verify, and the scripts' isolated paths, all against temporary fixtures, never the host), state layout and least privilege (21), targets contract and adversarial cases (71), codegen (277), divergence engine (24), the three agents (131, including fuzzer edge cases, governance/replay/history regressions and the production lock), driver logic (86), ops scripts (45), template (39 offline), CI workflow (13), … |
| In-process end-to-end (part of the 1,023) | 3 | `test_agents_loop.py`: the three real agents in a `FakeRoom` against real target subprocesses, the full loop plus a restart and replay. `test_parity_integration.py`: D-1..D-5 → 5/5 plus a `deprecated-violated` negative control |
| Live integration (`-m integration`) | **2** | `test_switch_integration.py::test_switch_client_end_to_end` (throwaway agents) and `test_template.py::test_live_validate_run_and_archive` |
| Live end-to-end | **35 checks** | `scripts/run_e2e_demo.py` against the real stack. Runs 2–7 all exited 0 (run 1 exposed a `targets.sh` hang, since fixed). Then 2 × `--fresh` and 1 × `--cinema --fresh`, each 35/35. After the critic's governance hardening: `make demo` and `run_e2e_demo.py --fresh`, each 35/35. Final recorded `--cinema --fresh` run (§5): 35/35, followed by `--fresh` verification runs after the final hardening (per-deployment router token; paged approval lookup and stricter replay checks), each 35/35. With isolation on (every component as its own Unix user from `/srv/stranglerroom`): 35/35 in each of three runs, on the round-2 code right after `make isolate`, then after review round 3, then after rounds 3b/3c |
| Live isolation (`make isolate-verify`) | **315 checks** | The final run on the dev box, with the stack live: 313 PASS, 0 FAIL, 2 SKIP (LLM mode off; Mattermost's placeholder password, too short to search for). The 313 cover 195 access probes run as each user, 30 refused cross-component signals, process uids, sessions and environments, listener owners, smoke imports, and a secret scan that covered all its roots: 6.5 GB of world-readable files searched once, plus each user's own reachable files |

Test hygiene: tests that start the modern target point `MODERN_LISTS_PATH` at a tmp file, all ports come from
`tests.procs.free_port()`, and unit tests never touch the live Switch. Spawned targets and the production lock use a
per-session temp dir, so the real `.state` is never written. Tests that drive the scripts' isolated paths stub process
control and use a test-only user prefix. The session fails if `/etc/stranglerroom`, the real API-key copies, or any
live `sr-*` process changed while it ran.

## 12. The HACP peer swarm

Claude led the build and delegated well-specified modules and reviews to other agent CLIs (codex, opencode, agy;
mostly non-Claude models) over
[HACP/2.0](https://github.com/manvendersingh21/hacp) (`tools/hacp_delegate.py`). The protocol runs open → contract
→ freeze (sha256 revision) → work → artifact manifest → measured verification → settlement, rework or escalation.
Each peer works in an **isolated snapshot** (`.hacp/work/<session>/`). Only its declared outputs that pass acceptance
are merged back, and only if the real file did not change in the meantime.

| Peer · model | Settled deliverables |
|---|---|
| **codex** · `gpt-6.1-sol` (text mode) | `agents/mattermost.py`, `agents/llm_synth.py`, `.github/workflows/e2e.yml`; reviews in `.hacp/reviews/`: `fuzzer_agent.md`, `bench.md`, `proxy_router.md`, `integration_tests.md`, `agents_protocol.md`, `integrate-live-e2e.md` (11 findings; the lead fixed 9 and declined 2), `docs-readme.md` (accuracy review of this README), `critic-dod-audit.md` and `critic-hardening.md` (the critic's Definition-of-Done audit and a review of its governance/replay hardening) |
| **opencode** · `opencode/big-pickle` | `targets/proxy/router.py`, `agents/proxy_control.py`, `scripts/agents.sh` + `Makefile` + `requirements.txt`, `tests/test_fuzzer_agent_edges.py`, `tests/test_targets_sh.py`, review `.hacp/reviews/agents_loop.md` |
| **agy** · `gemini-3.8-flash-high` | `agents/bench.py` |
| **agy** · `claude-opus-4-6-thinking` | `agents/legacy_introspect.py`, `strangler-room.template.yaml` |

22 tasks settled: 19 in the build and integration phases (one of them a codex smoke test of the harness; 3 needed
more than one session), the codex accuracy review of this README and the critic's two codex reviews. 3 escalated or were stopped and were then redone by the lead or re-issued: `legacy-target`
(codex), an agy loop review (redone by opencode), and the first `targets.sh` test (re-issued as `-r2`). Contracts are
in `.hacp/contracts/`, verdicts in `.hacp/results/*.json` and the session ledgers, and every protocol message in `.hacp/sessions/*.jsonl` (the earliest sessions are in `.hacp/s-*.jsonl`). HACP is
dev tooling only; the product itself speaks only Switch.

Every review round, each finding's disposition (fixed with a regression test, or declined with the reason) and the accepted residual risks are listed in [`docs/REVIEW_LOG.md`](docs/REVIEW_LOG.md).

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `make e2e` prints `PRE-FLIGHT FAILED: …` and exits 2 | Read the reason after the colon (Switch health, target ports, agents not `live`, proxy not on legacy, Mattermost login). If Switch is down, run `make switch-up` and confirm that `curl -s 127.0.0.1:18000/health` (docker: `:8000`) returns `{"status":"ok"}` |
| `targets.sh start --fresh` fails with a busy-port error | Something this repo doesn't track holds 9000, 9001 or 9002. Find its owner with `ss -ltnp \| grep ':900'` or `lsof -i :9000`, then stop it; `--fresh` deliberately never reuses an untracked server |
| An agent is not `live` / no replies | `bash scripts/agents.sh status`, then read `.state/run/<agent>.log`; `bash scripts/agents.sh restart` (with isolation on, run it as root: the restart re-copies the room and key into the service area and relaunches the agent as its user) |
| Isolation: `agents.sh`/`targets.sh` say a component runs "not as sr-<name>", or `isolate-verify` fails | Something runs unisolated or untracked: `bash scripts/agents.sh restart` / `bash scripts/targets.sh stop && bash scripts/targets.sh start`, then `make isolate-verify`. The failing rows name the process, uid or file |
| "You're not permitted to direct messages to me in this room." | The addressing policies were not cleared: run `make provision` (it uses the Gateway admin from `.state/switch.env`) |
| Old channel or the wrong room | `--fresh` archives the old room and creates a new channel with a new slug. Use the URL that `make cinema` prints, or the `channel_url` in `.state/room.json` |
| Proxy left on modern after a run | Post `@cutover-gate rollback` as `user`, or check with `curl -s 127.0.0.1:9000/_router/status` |
| `⚠️ LLM synthesis rejected …` | Only in `STRANGLER_TRANSLATOR_MODE=llm`: the generated module failed validation, and the translator fell back to deterministic codegen and said so (tolerated by the driver) |
| Step timeout on a slow box | `.venv/bin/python scripts/run_e2e_demo.py --fresh --step-timeout 480` (default 240 s; the slowest step, the benchmark, takes about 9 s) |
| Want a clean slate | `make clean-state` (keeps `.state/switch.env` and `connection.env`), then `make demo` |
