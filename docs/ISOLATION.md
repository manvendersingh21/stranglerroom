# Per-component OS users ("isolation")

Status: implemented (`agents/isolation.py`), revision 2. Revision 1 opened `/root` to the service users; the codex design review
(`.hacp/reviews/isolation-design.md`) rejected that, and this revision replaces it.

## 1. Why, and what it guarantees

Without isolation every StranglerRoom process runs as the operator (root on the demo host). Any one of them, or code
it executes (the translator-generated module running inside the modern server, an LLM-synthesised module), can then
read every secret on the host:

* the other agents' Switch API keys, so it can speak as another agent;
* the router token, so it can flip production with `POST /_router/flip`;
* `.state/switch.env`, which holds the Switch registration token, the gateway admin password **and the human lead's
  Mattermost password**, so it can impersonate the human and issue the human-only verbs.

Isolation runs every component as its own unprivileged Unix user, from a **service area** outside the operator's
home (`/srv/stranglerroom`). The operator's checkout, its `.state/` and `/root` keep their current permissions (0700),
so no service user can reach any of them. Each service receives a copy of exactly what it needs.

| component | Unix user | supplementary groups | receives | can write |
|---|---|---|---|---|
| translator | `sr-translator` | `sr-prodlock` | its own API key (copy), `room.json` (copy) | its private dir, the module dir, `production.lock` |
| fuzzer | `sr-fuzzer` | — | its own API key (copy), `room.json` (copy) | its private dir |
| cutover-gate | `sr-cutover-gate` | `sr-prodlock` | its own API key (copy), `room.json` (copy), the router token (environment only) | its private dir, `production.lock` |
| proxy | `sr-proxy` | — | the router token (environment only) | its private dir |
| legacy | `sr-legacy` | — | — | its private dir |
| modern | `sr-modern` | — | read access to the generated module | its private dir |

**Guarantees** (enforced by the kernel's file permissions and signal rules):

* a service cannot read the operator's files (`switch.env`, `router.token`, the master API keys, logs), another
  service's API key, private directory or environment;
* a service cannot write the code it or any other service runs, except that the translator writes the one generated
  module (its job), which the modern server serves;
* a service cannot signal or ptrace another service's processes (different uids, separate sessions).

**Not guaranteed** (an authority stays an authority): a compromised translator can publish any module and ignore the
advisory production lock; a compromised cutover gate holds the router token and can flip without a real approval; a
compromised proxy routes as it likes. Isolation limits each compromise to that component's own authority; it does not
make a component's authority conditional on the others. Also out of scope: Switch, Mattermost and Postgres themselves
(they authenticate their clients), other services on the host, a service binding a port that happens to be free, and
browser cookies. Cookies are host-scoped, not port-scoped: browse the targets at `127.0.0.1`, not at `localhost`
where Mattermost's session cookie lives. The proxy also drops Mattermost's and the Gateway's auth cookies before
forwarding.

## 2. Layouts

### 2.1 The checkout's `.state/` (both modes; the operator's)

Every file a component writes lives in that component's own directory. Paths honour `STRANGLER_STATE_DIR`.

```
.state/  switch.env (0600)  connection.env (URLs only, 0644)  router.token  room.json  production.lock
         agents/<name>.json  run/
         translator/pending_refusals.json   fuzzer/divergence_registry.json
         proxy/router.json                  legacy/legacy_openlibrary.log
```

### 2.2 The service area (isolated mode only)

`SERVICE_HOME` defaults to `/srv/stranglerroom` and is recorded in the marker.

```
/srv/stranglerroom/            root:root 0755
  app/                         root-owned mirror of the checkout's agents/, targets/, docs/ (no __pycache__, *.pyc,
                               nor the checkout's generated targets/modern_fastapi/app/lists.py)
  venv/                        root-owned mirror of the checkout's .venv (its python links to the system interpreter)
  state/                       root:root 0755, the STRANGLER_STATE_DIR of every service process
    room.json                  root 0644 (copy)
    agents/                    root 0711
      <name>.json              <user> 0600 (copy; written by root: mkstemp in this root-owned dir, fchown, fchmod,
                               fsync, rename)
    production.lock            root:sr-prodlock 0660
    module/                    sr-translator 0755: lists.py (0644), the MODERN_LISTS_PATH of translator and modern
    translator/ fuzzer/ cutover-gate/ proxy/ legacy/ modern/   <user> 0700 (its private dir and HOME)
```

Rules:

* **Root never writes inside a service-owned directory.** No "ownership repair": a service-owned directory is created
  empty by root and then belongs to its service. The only root operation inside one is a reset's `unlink` of known
  names, through a directory descriptor opened with `O_DIRECTORY|O_NOFOLLOW` whose owner has been checked (unlink
  never follows the final component).
* Every directory root writes into (`app/`, `venv/`, `state/`, `state/agents/`) is root-owned and not writable by any
  service. Before writing, root checks that it is not a symlink and is owned by root.
* The mirrors are refreshed by every isolated launch (only changed files; temp file plus rename), serialised by an
  `flock` on `SERVICE_HOME/.sync.lock`.

## 3. Mode

`STRANGLER_ISOLATION` = `off` | `on` | `auto` (default `auto`).

* The marker is `/etc/stranglerroom/isolation.json` (override: `STRANGLER_ISOLATION_MARKER`):
  `{"state": "setting-up"|"ready", "repo": "<resolved checkout>", "service_home": "/srv/stranglerroom", …}`.
* `auto`: on if and only if the marker exists, is `ready` and names this checkout. A marker that is not `ready` is an
  error (exit 2: "isolation setup incomplete: re-run `make isolate` or `make isolate-off`"). A CI runner or another
  checkout never picks the marker up.
* `on` without a ready marker for this checkout is an error, and so is a launch (`exec`) while isolation is on and the
  caller is not root. Isolation never silently falls back to off.
* The test suite forces `off` (autouse fixture), so tests never drop privileges, sync mirrors or touch the host.

## 4. Launcher: `python -m agents.isolation exec <component> -- <python> -m <module> [args…]`

The scripts decide the mode once, using `python -m agents.isolation mode`, which prints `on` or `off`. They launch
each process as

```bash
nohup ${ISOLATE:+"$PY" -m agents.isolation exec "$name" --} "$PY" -m "$module" … >>"$log" 2>&1 </dev/null 9>&- &
```

With isolation off, nothing changes. With it on, the launcher, running as root:

1. checks the marker and that `euid == 0`;
2. refreshes the mirrors and prepares the component's part of the service area (§2.2): private dir, API key and
   `room.json` copies for agents, the production lock for translator and gate, the module dir (owned by the
   translator; also created when the modern server starts first, because it serves from that directory). The mode of
   a service-owned directory is restored through its no-follow descriptor, so a `chmod 0777` by its owner does not
   survive a relaunch. Mirrored files lose group/other write and set-id bits; a mirrored symlink must stay inside its
   tree or point at a path only root can modify;
3. rewrites argv: `[<python>, "-m", <module>, …]` becomes `[SERVICE_HOME/venv/bin/python, "-m", <module>, …]`
   (argv[0] is discarded), with cwd `SERVICE_HOME/app`. Any other argv shape is refused, and so is a module other than
   the component's own;
4. builds the environment from **that component's** schema, not from a prefix wildcard:
   * every component gets a fixed `PATH` (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, keeping only
     directories whose whole path only root can modify; on the dev box `/usr/local/bin` belongs to another user and is
     dropped), never root's own; `LANG`, `LANGUAGE`, the 13 standard locale variables (`LC_ALL`, `LC_CTYPE`, … — an
     explicit list, not `LC_*`), `TZ`, `PYTHONUNBUFFERED`, `STRANGLER_LOG_LEVEL`;
   * agents get `STRANGLER_AGENT_PACE`, `LEGACY_URL`, `MODERN_URL`, `PROXY_URL`, and `SWITCH_URL` (from the
     environment, else the checkout's public `connection.env`, else the default; never `switch.env`);
   * the translator also gets `STRANGLER_TRANSLATOR_MODE`, `STRANGLER_LLM_CMD` (in LLM mode its first word is resolved
     on that `PATH`, checked, and pinned to the absolute path), and the variables named in
     `STRANGLER_TRANSLATOR_PASS_ENV` (comma-separated, for LLM credentials). Naming `LD_*`, `PYTHON*`,
     `STRANGLER_ISOLATION*` or any `switch.env`/Switch `.env` secret there is an error, not a silent skip;
   * the proxy gets `LEGACY_URL` and `MODERN_URL`;
   * the modern server gets `MODERN_NO_RELOAD`;
   * `SWITCH_URL`, `PROXY_URL`, `LEGACY_URL` and `MODERN_URL` must name an IP literal: `localhost` (which may
     resolve to `::1`, where any local user can bind a port Switch or a target leaves free) and other names are
     refused, and so are unspecified and IPv4-mapped addresses, backslashes, user info, queries and non-http(s)
     schemes. The
     component receives a canonical URL rebuilt from the parsed parts, never the original string;
   * set last by the launcher, overriding anything above: `STRANGLER_STATE_DIR=SERVICE_HOME/state`,
     `PYTHONPATH=SERVICE_HOME/app`, `PYTHONSAFEPATH=1`, `PYTHONDONTWRITEBYTECODE=1`, `HOME` (its private dir),
     `USER`/`LOGNAME`,
     `MODERN_LISTS_PATH=SERVICE_HOME/state/module/lists.py` (translator, modern), and `ROUTER_ADMIN_TOKEN`
     (proxy and cutover-gate only), whose value is `resolve_router_token()` evaluated as root, the rule both use
     unisolated;
   * finally removed from every other component: `ROUTER_ADMIN_TOKEN`, `ROUTER_TOKEN_FILE`. A pass-through name
     can never set a launcher-set or forbidden variable;
5. starts a new session (`setsid`; failure is an error), closes every inherited descriptor except 0, 1 and 2, sets
   the supplementary groups, gid and uid (real, effective and saved) with the process non-dumpable before and after
   the switch (so nothing of root's memory is readable while it still holds it; a host with `fs.suid_dumpable=1`,
   where the kernel would make it dumpable in between, is refused), sets `no_new_privs`, checks that `setuid(0)` now
   fails and that the permitted, effective and ambient capability sets are empty, joins a new session keyring (fail
   closed: the join must succeed, or the kernel must prove there is nothing to inherit with ENOKEY or EKEYREVOKED
   from `KEYCTL_GET_KEYRING_ID`, or a real ENOSYS on a known architecture; EPERM, EACCES and unknown architectures are
   errors), sets `umask 077`, changes to `SERVICE_HOME/app` and `execve`s the component. The recorded pid is
   therefore the component itself.

### Scripts

* The mode is decided once, before any `mkdir`, lock or log; `mode --run-dir DIR` also validates the run directory
  when isolation is on (every path component root-owned and not writable by others unless sticky; the directory
  itself 0700), creating missing parts descriptor by descriptor from `/`. Logs are created 0600. The marker is
  read the same way: an ancestor a service could rename is an error, never "off". Once a script has decided
  "on", it launches with `STRANGLER_ISOLATION=on`, so a marker that disappears in between is an error too.
* With isolation on, "already running" means a live tracked pid whose four uids are all the component's user and
  whose command line no longer holds `-m agents.isolation`; for a target, its user must also own the listening socket
  (`/proc/net/tcp`). A component found running as another uid, or a healthy port with no tracked isolated process, is
  an error naming the fix (`restart` or `stop`), never "already up". `status` applies the same acceptance
  ("running (isolated)" or "running, NOT isolated — run restart"). A launch that does not come up isolated is
  killed with its children, and `stop` repeats `pkill -u` for the service users until none of their processes is
  left. A sweep that ends with survivors fails: `stop` exits non-zero after trying every service, and `restart`,
  `start --fresh` and `reset` do not proceed. If the mode cannot be determined (e.g. a broken interpreter), `stop`
  and `status` still work, sweeping the fixed service accounts without reading pid files from the unvalidated run
  directory, while `start` fails closed.
* Paths (`STRANGLER_STATE_DIR`, `ROUTER_STATE_FILE`, `MODERN_LISTS_PATH`) resolve the same way in the scripts and in
  Python: relative to the checkout, and `~` or `~/…` to root's home. Named-user forms (`~name/…`) are refused.
* `targets.sh start --fresh` with isolation on also runs `python -m agents.isolation reset`. This unlinks
  `proxy/router.json`, `fuzzer/divergence_registry.json` (and `.staging`) and `module/lists.py` in the service area.
* `make clean-state` also runs `python -m agents.isolation clean`, whatever the mode, whenever a service area exists (so
  no API-key copy outlives isolation). It empties `SERVICE_HOME/state`:
  each entry is first renamed inside the root-only `state/`, then removed by an iterative, descriptor-based remover
  (every directory opened `O_DIRECTORY|O_NOFOLLOW` relative to its parent and identity-checked; no recursion, so a
  deep tree a service built cannot crash it; `agents/` first; errors are reported per entry). As non-root it leaves
  the service area alone. `clean-state` keeps `switch.env` and `connection.env`.
* The run directory must be root-owned and not group- or other-writable. The default `.state/run` under the 0700
  checkout is out of every service's reach.

## 5. Host setup and teardown (root)

`python -m agents.isolation setup [--service-home DIR]` (`make isolate`):

* refuses while any StranglerRoom component is running (stop them first);
* writes the marker with `"state": "setting-up"` first, then creates the group `sr-prodlock` and the six system
  users (no login shell, home `/nonexistent`, primary group named after the user, `sr-prodlock` for translator and
  gate), creates `SERVICE_HOME`, fills the mirrors, prepares every component, runs the startup smoke import as every
  service user, and only then marks it `ready` (a failure leaves `setting-up`, which `mode` reports as an error). It
  is idempotent: re-running it repairs or updates an existing installation. "Running" here means any process with a
  `-m <component module>` pair or any service uid; it also gates `clean`;
* never changes the permissions of `/root`, the checkout or anything outside `SERVICE_HOME`, `/etc/passwd`,
  `/etc/group` and the marker.

`python -m agents.isolation disable [--purge]` (`make isolate-off`) removes the marker; `--purge` also removes
`SERVICE_HOME` and the users and group. Both refuse while a component is running.

## 6. Verification: `python -m agents.isolation verify` (`make isolate-verify`)

Exit status 0 only if every check passes; prints one table.

1. **Configuration:** the marker is `ready`; the users, group and memberships exist; `SERVICE_HOME` ownership and
   modes match §2.2; `/root` and the checkout are not reachable by any service user.
2. **Access matrix:** for each service user, a forked child drops to that user and attempts each operation, so the
   kernel decides. Expected:
   * read: its own API key copy ✓, other key copies ✗, `room.json` ✓ (agents);
   * read or traverse: the checkout's `switch.env`, `router.token` and `.state/agents/*` ✗, `/root/.env` ✗;
   * write: its private dir ✓, others' private dirs ✗, `state/` ✗, `app/` ✗, `venv/` ✗;
   * `production.lock` read-write ✓ for translator and gate only;
   * create in `module/` ✓ for the translator only.
3. **Startup smoke:** as each user, the service interpreter imports the component's module. This proves the
   interpreter, standard library and dependencies are reachable. In LLM mode, the first word of
   `STRANGLER_LLM_CMD` must resolve for `sr-translator` to an executable whose whole path only root can modify.
4. **Secret exposure scan.** It searches for every secret value: `switch.env` and Switch `.env` passwords, tokens and
   keys, the router token, the three API keys, and every Mattermost database password. The search covers every
   reachable regular file under `SERVICE_HOME`, `/tmp`, `/var/tmp`, `/dev/shm`, `/etc`, `/home`, `/opt`, `/srv`,
   `/var/log`, `/var/lib/stranglerroom` and `/root`. Any hit fails, except a user's own API key in its own key copy
   (matched by inode).
   * Root only lists names, with an iterative descriptor walker (`O_DIRECTORY|O_NOFOLLOW`, depth at most
     min(256, RLIMIT_NOFILE/4), entry, directory and time budgets). It never opens a candidate.
   * The search happens in children dropped to the service users. World-readable files (o+r, every ancestor o+x)
     are searched once per run and count for every subject. Each subject then searches only what its own uid or
     groups add, and follows symlinks itself (visited set, caps).
   * Large files are streamed in overlapping chunks, not skipped.
   * Exhausting the 32 GiB / 2M-file budget, hitting a depth limit, or an unexpected listing/open error (EMFILE
     included) makes the scan incomplete, and an incomplete scan FAILs. It never reports PASS.
5. **Live processes:** every process running a component module is found from `/proc`, pid file or not. A
   duplicate, an untracked instance, or one with the wrong uid fails; SKIP means no instance exists. Each
   component's uids and groups match its user, each has its own session, and signals across components are
   refused. Every listener on the ports of the effective endpoints (`SWITCH_URL`, `PROXY_URL`, `LEGACY_URL`,
   `MODERN_URL` — local ones: loopback, or any address this host can bind) and on 9000–9002, in either address family
   and on any address (`[::1]` and `[::]` included), belongs to the expected uid; two owners expected on one port is
   a FAIL;
   `ROUTER_ADMIN_TOKEN` is in the environment of proxy and gate only (root lacks `CAP_SYS_PTRACE` here, so each
   process's `/proc/<pid>/environ` is read as that process's own user); no secret value appears in any process's
   command line. A secret shorter than 8 characters cannot be searched for reliably and is reported SKIP.

## 7. Interfaces (`agents/isolation.py`)

```python
LOCK_GROUP = "sr-prodlock"
DEFAULT_SERVICE_HOME = Path("/srv/stranglerroom")

@dataclass(frozen=True)
class Component:
    name: str                  # translator | fuzzer | cutover-gate | proxy | legacy | modern
    user: str                  # "sr-" + name
    module: str                # the one module it may run, e.g. "agents.translator_agent"
    agent: bool                # API key + room.json copies, SWITCH_URL
    router_token: bool         # ROUTER_ADMIN_TOKEN
    production_lock: bool      # member of LOCK_GROUP
    module_writer: bool        # owns SERVICE_HOME/state/module (Ids.module_owner: the translator's uid/gid)
    env_keys: tuple[str, ...]  # component-specific pass-through names (section 4)

COMPONENTS: dict[str, Component]

def repo_root() -> Path
def marker_path(env=None) -> Path
def load_marker(env=None) -> dict | None
def isolation_on(env=None) -> bool                      # section 3; raises IsolationError on misconfiguration
def service_paths(service_home: Path) -> ServicePaths   # app, venv, state, agents, module, lock, private(c)
def child_env(component, parent_env, *, paths, router_token=None, switch_url=None) -> dict[str, str]
def sync_mirrors(repo: Path, paths) -> list[str]        # actions taken
def prepare(component, ids, paths, repo_state: Path) -> list[str]
def reset(paths) -> list[str]
def drop_privileges(uid: int, gid: int, groups: list[int]) -> None
def secret_values(repo_state: Path, repo: Path, *, env=None) -> dict[str, str]  # label -> value, for verify
                                                  # (env: ROUTER_ADMIN_TOKEN / ROUTER_TOKEN_FILE / MM DSN overrides)
```
