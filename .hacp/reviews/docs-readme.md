# README accuracy review

Review scope: the supplied README, source excerpts, `readme_facts.txt`, and `channel_transcript.txt`. No commands were run. Findings use README section names and exact quotes as locators. A missing implementation or measurement is identified as unverifiable rather than assumed incorrect.

## Findings

1. [omission] **The prerequisites do not describe a host that can actually run the scripts.**

    **README (§7):** “Prerequisites: Python 3.12, `uv`, `curl`, `git`, and either a Docker daemon or a host that can run Switch natively”.

    **Why:** Both modes also require `make` and Linux process-management facilities. Native bootstrap has substantial additional requirements; it is not a general fallback for any machine without Docker.

    **Evidence:** `scripts/agents.sh` uses `mapfile`, `/proc/$pid/cmdline`, and `flock`. `scripts/targets.sh` also uses `pkill`. `scripts/bootstrap_switch.sh` invokes `openssl`, `apt-get`, `chown`, `su postgres`, and `npm ci`; it downloads `mattermost-team-${MM_VERSION}-linux-amd64.tar.gz` and defaults PostgreSQL data to `/var/lib/stranglerroom/pgdata`.

    **Exact replacement:**

    > Prerequisites: Python 3.12, `uv`, `make`, `curl`, `git`, OpenSSL, and a Linux environment with Bash supporting `mapfile`, `/proc`, `flock`, and `pkill`. Docker mode requires a reachable Docker daemon and the Docker Compose plugin. Native mode additionally requires an amd64 Linux environment compatible with the downloaded Mattermost binary, PostgreSQL 16, Node.js/npm for the Gateway build, and privileges for the script's package installation, ownership changes, and `su postgres` calls. Bootstrap requires network access to download the stack and dependencies.

2. [broken-command] **The template validation commands omit the Switch core virtual environment required in Docker mode.**

    **README (§9):** `.venv/bin/python scripts/validate_template.py` and `.venv/bin/python scripts/validate_template.py --run`.

    **Why:** These commands use Switch's offline template engine as well as the live service. Docker bootstrap does not prepare the host-side Switch core environment. Following the Docker quickstart and then these commands omits a necessary preparation step.

    **Evidence:** `.github/workflows/e2e.yml` explicitly says: “the template check runs Switch's own template engine offline, so it needs switch-core's venv (docker mode does not build one)”. It runs `uv sync --frozen --no-dev --project .switch-infra/core` before `.venv/bin/python scripts/validate_template.py --run`.

    **Exact insertion before the §9 commands:**

    > After Docker bootstrap, prepare the host-side Switch template engine with `uv sync --frozen --no-dev --project .switch-infra/core`. Native bootstrap prepares this environment itself.

    **Exact replacement for the §8 CI summary:**

    > CI has two jobs, with `e2e` depending on `unit`. The unit job runs `make install` and `make test`. The e2e job bootstraps Docker Switch, runs `make install` and `make e2e`, prepares `.switch-infra/core` with `uv sync --frozen --no-dev --project .switch-infra/core`, then runs `.venv/bin/python scripts/validate_template.py --run`. On failure or cancellation it uploads `.state/run/*.log` and `.state/ci/compose.log`.

3. [misleading] **Creating a template room does not make the running agents serve it.**

    **README (§9):** “To create the room from the Switch Console instead of `agents.provision`, import it under **Templates** and run it.”

    **Why:** This presents the template as an operational substitute for provisioning, but the agents serve the room recorded in `.state/room.json`. A new template room does not update that file. Merely choosing another room name also does not connect the running agents to it.

    **Evidence:** `strangler-room.template.yaml` warns: “Running agents … serve only the room recorded in `.state/room.json` by python -m agents.provision.” It also says Switch accepts duplicate active room names, but provisioning could then select either room. `agents/provision.py` writes the room ID, channel metadata, agent IDs, and human to that file.

    **Exact replacement:**

    > Importing and running the template creates a room containing the registered agents, but does not update `.state/room.json` or move running agent processes to that room. Before using the agents there, record the new room and channel metadata in `.state/room.json` and restart the agents. Avoid duplicate active room names: archive the provisioned room or choose another name. Switch permits duplicates, but provisioning can then select either room.

4. [misleading] **The automated demonstration scripts the human account's decisions.**

    **README (opening):** “A human in Mattermost governs the run and makes every decision.”  
    **README (§3):** “there is no orchestrator process.”

    **Why:** The interactive governance boundary is real, but `make demo` does not wait for a person to choose each decision. Its driver posts the prescribed decisions and approval using the human account. The unqualified absence of an orchestrator also obscures this driver.

    **Evidence:** `docs/AGENTS_SPEC.md` §3 says the driver logs into Mattermost as `user`, posts the migration command, posts `preserve D-1 D-2 D-4 and deprecate D-3 D-5`, and posts `approve-cutover`.

    **Exact replacement for the opening sentence:**

    > Governance commands are accepted only from a human-type Switch participant. In the automated demo, the driver posts the decisions and approval using the Mattermost `user` account; an interactive run lets a person make those choices.

    **Exact replacement for “there is no orchestrator process”:**

    > Agent handoffs use Switch messages; the demo driver scripts the human account's commands and verifies the resulting run.

5. [misleading] **Document ownership and a locally recomputable hash chain are overstated as comprehensive tamper evidence.**

    **README (opening):** “Every decision, verdict and flip is written to tamper-evident Switch room documents and sealed in a hash-chained audit ledger.”  
    **README (§3):** “hash = sha256(canonical(entry))”.

    **Why:** The matrix and scorecard are mutable documents, not individually hash-chained records. The ledger mirrors selected evidence when sealing; its 14-entry trail does not contain every campaign verdict or every later rollback. Its hashes are unkeyed and can be recomputed by a writer replacing the chain. The hash formula must also exclude the entry's own `hash` field. `verify_chain` alone does not authenticate the writer or check the separate sealing metadata.

    **Evidence:** `agents/ledger.py::append_entry` documents “entry without `"hash"`”. `verify_chain` removes `hash` before recomputation and checks only sequence, links, and hashes. `render_scorecard` separately renders `rollbacks`. The live trail has one `parity-verified` entry despite two campaigns. `docs/AGENTS_SPEC.md` describes rollback recording in the scorecard without a new ledger-sealing handoff.

    **Exact replacement for the opening claim:**

    > Agents record campaigns and governance actions in their own room documents. At cutover, the translator mirrors the approved parity, benchmark, and approval into a SHA-256 hash-chained ledger. Later rollbacks are recorded in the scorecard.

    **Exact replacement for the §3 hash-chain explanation:**

    > Each trail entry stores `prev` and `hash = sha256(canonical(entry without its hash field))`. Sealing stores `{at, head_hash, entries}`. The driver independently checks the chain through the Gateway. These checks detect inconsistencies against the recorded hashes; they do not prevent an authorized writer from replacing a chain and recomputing its hashes.

6. [misleading] **Creator-only updates do not establish isolation from a compromised local agent process.**

    **README (§3):** “A compromised fuzzer therefore cannot rewrite the ledger.”

    **Why:** This is supported only for requests authenticated with the fuzzer's own credentials. The local deployment launches agents under the same calling account and shares local configuration and credential storage. No process or credential isolation is demonstrated.

    **Evidence:** `scripts/agents.sh` launches every agent with the same `$PYTHON`, repository root, and calling account. `docs/AGENTS_SPEC.md` places agent credentials in `.state/agents/<name>.json` and describes shared settings containing Gateway admin credentials. `scripts/bootstrap_switch.sh` writes those credentials to `.state/switch.env`.

    **Exact replacement:**

    > Requests authenticated as the fuzzer cannot update the translator-owned ledger under Switch's creator-only rule. This local deployment does not demonstrate isolation of agent processes or their stored credentials.

7. [misleading] **The opening describes a local reconstruction as an actual Open Library migration, and synthetic probes as live traffic.**

    **README:** “Three Switch agents migrate Open Library's legacy web.py endpoints to FastAPI” and “The `fuzzer` diffs legacy against modern under live traffic.”

    **Why:** The supplied contract describes a local Open Library-shaped application, an infogami shim, and fixed seed data. The traffic consists of generated and deterministic requests sent directly to the two targets; no production traffic capture or proxy traffic mirroring is shown.

    **Evidence:** `docs/SPEC.md` §1 defines fixed users, lists, editions, and prices. §5 describes a “tiny `delegate.page` base class + registry”. §7 defines deterministic probes, Hypothesis-generated GETs, and sequential POSTs. The transcript calls the campaign a “deterministic quirk corpus + 120 generated GETs”.

    **Exact replacement for the opening first sentence:**

    > Three Switch agents demonstrate migration of a local Open Library-shaped web.py lists contract to FastAPI, behind a strangler proxy.

    **Exact replacement for the fuzzer sentence:**

    > The `fuzzer` sends synthetic probes to the locally running legacy and modern services and compares their responses.

8. [wrong] **The claimed seven consecutive successful end-to-end runs contradict the run report.**

    **README (§11):** “Passed 7 times in a row, the last 3 on the final code”.

    **Why:** Run 1 hung. Runs 2–7 are six successful runs, and the first two successes did not have 35 checks.

    **Evidence:** `readme_facts.txt`: “runs 2-4 also exit 0 (27/27, 28/28, 35/35 on interim code); run 1 hung (fixed bug)”; runs 5–7 passed 35/35, with runs 5–6 fresh and run 7 cinema/fresh.

    **Exact replacement:**

    > Runs 2–7 exited 0 after run 1 hung and its bug was fixed. Runs 2 and 3 passed 27/27 and 28/28 checks; runs 4–7 passed 35/35. The last three ran on the final code: two with `--fresh` and one with `--cinema --fresh`.

9. [misleading] **The HACP tally mixes distinct tasks, sessions, and result records.**

    **README (§12):** “19 tasks settled. 3 escalated or were stopped”.

    **Why:** Nineteen distinct task IDs have a settled session, so that number is supportable with that definition. However, the result table contains only 17 settled task records. Five distinct task IDs have escalated sessions, and three have sessions with no close. Several retried tasks appear in more than one outcome category. An unclosed session does not prove that it was stopped.

    **Evidence:** Counting `readme_facts.txt` directly:
    - Results: 18 task IDs, comprising **17 settled and 1 escalated**.
    - Sessions: 28 sessions, comprising **20 settled, 5 escalated, and 3 open/no-close**.
    - The 20 settled sessions represent **19 distinct task IDs**, because `bench-lib` has two settled sessions.
    - Escalated task IDs: `bench-lib`, `legacy-target`, `integrate-loop-adversarial-review`, `legacy-introspect`, `smoke-codex`.
    - Open/no-close task IDs: `bench-lib`, `integrate-live-e2e-targets-sh-test`, `legacy-introspect`.
    - `bench-lib` and `proxy-router` have settled sessions but no entry in the supplied results table.

    **Exact replacement:**

    > The session table records 28 sessions: 20 settled, 5 escalated, and 3 without a close. Nineteen distinct task IDs have a settled session. The results table contains 18 task records: 17 settled and 1 escalated. Five distinct task IDs had an escalated attempt and three had an unclosed attempt; these categories overlap because of retries. Three task IDs have no settled session: `legacy-target`, `integrate-loop-adversarial-review`, and `integrate-live-e2e-targets-sh-test`; the latter two have separately named settled replacements.

10. [wrong] **The peer swarm was not exclusively non-Claude.**

    **README (§12):** “delegated well-specified modules and reviews to non-Claude peers”.

    **Why:** The table itself includes Claude Opus workers, and their participation is corroborated by the session records. The supplied records also do not independently establish who led the build.

    **Evidence:** `readme_facts.txt` lists worker `agy.claude-opus-4-6-thinking` for the template, legacy introspection, and an escalated loop review. The template and one legacy introspection session settled.

    **Exact replacement:**

    > Development used HACP workers running Codex, OpenCode, Gemini, and Claude Opus models for modules and reviews.

11. [misleading] **The known quirks and their demonstration ordering are deliberately encoded.**

    **README (§4):** “The fuzzer finds and numbers these itself … They are not hard-coded.”

    **Why:** IDs are assigned by the registry rather than embedded in the classifier, but the five known classifications and deterministic corpus ordering are explicitly designed around these quirks. The blanket statement denies that distinction.

    **Evidence:** `docs/SPEC.md` §7 enumerates classification rules for the five tags and says the deterministic corpus “is ordered D-1…D-5 as in §3, so the five known quirks get D-1..D-5”.

    **Exact replacement:**

    > The classifier contains rules for these five known quirk tags. A persistent registry assigns divergence IDs in order of discovery; the deterministic demo corpus is ordered so these tags receive D-1 through D-5. Other differences receive additional IDs.

12. [omission] **The manual instructions need a fresh migration state and explicit waits.**

    **README (§7):** “With the stack, targets and agents up, post in the channel as `user`”, followed by migration, decisions, approval, and rollback.

    **Why:** After the preceding `make demo`, the ledger is sealed and the proxy targets modern. That is not the fresh state needed to reproduce the demonstration. Posting the listed commands together also races the campaign and benchmark prerequisites.

    **Evidence:** `docs/AGENTS_SPEC.md` requires an active unsealed ledger for decisions, approved parity and a current benchmark for approval, and prevents new builds while a cutover is active. `scripts/targets.sh start --fresh` removes generated code, router state, and the divergence registry; `agents/provision.py --fresh-room` archives the old room.

    **Exact replacement:**

    > To reproduce the flow by hand after a previous demo, leave Switch running and prepare a fresh run from the repository root: run `make agents-stop`, `PYTHON=.venv/bin/python bash scripts/targets.sh start --fresh`, `PYTHONPATH=. .venv/bin/python -m agents.provision --fresh-room`, and `make agents`. Open the newly recorded channel. Send the migration command first; wait for the divergence table before sending decisions, then wait for approved parity and the benchmark's approval request before sending `approve-cutover`. Send `rollback` separately if you want to return to legacy.

13. [wrong] **`clean-state` does not stop Switch.**

    **README (§7 target table):** “`make stop` / `clean-state` | stop everything / also wipe `.state/` except `switch.env`”.

    **Why:** This implies `clean-state` performs `stop` and then deletes state. It stops only the agents and targets; Switch remains running.

    **Evidence:** `Makefile::stop` invokes `agents-stop`, target stop, and `switch-down`. `clean-state` invokes only agent stop, target stop, and removal of `.state` children other than `switch.env`.

    **Exact replacement:**

    > `make stop` stops agents, targets, and Switch. `make clean-state` stops agents and targets, then removes `.state/` contents except `switch.env`; it leaves Switch running.

14. [broken-command] **The operational Gateway instructions hard-code the native port despite supporting Docker.**

    **README (§7):** “Forward ports **18065** … and **18000**”; “open the Switch Gateway at `http://localhost:18000`”.  
    **README (§13):** `curl -s localhost:18000/health`.

    **Why:** Docker defaults to port 8000. Both modes also support `API_HOST_PORT`, so the configured URL is authoritative.

    **Evidence:** `scripts/bootstrap_switch.sh::prepare_env` sets Docker `API_PORT="${API_HOST_PORT:-8000}"` and native `API_PORT="${API_HOST_PORT:-18000}"`. `write_state_env` writes the resulting `SWITCH_URL`.

    **Exact replacement for the recording instructions:**

    > Forward the configured Mattermost port (default 18065) and Switch Gateway port (native default 18000, Docker default 8000). Open the Gateway at the configured `SWITCH_URL`, recorded in `.state/switch.env` unless overridden in the environment.

    **Exact replacement for the health command description:**

    > Check the configured `SWITCH_URL` at `/health`: the default command is `curl -s localhost:18000/health` for native mode or `curl -s localhost:8000/health` for Docker mode.

15. [wrong] **A pre-flight failure does not uniquely mean Switch is down.**

    **README (§13):** “`make e2e` prints `PRE-FLIGHT FAILED` and exits 2 | Switch is not up”.

    **Why:** Pre-flight also starts fresh targets, provisions the room, restarts agents, and waits for live agents. Those operations can fail independently.

    **Evidence:** `docs/AGENTS_SPEC.md` §3 lists all these pre-flight stages. `scripts/targets.sh::ports_free` explicitly returns failure for untracked listeners.

    **Exact replacement:**

    > Read the reported pre-flight error. Check Switch health at the configured URL, target port conflicts, provisioning credentials, and agent startup logs. If Switch is down, run `make switch-up` and retry.

16. [broken-command] **`localhost:900X` is not a valid runnable URL, and the suggested status command does not identify a listener's process.**

    **README (§13):** “Find it with `bash scripts/targets.sh status` / `curl -s localhost:900X/health` and stop it”.

    **Why:** `900X` is not a numeric port. The status script prints HTTP responses, not process ownership; it cannot tell the user which untracked PID to stop.

    **Evidence:** `scripts/targets.sh` status loops over `9001:/health`, `9002:/_meta`, and `9000:/_router/status`, using only `printf` and `curl`.

    **Exact replacement:**

    > Run `bash scripts/targets.sh status` to probe legacy `:9001/health`, modern `:9002/_meta`, and proxy `:9000/_router/status`. This reports HTTP status data, not listener ownership. Inspect the busy port with the host's socket/process tools and identify its owner before stopping it and retrying.

17. [wrong] **Reload mode uses a supervisor and a serving worker, not a single process.**

    **README (§6):** “The modern target is a single uvicorn process in `--reload` dev mode”.

    **Why:** The scripts explicitly account for the reload worker as a child process. A single serving worker is different from a single operating-system process.

    **Evidence:** `scripts/targets.sh::stop_one` says “uvicorn --reload spawns a child worker” and invokes `pkill -TERM -P "$pid"`. `readme_facts.txt` confirms the reload setting and cheroot's `numthreads=30`.

    **Exact replacement:**

    > By default, modern runs one uvicorn serving worker under a reload supervisor, while legacy uses cheroot with 30 threads.

18. [unverifiable] **The four-core host claim has no identified measurement.**

    **README (§6):** “The load generator and both servers also share 4 CPU cores.”  
    **README (§11):** “~4 min on 4 cores”.

    **Why:** No labeled CPU topology, affinity, or container quota output establishes four cores. The standalone `96` in the grep facts is unlabeled and cannot establish either a four-core or a 96-core allocation.

    **Evidence:** `readme_facts.txt` gives elapsed test times and server settings, but no identified CPU-allocation command or result.

    **Evidence needed:** CPU affinity and container quota information for the measured run, with the meaning of “cores” stated.

    **Exact replacements:**

    > The load generator and both servers run on the same dev host. This is a local development setup, not a controlled comparison of production deployments.

    > `make test` → 737 passed in 257.73 s in the reported run.

19. [omission] **The timing summary needs to distinguish a warm driver run from bootstrap and from the displayed transcript.**

    **README:** “about 23 s for all 8 steps (about 34 s wall clock with pre-flight)”.

    **Why:** The facts support these approximate timings for particular runs, but they do not include cold Switch bootstrap or dependency installation. The displayed migration's transcript spans 57.064 seconds from the human migration command to the sealed-ledger post.

    **Evidence:** `readme_facts.txt`: “Wall: ~34-35 s fresh, ~83 s cinema. Step 8 completes at 22.6 s in run 5.” `Makefile::demo` runs `switch-up` before `e2e`. Transcript timestamps are `1793861137087` for the migration request and `1793861194151` for the sealed post.

    **Exact replacement:**

    > With dependencies installed and Switch already running, reported fresh driver runs took about 34–35 s including driver pre-flight; run 5 reached step 8 at 22.6 s. Cinema took about 83 s. These timings exclude dependency installation and cold Switch bootstrap. The transcript shown below is a separate recorded run.

20. [omission] **The request-count convention should distinguish paired probes from HTTP requests.**

    **README:** “162 shadow requests per campaign”.

    **Why:** The reported 162 are comparison probes sent to both targets. They are not 162 HTTP requests total. Resets and readiness checks add further traffic.

    **Evidence:** `docs/SPEC.md` §7 describes requests to “both targets per request” and resets “on both targets before each” sequential POST. The transcript reports two 162-probe campaigns. The benchmark summary records 500 requests for each target.

    **Exact insertion after the live-result summary:**

    > Each campaign's 162 reported requests are paired comparison probes: 324 HTTP requests to contract endpoints, plus reset and readiness traffic. The two campaigns therefore contain 324 paired probes and 648 contract-endpoint requests. The benchmark makes another 1,000 requests, 500 per target; these figures are not the total HTTP traffic for the entire demo.

21. [unverifiable] **The absolute guarantee about accidental mentions is not established.**

    **README (§5):** “so an instruction can never accidentally wake an agent.”

    **Why:** The recorded human prompts follow the intended convention, but the supplied material does not show every posting path applying mention defusing. Defining a helper does not establish its universal use.

    **Evidence:** `agents/commands.py::defuse_mentions` inserts a U+2060 word joiner, but the runtime and posting call sites are absent. The same file also contains mention-bearing strings such as `MIGRATE_EXAMPLE` and `decision_template`.

    **Evidence needed:** Runtime posting code, all relevant callers, and tests covering quoted commands and error messages.

    **Exact replacement:**

    > The recorded prompts to the human name agents without active agent mentions. Deliberate handoffs use targeted mentions. `agents.commands.defuse_mentions` is available to make quoted handles inert.

22. [unverifiable] **The claimed live restart observation is missing.**

    **README (§3):** “A restarted agent rebuilds its state from the room documents and posts nothing (verified live: 0 new posts).”

    **Why:** Rehydration is described in the agent contract, but neither live-facts file contains a restart observation or before/after post counts.

    **Evidence:** `docs/AGENTS_SPEC.md` specifies rehydration on connection for the fuzzer and cutover gate. `readme_facts.txt` only reports running PIDs; the supplied transcript contains no restart interval.

    **Evidence needed:** The restart action, affected agents, observation window, and channel post counts or transcript before and after it.

    **Exact replacement:**

    > The fuzzer and cutover gate are specified to rehydrate their state from room documents on connection.

23. [unverifiable] **The exact HTTP 403 claim is stronger than the supplied ownership evidence.**

    **README (§3):** “Switch returns 403 when anyone other than the creator calls `update_room_document`.”

    **Why:** Creator-only updates are explicitly documented, but no supplied Switch handler or denied-request output verifies that exact status code for this operation and all non-creator identities.

    **Evidence:** `agents/ledger.py` says “only the creating agent [may] update a room document”; `docs/AGENTS_SPEC.md` calls this a “live-verified creator-only rule”. Neither supplies the claimed 403 response.

    **Evidence needed:** A denied update's status and response, or the relevant Switch authorization handler.

    **Exact replacement:**

    > Switch's documented creator-only rule permits each agent to update its own room document; other room members read it through `load_documents`.

24. [unverifiable] **The `--offline-only` flag is not corroborated.**

    **README (§9):** `.venv/bin/python scripts/validate_template.py --offline-only`.

    **Why:** The validator's argument parser is not supplied. The default invocation has a live result, and `--run` is documented and used in CI, but `--offline-only` appears only in the README.

    **Evidence:** `readme_facts.txt` records default validation. `.github/workflows/e2e.yml` invokes `--run`. The template header documents `[--run]`.

    **Evidence needed:** `scripts/validate_template.py`, its `--help` output, or an actual successful `--offline-only` execution.

    **Exact replacement:** Delete the `--offline-only` command until its flag and behavior are verified. Retain:

    > `.venv/bin/python scripts/validate_template.py` — offline engine plus live Gateway validation.  
    > `.venv/bin/python scripts/validate_template.py --run` — additionally create, check, and archive the validation room.

25. [unverifiable] **The timeout troubleshooting command assumes an executable Python script and an unsupported timing characterization.**

    **README (§13):** “`scripts/run_e2e_demo.py --step-timeout 480` (default 240 s; the slowest step, the benchmark, takes about 9 s)”.

    **Why:** The supplied material does not establish the script's executable bit or interpreter selection. The supported Makefile invocation explicitly uses the project Python. There is also no step-duration breakdown proving the benchmark is the slowest step or takes about nine seconds. The benchmark's two request durations sum to 7.9367 seconds, excluding surrounding work.

    **Evidence:** `Makefile::e2e` uses `$(PY) scripts/run_e2e_demo.py --fresh`. `docs/AGENTS_SPEC.md` documents the default 240-second step timeout. The sealed benchmark reports `duration_s: 2.765` and `5.1717`.

    **Exact replacement:**

    > Run `.venv/bin/python scripts/run_e2e_demo.py --fresh --step-timeout 480` to increase the default 240 s per-step timeout. Actual step duration depends on the host and workload.

26. [unverifiable] **Some peer deliverables and the “11 findings” resolution tally lack supporting artifacts.**

    **README (§12):** “`integrate-live-e2e.md` (11 findings: 9 fixed, 2 declined with reasons)”; the bench and proxy peer rows.

    **Why:** The results table verifies the review's merged path, not its contents or subsequent fixes. Settled `bench-lib` and `proxy-router` sessions exist, but their artifact manifests and merged paths are absent from the results table.

    **Evidence:** `readme_facts.txt` records `integrate-live-e2e-review | settled | ['.hacp/reviews/integrate-live-e2e.md']`. It contains no review text, fix mapping, or decline reasons. There are no `bench-lib` or `proxy-router` result rows.

    **Evidence needed:** The review and disposition records; the bench/proxy artifact manifests or result records.

    **Exact replacement for the review annotation:**

    > `integrate-live-e2e.md` (settled review artifact; finding dispositions are not included in the supplied record).

    **Exact replacement for the bench/proxy artifact attribution:**

    > The session table records settled `bench-lib` work by Gemini and settled `proxy-router` work by OpenCode; their merged artifact paths are not included in the supplied results table.

27. [unverifiable] **The detailed HACP enforcement guarantees cannot be audited from the supplied files.**

    **README (§12):** “Only its declared outputs that pass acceptance are merged back, and only if the real file did not change in the meantime”, together with the detailed protocol sequence.

    **Why:** The agent contract states the intended isolated-workspace and acceptance behavior, but the harness implementation is absent. The result table does not prove snapshot isolation, measured verification, or a concurrent-file-change guard.

    **Evidence:** `docs/AGENTS_SPEC.md` says peers work in an isolated workspace and only declared outputs passing acceptance are merged. `tools/hacp_delegate.py` itself is not supplied.

    **Evidence needed:** The harness implementation, acceptance logs, artifact manifests, and the implementation or tests of the concurrent-change check.

    **Exact replacement:**

    > Development used HACP contracts, session records, and result records. The supplied records identify worker outcomes and merged paths. The development contract requires isolated workspaces and acceptance checks; the harness's detailed enforcement is not demonstrated here.

28. [unverifiable] **Collection counts do not establish the stated test behavior or hygiene guarantees.**

    **README (§11):** “all ports come from `tests.procs.free_port()`”, “unit tests never touch the live Switch”, and the full-loop “restart and replay” description.  
    **README (§10):** “FakeRoom … with faithful Switch semantics”.

    **Why:** The collection report establishes counts and selected integration cases, not test bodies, fixture behavior, network destinations, or FakeRoom fidelity. The contract forbids live Switch access from unit tests, but a prohibition is not an audit of compliance.

    **Evidence:** `pytest.ini` defines the `integration` marker but does not enforce network isolation. `docs/AGENTS_SPEC.md` says “Never register/re-register Switch agents, create rooms, or post … from a unit test.” No test implementations, `tests/procs.py`, or `tests/fakes.py` are supplied.

    **Evidence needed:** Those test and fixture sources, plus the relevant loop/restart/replay assertions.

    **Exact replacement:**

    > The collected suite contains 737 non-integration cases and two integration cases. The project contract reserves live Switch use for integration tests and the demo driver. The collection report alone does not verify fixture isolation, port allocation, restart/replay coverage, or FakeRoom fidelity.

29. [misleading] **The migration guide does not establish the asserted absence of upstream decision or approval records.**

    **README (§1):** “nobody records whether the port should keep it or fix it” and “nothing shows which behaviour was approved, by whom”.

    **Why:** A temporary comparison script does not itself provide a durable approval record, but the supplied guide excerpt does not show that Open Library lacks issue discussions, reviews, CI evidence, or other records.

    **Evidence:** `readme_facts.txt` quotes the guide's temporary script, `curl` comparison, and “We will delete it before merging.” It contains no evidence about all other upstream recording practices.

    **Exact replacement for the three-gap introduction and bullets:**

    > A temporary comparison script alone does not provide systematic edge-case coverage, a durable record of keep-or-fix decisions, or an attributed cutover approval. StranglerRoom demonstrates those records for its local migration contract.

30. [omission] **The upstream issue's known closed status should be disclosed when presenting it as the motivating task.**

    **README (§1):** “the lists endpoints are [internetarchive/openlibrary#12487]”.

    **Why:** The supplied live facts explicitly report the issue as closed. The README does not claim it is open, but its present-tense framing can suggest an outstanding task this project is completing. Closure does not establish that all Open Library migration work is finished.

    **Evidence:** `readme_facts.txt`: `internetarchive/openlibrary#12487 title: 'Migrate lists endpoints to fastapi' (CLOSED)`.

    **Exact replacement:**

    > This demo is inspired by Open Library's web.py-to-FastAPI migration and issue [internetarchive/openlibrary#12487](https://github.com/internetarchive/openlibrary/issues/12487), “Migrate lists endpoints to fastapi”, reported closed in the supplied 2026-11-05 upstream facts.

31. [misleading] **The header-inspection command relies on an unverified capitalization.**

    **README (§7):** `curl -si localhost:9000/people/george08/lists.json | grep X-Strangler-Upstream`.

    **Why:** HTTP header names are case-insensitive, but this grep is case-sensitive. The supplied material establishes the header's logical name, not the capitalization emitted on the wire; the command can discard the header it is meant to show.

    **Evidence:** `docs/SPEC.md` §6 specifies the `X-Strangler-Upstream` response header. No raw `curl -i` output or proxy response implementation is supplied.

    **Exact replacement:**

    > `curl -si localhost:9000/people/george08/lists.json | grep -i '^X-Strangler-Upstream:'`

32. [omission] **The stored channel URL is a Mattermost deep link, not the browser URL shown earlier.**

    **README (§7):** “The current channel URL is always available with” the Python command printing `channel_url`.

    **Why:** The command correctly prints the stored value, but in the recorded state it uses `mattermost://`. A judge expecting the preceding HTTP browser link needs that distinction. The command also requires provisioning to have created `.state/room.json`.

    **Evidence:** `readme_facts.txt` records `"channel_url": "mattermost://localhost:18065/switch/channels/strangler-migration-64d6cc4c"`.

    **Exact replacement:**

    > After provisioning, `.state/room.json` contains the channel's Mattermost deep link. For the default local deployment, print a browser URL with `.venv/bin/python -c "import json; print(json.load(open('.state/room.json'))['channel_url'].replace('mattermost://', 'http://', 1))"`. The demo driver also prints an HTTP channel URL.

33. [misleading] **The numbered flow places the governance refusal before benchmarking, unlike the recorded post order.**

    **README (§5):** Step 6 says the fuzzer's approval attempt is “refused”; step 7 then says the cutover gate “benchmarks legacy then modern”.

    **Why:** The probe was sent before the benchmark card, but its refusal was posted after the benchmark card and approval request. Driver wait-step numbering should not be presented as the chronological order of the gate's work.

    **Evidence:** `channel_transcript.txt` timestamps: probe `1793861171474`; benchmark card `1793861178879`; approval request `1793861181042`; refusal `1793861183265`; human approval `1793861186624`.

    **Exact replacement for steps 6 and 7:**

    > 6 | fuzzer / driver | Approved parity hands off to the cutover gate; the driver sends the agent-originated `approve-cutover` governance probe.  
    > 7 | cutover-gate | Finishes the benchmark and asks for human approval, then refuses the queued agent approval. The driver verifies that the proxy remains on legacy before sending the human-account approval.

## Checked and fine

- **Dependency versions:** `requirements.txt` pins FastAPI `0.115.12` and web.py `0.62`, matching the headline. Python `3.12` is the configured version in `docs/SPEC.md` and both CI jobs. Bootstrap pins Switch `0.31.0`, tag `switch-v0.31.0`, and native Mattermost `11.9.0`. These establish configuration and pins; they are not a complete runtime package inventory.

- **Temporary comparison-script motivation:** The quoted upstream facts support `test_<endpoint>_compare.sh`, comparison using `curl` against old and new endpoints, and deletion before merging. The issue title is correctly quoted. The broader absence-of-records claims are addressed above.

- **Default architecture and routing state:** `docs/SPEC.md` specifies legacy `9001`, modern `9002`, proxy `9000`, native Switch `18000`, Docker Switch `8000`, and Mattermost `18065`. It specifies loopback target binding and `.state/router.json`. Live status confirms the proxy targeting `http://127.0.0.1:9002`.

- **Room and membership:** `agents/provision.py` declares `translator`, `fuzzer`, `cutover-gate`, room `strangler-migration`, and human `user`. Its implementation creates or reuses the room, attaches missing members, and saves `.state/room.json`. The live facts provide the same room, agents, and human.

- **Addressing-policy preparation:** `ensure_agents` calls `SwitchGateway.set_addressing_policy(c.agent_id, None)` for every agent. Its docstring specifies `PUT /gateway/agents/{id}/addressing-policy {"policy": null}` and explains the bootstrap-owner restriction. This matches the README's provisioning description.

- **Document names and intended ownership:** `agents/ledger.py` defines exactly “Parity & Cutover Audit Ledger”, “Parity Audit Matrix”, and “Cutover Scorecard”. `docs/AGENTS_SPEC.md` assigns them respectively to translator, fuzzer, and cutover gate and documents creator-only updates and cross-agent reads.

- **Ledger mechanics:** `canonical` sorts JSON keys and uses compact separators; `digest` computes SHA-256 over that JSON. `append_entry` includes sequence, actor, event, evidence, source message ID, and previous hash. `verify_chain` checks sequence, links, and recomputed hashes. The ownership and authentication limits are addressed in the findings.

- **SSE and heartbeat configuration:** `docs/AGENTS_SPEC.md` specifies one stream with `scope=single&filter=addressed&rooms=<room>`, reconnect behavior, and sequence deduplication. The grep facts independently show `HEARTBEAT_INTERVAL_S = 2.0`.

- **Human-only command contract and observed refusal:** `agents/commands.py` documents the four governance command families and parses the shown commands. `docs/AGENTS_SPEC.md` specifies classification by participant `type == "user"`. The transcript demonstrates rejection of the fuzzer's approval and subsequent acceptance of `user`'s approval. It does not independently exercise every governance verb.

- **Approval attribution:** The transcript and sealed facts agree on approval message `sw_baee9d06bf00f6421c7b2f8128a0aa8c`, approver `user`, and modern cutover. The sealed facts explicitly contain `approval_message_verified: true` and `verified: true`. Message/event-ID equivalence is also explicitly recorded as live-verified in `docs/AGENTS_SPEC.md`.

- **Five-quirk table:** Every tag, legacy behavior, naïve-modern behavior, and demo decision matches `docs/SPEC.md` §§2–3: `.json` aliases, loose query coercion, missing-list 200 versus 404, quantities −10 through 0, and empty seeds-edit 500 versus 422. Warehouse credit-reversal semantics are explicitly part of this local contract.

- **Route count and generated file:** The transcript lists five method/path operations, including separate GET and POST seeds operations. Live `/_meta` lists those five operations and their five `.json` aliases, plus `/health`. `.gitignore` explicitly excludes `targets/modern_fastapi/app/lists.py`; the build contract identifies it as translator-generated.

- **Displayed migration and quotations:** The migration ID `MIG-20261105-064537`, v1 revision `eada3f1503f1`, v2 revision `333b5f94c9df`, decision text, quoted campaign results, refusal, cutover, and sealing excerpts match `channel_transcript.txt`. The explanatory attribution of the governance probe to the driver is supported by `docs/AGENTS_SPEC.md`.

- **Campaign arithmetic:** Campaign 1 has `31 + 131 = 162`; its cluster hits sum to `62 + 31 + 29 + 4 + 5 = 131`. Campaign 2 has `97 + 65 = 162`; remaining divergent hits sum to `58 + 7 = 65`. Thus approved parity does not mean identical responses: campaign 2 has 97 identical probes and 65 approved deviations.

- **Meaning of approved parity:** `docs/SPEC.md` defines approval as all known clusters resolved under preserve/deprecate decisions, with no unresolved randomized differences. Preserved clusters must match; deprecated clusters must consistently retain the accepted modern status. This supports “100% Approved Parity (5/5)” for the reported campaign, rather than a claim of universal input equivalence.

- **Sealed result:** The room ID `3e2ac25a-a414-4efb-910c-bae6e3427cea`, decision message `sw_81b1806b802970315e222b7d5aef3eaf`, five human decisions, 14-entry trail, and full head `8f89b3b8cf7b49f57955b5edc13d95ecce7fbf04d2bc9fa272e91d251748071c` match the live facts and transcript. The recorded chain check is `(True, '14 entries, head 8f89b3b8cf7b')`.

- **Benchmark workload:** The sealed facts record 500 requests per target, concurrency 50, and five explicit GET paths. `docs/AGENTS_SPEC.md` and the transcript specify sequential execution, legacy first. This is 1,000 benchmark requests total.

- **Benchmark measurements:** The README's p50, p95, p99, and throughput values match the displayed transcript: legacy `66.5 / 1096.8 / 1912.2 ms`, `180.8 req/s`; modern `334.9 / 1414.5 / 2193.6 ms`, `96.7 req/s`. Both have 500 successful 200 responses and zero errors. The reported p99 increase of 14.7% and throughput decrease of 46.5% agree with the sealed comparison. The underlying durations also support approximately `500 / 2.765 = 180.8` and `500 / 5.1717 = 96.7 req/s`.

- **Performance interpretation:** “Modern is slower” accurately describes this measured run. The README appropriately avoids turning that result into a general claim about FastAPI. Cheroot's 30-thread configuration is directly confirmed by the grep facts; worker/process wording and the unsupported core count are addressed above.

- **Make targets and execution order:** `Makefile` confirms `install`, `switch-up`, `switch-down`, `targets`, `provision`, `agents`, `agents-stop`, `test`, `test-integration`, `e2e`, `demo`, `cinema`, `stop`, and `clean-state`. `demo` orders Switch startup, fresh targets, provisioning, agents, and fresh e2e. `e2e` passes `--fresh`; `cinema` passes `--cinema --fresh`. `PY` defaults to `.venv/bin/python`.

- **Operational scripts:** `scripts/agents.sh` implements start, stop, restart, and status with logs/PIDs under `.state/run` by default. It verifies the exact `-m <module>` pair before signaling. `scripts/targets.sh` additionally verifies the port, uses a lock, and refuses occupied untracked ports during `--fresh`. Fresh startup removes generated modern code, router state, and the divergence registry.

- **Cinema contract and measured duration:** `docs/AGENTS_SPEC.md` specifies `STRANGLER_AGENT_PACE=2`, three-second pauses, a ten-second countdown, and an HTTP channel URL. The facts report approximately 83 seconds for cinema. `make cinema` itself requires the already installed dependencies and running Switch stack.

- **Credentials and settings:** Bootstrap writes the exact advertised Gateway and Mattermost credential keys to `.state/switch.env`, with mode 600. `docs/AGENTS_SPEC.md` specifies that `Settings.load()` gives environment variables precedence over that file. The live room's human is `user`.

- **Bootstrap mode selection:** `scripts/bootstrap_switch.sh` selects Docker only when the Docker command exists and `docker info` succeeds; otherwise it selects native mode. It accepts `SWITCH_MODE`, checks out the stated tag in both modes, runs the upstream seeder, and writes the state environment. The Docker Compose file and both profile names match the README.

- **CI source:** The workflow has two jobs, Python 3.12, `make install`, `make test`, Docker bootstrap, fresh e2e, and live template validation. It collects Compose logs and uploads those plus `.state/run/*.log` on failure or cancellation, includes hidden files, and retains artifacts for seven days. No successful CI execution is established by the supplied live facts.

- **Template structure and default validation:** `strangler-room.template.yaml` has `version: 2`, one room resource, the three registered agent names, conditional creator membership when a bridge is selected, no document resources, and an unaddressed kickoff. The live facts report `ok=True blocked=False errors=0 warnings=0` and `strangler-room.template.yaml: PASS (offline, live)` for default validation.

- **Test totals:** The 24 supplied per-file collection counts sum to **739**. Exactly two collected cases are selected by `-m integration`, leaving **737** non-integration cases. The full-run results are **737 passed, 2 deselected in 257.73 s** and **2 passed, 737 deselected in 33.60 s**.

- **Test category arithmetic:** Legacy contract/adversarial cases are `39 + 32 = 71`; codegen 277; divergence 24; agent cases `25 + 17 + 27 + 29 = 98`; driver 86; ops scripts `3 + 30 + 9 = 42`; offline template cases `40 − 1 = 39`; CI workflow 12. These displayed categories total 649; the ellipsis accounts for the remaining 88 non-integration cases. The loop/parity row's count is `1 + 2 = 3`. The 737 non-integration cases occur across all 24 files because `test_switch_integration.py` contains two collected cases but only one selected integration case.

- **Supported HACP attribution:** Joining session workers to result task IDs corroborates the listed Codex Mattermost, LLM synthesis, CI, and review artifacts; OpenCode proxy-control, ops-script, fuzzer-edge, target-script-test, and loop-review artifacts; and Claude Opus legacy-introspection and template artifacts. Bench/proxy merged-path evidence and the review-disposition tally are limited as described above.

- **Remaining troubleshooting contract:** The addressing-policy recovery matches provisioning code. Fresh-room archiving matches `agents/provision.py`. Human rollback, LLM rejection with deterministic fallback, and the default 240-second step timeout are specified in `docs/AGENTS_SPEC.md`. `make clean-state` preserves `.state/switch.env`, as the Makefile states.
