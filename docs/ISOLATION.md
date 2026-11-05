# Per-component OS users ("isolation")

Status: design spec (implementation in progress on branch `per-user-isolation`).

## 1. Why

Without isolation every StranglerRoom process runs as the operator (root on the demo host). Any one of them, or code
it executes (the translator-generated `app/lists.py` running inside the modern server, an LLM-synthesised module),
can then read every secret on the host:

* the other agents' Switch API keys (`.state/agents/*.json`), so it can speak as another agent;
* the router token (`.state/router.token`), so it can flip production with `POST /_router/flip`;
* `.state/switch.env`, which holds the Switch registration token, the gateway admin password **and the human lead's
  Mattermost password**, so it can impersonate the human and issue the human-only verbs (`approve-cutover`, …).

Isolation runs every component as its own unprivileged Unix user, so the kernel's file permissions keep each secret
with its one legitimate holder.

| component | Unix user | supplementary groups | can read (besides code) | can write |
|---|---|---|---|---|
| translator | `sr-translator` | `stranglerroom`, `sr-prodlock` | its own API key, `room.json` | `.state/translator/`, its own files in `targets/modern_fastapi/app/` (sticky dir), `production.lock` |
| fuzzer | `sr-fuzzer` | `stranglerroom` | its own API key, `room.json` | `.state/fuzzer/` |
| cutover-gate | `sr-cutover-gate` | `stranglerroom`, `sr-prodlock` | its own API key, `room.json`, the router token (environment only) | `.state/cutover-gate/`, `production.lock` |
| proxy | `sr-proxy` | `stranglerroom` | the router token (environment only) | `.state/proxy/` |
| legacy | `sr-legacy` | `stranglerroom` | — | `.state/legacy/` |
| modern | `sr-modern` | `stranglerroom` | the generated `app/lists.py` | `.state/modern/` |

No component can read `switch.env`, `router.token`, another component's API key, the process logs (`.state/run/`,
0700), `.switch-infra/` or `.native/`. None can signal or ptrace another component's process (different uids).
None can write the code tree, except that the translator can add and replace **its own** files in the one
directory that holds the module it generates (sticky bit: it cannot replace `main.py` or `store.py`).

**Still trusted:** the operator (root), who runs bootstrap, provisioning, the launchers and the e2e driver.
**Out of scope:** Switch, Mattermost and Postgres themselves (unchanged; they authenticate their clients); other
services on the host; a component binding a port that happens to be free; per-user network egress rules (this
pod has no `CAP_NET_ADMIN`).

## 2. State layout (the same in both modes)

Every file a component writes lives in that component's own directory, so each directory can be owned by a
different user. Paths honour `STRANGLER_STATE_DIR` (default `<repo>/.state`).

```
.state/
  switch.env            operator only (0600 root). Agents never read it; they get SWITCH_URL from the launcher.
  router.token          operator only (0600 root). Proxy and gate receive the value in their environment.
  room.json             0644 root (room id, channel, names: not secret)
  production.lock       root:sr-prodlock 0660 when isolated
  agents/               0711 root when isolated (traverse only)
    translator.json     sr-translator 0600         (and fuzzer.json, cutover-gate.json likewise)
  translator/           sr-translator 0700   pending_refusals.json   (was .state/translator_pending_refusals.json)
  fuzzer/               sr-fuzzer 0700       divergence_registry.json (was .state/divergence_registry.json)
  proxy/                sr-proxy 0700        router.json              (was .state/router.json)
  legacy/               sr-legacy 0700       legacy_openlibrary.log   (was .state/legacy_openlibrary.log)
  cutover-gate/ modern/ their users, 0700    HOME only
  run/                  0700 root when isolated: pid files and logs (each process inherits its log descriptor)
```

The generated module stays at `targets/modern_fastapi/app/lists.py` (the path the ledger names). The translator
publishes it as mode 0644 (the modern server is another user), and API-key files are created 0600 from the start
(no window in which a temporary copy is readable by others).

## 3. Mode

`STRANGLER_ISOLATION` = `off` | `on` | `auto` (default `auto`).

* `auto`: on if and only if the marker `/etc/stranglerroom/isolation.json` (override: `STRANGLER_ISOLATION_MARKER`)
  exists and its `"repo"` equals this checkout's resolved root. A CI runner or another checkout never picks it up.
* `on` without a matching marker, or isolation on while the launcher is not root: fail loudly. Never fall back to
  running unisolated.
* The test suite forces `off` (autouse fixture), so tests never drop privileges or touch the host.

## 4. Launcher: `python -m agents.isolation exec <component> -- <argv…>`

`scripts/agents.sh` and `scripts/targets.sh` decide the mode once (`python -m agents.isolation mode` prints `on` or
`off`) and launch each process as

```bash
nohup ${ISOLATE:+"$PY" -m agents.isolation exec "$name" --} "$PY" -m "$module" … >>"$log" 2>&1 </dev/null 9>&- &
```

so with isolation off nothing changes, and with it on the recorded pid still ends up as the component itself (each
step `exec`s). When on, the launcher, as root:

1. **prepares** the component's files: symlink-safe (`O_NOFOLLOW`, `dir_fd` with `follow_symlinks=False`), never
   recursive. It refuses a symlinked or foreign-owned directory instead of following it.
   * `.state` exists, owned by root, not group- or other-writable;
   * its private directory: owned by the component's user, mode 0700. Entries directly inside it that root created
     (e.g. a `router.json` from an unisolated run) are `lchown`ed to the user;
   * agents: `.state/agents` 0711 root; its own `<name>.json` is owned by its user, 0600;
   * translator and gate: `.state/production.lock` root:sr-prodlock 0660 (created if missing);
   * translator: the module directory (parent of `MODERN_LISTS_PATH`, default `targets/modern_fastapi/app`) is
     root:sr-translator 1775, and an existing `lists.py` is owned by sr-translator and set to 0644;
2. builds a **minimal environment** from an allow-list instead of inheriting root's:
   * kept: `PATH`, `LANG`, `LANGUAGE`, `LC_*`, `TZ`, `TERM`, `PYTHONPATH`, `PYTHONUNBUFFERED`, `STRANGLER_*`,
     `LEGACY_URL`, `MODERN_*`, `PROXY_URL`, `ROUTER_*`;
   * always dropped: `ROUTER_ADMIN_TOKEN` and `ROUTER_TOKEN_FILE` (re-added below for proxy and gate only);
   * set: `HOME` = its private directory, `USER`/`LOGNAME`, `PYTHONDONTWRITEBYTECODE=1`;
   * agents: `SWITCH_URL` (from the environment, else read from `switch.env` as root, else the default);
   * proxy and cutover-gate: `ROUTER_ADMIN_TOKEN` = `resolve_router_token()` evaluated as root (the same rule both
     use unisolated), so the token never appears in a file they can read or in any argv (`/proc/<pid>/cmdline` is
     world-readable; `/proc/<pid>/environ` is not);
   * translator only: the variables named in `STRANGLER_TRANSLATOR_PASS_ENV` (comma-separated), e.g. an API key
     for LLM mode;
3. **drops privileges**: `setgroups(supplementary)`, `setgid`, `setuid` (real, effective and saved), then checks
   that `setuid(0)` fails, sets `umask 077`, and `execvpe`s the component.

## 5. Host setup and teardown (root, idempotent)

`python -m agents.isolation setup` (`make isolate`):

* creates the groups `stranglerroom` and `sr-prodlock` and the six system users (no login shell, home
  `/nonexistent`, primary group named after the user) with the memberships above;
* makes the path to the repository traversable by the service users. For each ancestor that "other" cannot
  traverse (on this host `/root`, 0700): if its group bits are empty, first remove "other" permissions from every
  **other** entry in that directory, then `chgrp stranglerroom` and `chmod g+x` (0710). Under a 0700 `/root` those
  "other" bits are unreachable today, so no existing access changes; afterwards the service users can reach the
  repository and nothing else in `/root`. An ancestor whose group bits are already in use makes setup refuse;
* removes group/other access from the repository's secret-holding, runtime-irrelevant directories:
  `.switch-infra/`, `.native/`, `.hacp/`;
* records every change (path, previous mode, previous group) in the marker, which it writes last.

`python -m agents.isolation disable` (`make isolate-off`) restores every recorded mode and group and removes the
marker; `--remove-users` also deletes the users and groups.

## 6. Verification: `python -m agents.isolation verify` (`make isolate-verify`)

Exit status 0 only if every check passes; prints one table.

1. **Configuration:** users, groups, memberships, marker, path access.
2. **Access matrix:** for each service user, a forked child drops to that user and attempts each operation, so
   the kernel decides. Expected: read own key ✓, other keys ✗, `switch.env` ✗, `router.token` ✗, `.state/run/*` ✗,
   `.switch-infra/.env` ✗; write own directory ✓, others' ✗, `.state/` ✗, code (`agents/`) ✗; `production.lock`
   read-write ✓ for translator and gate only; create in the module directory ✓ translator only; replace `app/main.py`
   ✗ for everyone; anything else under `/root` ✗.
3. **Secret exposure scan:** as each service user, read every reachable regular file under the repository,
   `/root`, `/tmp`, `/var/tmp`, `/dev/shm`, `/etc`, `/home`, `/opt`, `/srv`, `/var/log` and `/var/lib/stranglerroom`
   (skipping `.venv` and files over 8 MB). Search for every secret value: `switch.env` and Switch `.env`
   passwords, tokens and keys, the router token, the three API keys, and the Mattermost database password. Any hit
   fails, except the user's own API key in its own key file.
4. **Live processes:** each running component's uid matches its user; `ROUTER_ADMIN_TOKEN` is in the environment of
   the proxy and the gate only; no secret value appears in any process's command line.

## 7. Interfaces (`agents/isolation.py`)

```python
SERVICE_GROUP = "stranglerroom"
LOCK_GROUP = "sr-prodlock"

@dataclass(frozen=True)
class Component:
    name: str                  # translator | fuzzer | cutover-gate | proxy | legacy | modern
    user: str                  # "sr-" + name
    groups: tuple[str, ...]    # supplementary groups
    agent: bool                # owns .state/agents/<name>.json and receives SWITCH_URL
    router_token: bool         # receives ROUTER_ADMIN_TOKEN
    production_lock: bool      # member of LOCK_GROUP
    writes_module: bool        # owns its files in the module directory

COMPONENTS: dict[str, Component]

def repo_root() -> Path                      # resolved checkout root
def state_dir(env=None) -> Path              # STRANGLER_STATE_DIR or <repo>/.state
def private_dir(component, env=None) -> Path # state_dir()/component.name
def module_dir(env=None) -> Path             # parent of MODERN_LISTS_PATH or targets/modern_fastapi/app
def marker_path(env=None) -> Path
def isolation_on(env=None) -> bool           # mode resolution of section 3 (raises IsolationError on misconfiguration)
def child_env(component, parent_env, *, router_token=None, switch_url=None) -> dict[str, str]
def prepare(component, ids, env=None) -> list[str]   # ids: resolved uid/gid numbers; returns the actions taken
def drop_privileges(uid: int, gid: int, groups: list[int]) -> None
def secret_values(env=None) -> dict[str, str]        # label -> value, for verify
```
