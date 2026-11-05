# Isolation design security review

## Findings

### 1. High — Ownership repair accepts hard-link aliases to unrelated files

**Scenario:** Section 4 treats root ownership of an entry in a component-owned directory as evidence that root created it there. That is insufficient. `O_NOFOLLOW`, `lchown`, and `follow_symlinks=False` do not distinguish hard links.

If `fs.protected_hardlinks` is disabled, a compromised component can hard-link a readable root-owned code file such as `agents/isolation.py` into its private directory. The next prepare operation transfers ownership of the shared inode to the attacker. The attacker can then rewrite the isolation launcher, obtaining root execution on its next invocation. The stated host constraints do not require hard-link protection.

There is also an attack with hard-link protection enabled: translator has read/write access to the root-owned `production.lock`, so Linux permits it to hard-link that file to `app/lists.py`. Preparing translator then changes the lock inode to translator ownership and mode 0644. Gate can no longer open the lock read/write, and translator can subsequently change its permissions.

**Minimal design fix:** Do not repair arbitrary entries in attacker-controlled directories as root. Migrate known old files while components are stopped, using a trusted manifest. For any existing file whose metadata root changes, open it without following symlinks, validate its type, ownership and link count on the descriptor, require `st_nlink == 1`, and operate on that same descriptor. Reject aliases rather than changing them.

### 2. High — Opening `/root` changes its confidentiality boundary permanently

**Scenario:** Removing other permissions from existing sibling entries preserves only a snapshot of the protection previously provided by `/root` mode 0700.

After `/root` becomes traversable by every service user, a new `/root/operator-notes` created with mode 0644 is readable by a compromised component that knows its name. Previously, `/root` prevented that access. Existing siblings with permissions or ACLs granting one of the service identities access can also become reachable; removing only other permission bits does not revoke those grants.

The claim that service users can subsequently reach “nothing else in `/root`” therefore does not hold. A verification scan cannot protect files created after it runs.

**Minimal design fix:** Put the checkout and its runtime dependencies under a dedicated service location such as `/srv/stranglerroom`, leaving `/root` at 0700. Avoid modifying the operator’s home-directory boundary.

### 3. High — Existing processes and launcher wrappers can pass startup checks without isolation

**Scenario:** The current agent launcher accepts an existing PID based on its command line. The target launcher accepts any healthy response on the expected port before checking a tracked process. If setup enables isolation while old root-run components remain alive, subsequent starts can report success while continuing to use those unisolated processes.

The proposed wrapper also contains the eventual `-m <module>` and `--port <port>` arguments in its own command line. The current checks search for these pairs anywhere in argv, so they can accept the root isolation helper while it is still preparing files. An agent start can report success before privilege dropping or component execution occurs.

**Minimal design fix:** When isolation is selected, require the expected UID and completed component exec before accepting an existing or newly started process. Reject healthy endpoints without a matching isolated process. Make setup require stopped components or stop them through verified supervision. Require an explicit successful startup handshake; argv substring checks alone are insufficient.

### 4. High — Privileged shell filesystem operations precede preparation

**Scenario:** The scripts create run directories, open lock files, open logs, and write PID files outside the proposed Python prepare step. Checking paths inside `agents.isolation exec` cannot protect earlier shell operations.

For example, the supported `STRANGLER_RUN_DIR` can point inside a component-owned directory. That component can plant `.lock` as a symlink to a root-owned file; `exec 9>"$RUN/.lock"` truncates the target as root before isolation preparation runs. Log and PID redirections have the same class of problem. A pre-existing unsafe default run directory creates the same exposure.

**Minimal design fix:** Validate and prepare state and run directories before any privileged shell filesystem access, including locking. Require protected ancestry, root ownership, restrictive modes, and safe regular files for logs, locks and PID records. Prefer descriptor-based opens in the trusted launcher. Reject run-directory overrides beneath component-writable directories.

### 5. High — Dropping credentials leaves inherited privileged descriptors usable

**Scenario:** The launch sequence closes descriptor 9 but does not require closing other inherited descriptors. If the operator invokes it with, for example, descriptor 8 open on `switch.env`, every launched component can read that descriptor after dropping privileges. File permissions and `/proc` restrictions do not revoke an already-open file description.

Inherited writable descriptors can similarly permit changes to another component’s state or to operator files.

**Minimal design fix:** Before executing a component, close every descriptor except an explicitly constructed standard-input/output/error set. Close preparation and secret-reading descriptors as well. Do not rely on callers or Python’s defaults to sanitize descriptors inherited from the shell.

### 6. High — Runtime settings still require the secrets isolation withholds

**Scenario:** `RoomAgent.__init__` calls `Settings.load()`. That method calls `read_env_file(STATE_DIR / "switch.env")` before resolving settings. With the proposed layout, the file exists but is root-owned mode 0600; `read_text()` raises `PermissionError`.

Even if that read is removed or skipped, `Settings.load()` still requires the registration token, gateway admin credentials and Mattermost password. Giving agents only `SWITCH_URL`, as specified, cannot start the current runtime. Restoring those secrets to make it work would defeat the isolation objective.

**Minimal design fix:** Split runtime connection settings from provisioning/admin settings. Running agents must load only their own credentials, room metadata and Switch URL, without opening `switch.env` or requiring administrative or human credentials. Keep the privileged settings loader exclusive to operator workflows.

### 7. High — A malicious web target can steal the human’s browser session

**Scenario:** Suppose the human logs into Mattermost on `http://localhost:18065` and then visits a target or the proxy using `localhost`. Cookies are scoped by host and path, not port. A Mattermost session cookie applicable to `/` can therefore accompany requests to another localhost port.

A compromised modern or legacy server receives those cookies. The proxy explicitly forwards incoming `Cookie` headers to its selected upstream. `HttpOnly` does not prevent a malicious server from reading the request header. The attacker can replay a captured session to Mattermost and issue commands as the human lead without learning the password.

**Minimal design fix:** Serve Mattermost/Gateway and untrusted targets on distinct hostnames with appropriately restricted, host-only authentication cookies. Establish that separation for browser access as well as backend URLs. Do not place untrusted web applications on the authentication cookie’s host.

### 8. High — The production lock and human-approval boundary remain cooperative

**Scenario:** A compromised translator can replace `lists.py` directly while gate holds `production.lock`; nothing in Unix permissions requires it to acquire the advisory lock. It can therefore replace the live build during or after a cutover, contradicting the stated production invariant.

A compromised gate receives the router token and can call `POST /_router/flip` directly. The proxy accepts arbitrary nonempty `approved_by` and `evidence` strings; it does not authenticate the named human or verify an approval message. Gate can consequently flip routing and attribute that action to the human without possessing the human’s credentials. A compromised proxy can also change its own routing directly.

**Minimal design fix:** Explicitly exclude compromised deployment and routing authorities from these guarantees, or move enforcement into a separate trusted service. For enforcement, that service must own publication and routing authority, serialize both operations, and independently verify approval bound to the exact migration and build. Merely hiding the token from other UIDs cannot enforce human approval against its holder.

### 9. Medium — The environment allow-list crosses component boundaries

**Scenario:** `STRANGLER_*` is forwarded to every component, despite including translator configuration and arbitrary secret-bearing names.

For example, an operator can supply an LLM credential as `STRANGLER_LLM_API_KEY` and name it in `STRANGLER_TRANSLATOR_PASS_ENV`, reasonably expecting translator-only access. The wildcard already gives that value to modern, legacy, proxy and the other agents. A credential embedded in `STRANGLER_LLM_CMD` is likewise copied to every component.

The translator pass-through step can also reintroduce `ROUTER_ADMIN_TOKEN` after the unconditional drop, contradicting the proxy-and-gate-only rule.

**Minimal design fix:** Use explicit per-component environment schemas instead of prefix wildcards. Pass translator configuration and LLM credentials only to translator. Apply forbidden-secret checks after all additions, and prevent pass-through configuration from overriding security-critical variables. Keep credentials out of command strings and argv.

### 10. Medium — Different UIDs do not prevent every cross-component signal

**Scenario:** Linux permits `SIGCONT` between processes in the same session even when their UIDs differ. The launch sequence does not create separate sessions; backgrounding and `nohup` do not do so. Components launched from the same operator session can therefore resume one another, contradicting the unconditional “None can signal” claim.

**Minimal design fix:** Create a separate session for each component before dropping privileges, and verify session separation. Scope the documented signal guarantee to the actual kernel rules.

### 11. High — Setup changes permissions before creating its recovery record

**Scenario:** Setup records changes in a marker written last. If it fails or is interrupted after opening an ancestor but before writing that marker, permission changes remain without the recovery record needed by disable.

In default `auto` mode, the missing marker also selects unisolated execution despite the partially completed setup. There is no specified transaction lock coordinating setup, disable and launch. Disable additionally needs ordering guarantees: restoring readable sibling modes before revoking ancestor traversal can temporarily expose files.

**Minimal design fix:** Persist a protected recovery journal before each permission change, roll back failures, and serialize setup/disable with launchers. Treat an incomplete transaction as an error, never as automatic isolation-off. Revoke newly granted traversal before restoring permissions that depended on the original private ancestor.

### 12. Medium — Making only the checkout reachable can make the interpreter unreachable

**Scenario:** The Makefile creates the environment with `uv venv`. A supported uv environment can have `.venv/bin/python` resolve into a managed installation under `/root/.local/share/uv/python/...`.

Setup grants access to the checkout path while removing other access from sibling entries such as `/root/.local`. Root can start the isolation helper, but after dropping privileges its exec of `.venv/bin/python` fails because the interpreter’s symlink target is unreachable. A reachable interpreter can also have unreachable standard-library or dependency paths. The specified access matrix does not test these runtime dependencies.

**Minimal design fix:** Install the interpreter and required dependencies in a protected service-accessible location. Validate resolved executable and dependency paths, and run an isolated startup check for every component before declaring setup complete. Do not solve this by opening the operator’s entire tool or credential directory.

## Verdict

**Reject pending changes.** The proposed UID separation provides useful discretionary access controls, but the design still permits unsafe privileged ownership changes, inherited-descriptor leaks, silent reuse of unisolated processes, and browser-session theft. It also cannot currently start the agents with the specified secret restrictions.

Approval requires fixing those boundaries, making setup recoverable, and either enforcing production governance outside compromised components or explicitly narrowing that guarantee.
