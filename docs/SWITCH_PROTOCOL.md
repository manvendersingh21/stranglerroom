# Switch v0.31.0 protocol reference (implementation-ready)

Merged from six source-reading reports and re-checked against the tree at
`/root/another_hackathon/.switch-infra` (tag `switch-v0.31.0`). Where the reports disagreed, the
source was read again; the resolution is marked **[resolved]**.

Path conventions:

- `SC/` = `/root/another_hackathon/.switch-infra/core/switch_core/`
- `H` = `SC/bridges/agent/api/handlers.py`, `S` = `SC/bridges/agent/protocol/stream.py`,
  `AC` = `SC/bridges/agent/protocol/agent_connections.py`, `T` = `SC/bridges/agent/protocol/types.py`,
  `OPS` = `SC/bridges/agent/operations/`, `MM` = `SC/bridges/collaboration/mattermost/adapter.py`
- `INFRA/` = `/root/another_hackathon/.switch-infra/`

Live state observed on this box (read-only gateway calls, 2026-11-05): switch-core healthy on
`http://localhost:18000`, Mattermost on `:18065`, one bridge `mattermost` (`status: active`,
`is_default: true`), the gateway admin already linked to Mattermost account `user`, zero agents,
zero rooms, cookie issued **without** `Secure` (`GATEWAY_COOKIE_SECURE=false`).

---

## 1. Native boot environment

### 1.1 What runs where (this box: k8s pod, no Docker)

| Component | Address | Notes |
|---|---|---|
| switch-core (agent bridge + gateway + UI) | `http://localhost:18000` | One process. Agent API at `/agents/...`, gateway mounted at `/gateway` (`SC/main.py:1261`), `/health` and `/health/ready` (`SC/main.py:1197,1206`). Port 8000 is taken by an unrelated app ("ThermoMind"). |
| Collaboration callback listener | `127.0.0.1:18081` | Only Mattermost button presses (`POST /collaboration/{bridge_type}/{bridge_id}/callback`, `SC/bridges/collaboration/ingress.py`). Not a way to send messages. |
| Mattermost 11.9.0 | `http://localhost:18065` | Tarball in `/root/another_hackathon/.native/mattermost`, team `switch`. |
| Postgres 16 | `127.0.0.1:5432` | Data in `/var/lib/stranglerroom/pgdata`; DBs `switch`, `mattermost`. |

There is no separate gateway port natively. The docker-compose stack's `:3000` gateway UI and its
default `:8000` API do not apply here. **[resolved]** The gateway report's `BASE=http://localhost:8080`
and the ops report's "gateway on 3000" are both wrong for this stack: every gateway route is
`{SWITCH_URL}/gateway/...` with `SWITCH_URL=http://localhost:18000`.

### 1.2 Boot

`/root/another_hackathon/scripts/bootstrap_switch.sh` (auto: docker if a daemon answers, else native)
does it end to end and writes `/root/another_hackathon/.state/switch.env` (mode 600) with
`SWITCH_URL, MATTERMOST_URL, AGENT_REGISTRATION_TOKEN, GATEWAY_ADMIN_EMAIL/PASSWORD,
MATTERMOST_TEAM_NAME, MATTERMOST_USER(/PASSWORD), MATTERMOST_ADMIN_USER(/PASSWORD)`.
Load it with `set -a; . /root/another_hackathon/.state/switch.env; set +a`.

Native switch-core is started with (`scripts/bootstrap_switch.sh`, `native_switch_core`;
also `/root/another_hackathon/.native/core-env.sh`):

```bash
set -a; . /root/another_hackathon/.switch-infra/.env; set +a
export DB_HOST=127.0.0.1 DB_PORT=5432 ID_SERVER_NAME=localhost MATRIX_SERVER_NAME=localhost
export SERVER_HOST=127.0.0.1 SERVER_PORT=18000
export COLLABORATION_CALLBACK_HOST=127.0.0.1 COLLABORATION_CALLBACK_PORT=18081
export GATEWAY_COOKIE_SECURE=false OUTBOUND_ALLOWED_PRIVATE_HOSTS=localhost,127.0.0.1
export GATEWAY_UI_DIR=/root/another_hackathon/.switch-infra/gateway/dist FRONTEND_BASE_URL=http://localhost:18000
export TELEMETRY_ENABLED=false THIRD_PARTY_AVATARS_ENABLED=false
unset FEATURE_FLAGS_ENABLED CONTROLLER_TOKEN_SECRET JWT_SECRET_KEY API_HOST_PORT MATTERMOST_HOST_PORT POSTGRES_HOST_PORT
cd /root/another_hackathon/.switch-infra/core && nohup .venv/bin/python -m switch_core.main > /root/another_hackathon/.native/core.out 2>&1 &
```

Then the seeder: `MATTERMOST_URL=MATTERMOST_URL_FOR_SWITCH=http://localhost:18065`,
`MATTERMOST_CALLBACK_BASE_URL=http://127.0.0.1:18081`, `SWITCH_URL=http://localhost:18000`,
`core/.venv/bin/python deploy/shared_resources/setup.py` (retried up to 5 times).

### 1.3 Required env (no defaults; all missing = 10 validation errors)

| Var | Source | Notes |
|---|---|---|
| `DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME` | `SC/config.py:145-149` | `DB_PORT` is a string. Runtime role here is `switch_app` (NOSUPERUSER NOBYPASSRLS). |
| `DB_OWNER_USER, DB_OWNER_PASSWORD` | `SC/config.py:176-177` | Optional but needed: Alembic and grants run as owner (`postgres` here). |
| `ID_SERVER_NAME` | `SC/config.py:205` | Old name `MATRIX_SERVER_NAME` still accepted. |
| `AGENT_REGISTRATION_TOKEN` | `SC/config.py:208` | Seeded as the deployment bootstrap registration key. |
| `SECRET_KEYS` | `SC/config.py:214` | `<id>:<secret>[,...]`, current first, secret >= 32 chars (`SC/keys.py:37,99`). |
| `GATEWAY_ADMIN_EMAIL, GATEWAY_ADMIN_PASSWORD` | `SC/config.py:243-244` | Admin (`role="admin"`) seeded at boot (`SC/main.py:1615-1622`). Here `admin@switch.local`. |

- `SwitchConfig` has no `env_file` (`SC/config.py:143`): only the process env counts.
- Migrations run at boot when `DB_MIGRATE_ON_BOOT` (default true, `SC/config.py:183`; `SC/main.py:2104-2155,2234-2237`).
- Before serving, `verify_restricted_role` (`SC/db/runtime_role.py:210`) refuses a superuser/BYPASSRLS runtime role. Escape hatch `DB_REQUIRE_RESTRICTED_ROLE=false` (`SC/config.py:197`).
- `GATEWAY_UI_DIR` must contain `index.html` or boot fails (`SC/gateway/ui.py:95,107`).
- `OUTBOUND_ALLOWED_PRIVATE_HOSTS` defaults to empty (`SC/config.py:226`); without `localhost` the Mattermost bridge is refused at register and at every start (`SC/outbound.py:96-107`; `SC/bridges/collaboration/lifecycle_service.py:515-528,943`).
- `FEATURE_FLAGS_ENABLED` is empty/unset here, so **agent management is off** (blocks v2 templates that create agents, section 7.6). Turning it on also needs `CONTROLLER_TOKEN_SECRET` >= 32 chars or boot fails (`SC/config.py:868-897`).

### 1.4 Mattermost server settings Switch relies on

Set by `native_mattermost` in the bootstrap script, mirroring `INFRA/deploy/local/docker-compose.yml:60-79`:
`EnableBotAccountCreation=true` (one bot per agent), `EnableUserAccessTokens=true` (`MM:2258-2267`),
`TeammateNameDisplay=full_name` (`MM:2526-2542`), `EnableOpenServer=true`,
`SiteURL=http://localhost:18065`, `AllowedUntrustedInternalConnections="localhost 127.0.0.1"` (button callbacks only).

---

## 2. Register an agent

### 2.1 Credentials

| Credential | Origin | Owner of agents it registers | Can call |
|---|---|---|---|
| Bootstrap token (`ApiKey.type="bootstrap"`) | `AGENT_REGISTRATION_TOKEN` env (`SC/main.py:1726`; `SC/bridges/agent/registration_bootstrap.py`) | Synthetic non-admin user `agent-bootstrap@switch.local` (`registration_bootstrap.py:57,159-179`) | `POST /agents`, `/agents/register-known*` only |
| Registration token (`type="registration"`) | `POST /gateway/api-keys {"label": "..."}` with the gateway cookie; response `{id, label, key, created_at}` (`SC/gateway/api_keys.py:72-111`; `SC/gateway/schemas.py:545-561`) | The logged-in user who minted it | same |
| Agent API key (`type="agent"`) | Returned by registration | n/a | every `/agents/{id}/...` route for that agent |

**[resolved]** The Mattermost report claimed bootstrap-registered agents are admin-owned (citing the
warning at `SC/main.py:1699-1703`). The source says otherwise: `resolve_registration_owner_id`
returns the bootstrap owner for a bootstrap key (`registration_bootstrap.py:159-179`); the
`main.py` warning is about *legacy* admin-owned agents. Consequence: see 3.5 and gotcha G1.

- Registration tokens are refused on agent routes (401 `{"detail":"Not authenticated"}`, `SC/bridges/agent/auth.py:612-620`) and on `/mcp` (`auth.py:318-320`).
- Agent keys are refused for registration: 401 `{"detail":"Invalid registration token"}` (`H:154-156`; `REGISTRATION_KEY_TYPES` at `registration_bootstrap.py:42`).

### 2.2 `POST {SWITCH_URL}/agents`

Router mounted at `/agents` (`SC/bridges/agent/app.py:151`). Header `Authorization: Bearer <bootstrap-or-registration token>`.

Body `RegisterAgentRequest` (`SC/bridges/agent/api/schemas.py:20-30`):

| Field | Type | Req | Notes |
|---|---|---|---|
| `name` | str | yes | `\A[a-z0-9][a-z0-9._-]*\Z` (`SC/bridges/agent/protocol/agent_core.py:180,481-485`); unique per tenant (`SC/db/models.py:668`). This is the handle: `@name` mentions and the Mattermost bot username. Not `everyone/channel/here/all` (`SC/room_wide_mention.py:32,65-75`). |
| `description` | str | yes | |
| `connector_type` | str | yes | Free-form; use `"custom"` (the e2e harness does). |
| `integration_profile` | object | yes | below |
| `display_name` | str\|null | no | <= 64 chars, no control chars (`SC/agent_display_name.py:25,51-71`). |
| `icon_url` | str\|null | no | Absolute https only (`SC/agent_icon.py:55`). |
| `tools` | `[{name, description, parameters?}]` | no | default `[]` (`T:46-49`) |
| `models` | `[{name, description}]` | no | default `[]` (`T:52-54`) |
| `metadata` | dict | no | default `{}` |
| `overwrite` | bool | no | default false. True rotates the key (2.4). |

`integration_profile` = `IntegrationProfile` (`T:19-43`, `extra="ignore"`):

| Field | Values |
|---|---|
| `connection_model` | `"always_on"` \| `"session_addressable"` \| `"session_passive"` \| `"auto_session"`; copied to `agents.agent_type` (`agent_core.py:503`) |
| `message_exchange` | bool (required) |
| `pre_invocation_mediation`, `post_invocation_mediation`, `event_reporting` | list[str] (required, free strings; use `[]`) |
| `command_capabilities` | optional `{reset, compact, interrupt}`, each `"unsupported"` (default) \| `"always"` \| `"session_dependent"` |

There is no top-level `handle` or `capabilities` field.

Response 200: `{"id": "<agent_id>", "api_key": "<token>"}` (`schemas.py:33-35`; `H:221`). The key is
`secrets.token_urlsafe(32)`, stored hashed (`agent_core.py:505-507`). Shown once.

Errors: 401 bad token (middleware 401 bodies are **plain text** `Invalid credentials` /
`Missing or invalid Authorization header`, `auth.py:259-263,336,587-605`); 400 bad name / icon /
display name / reserved word; 409 `Agent already exists: 'x'. Pass overwrite=True ...` (also when
`overwrite=true` but caller is not the owner, same text, `agent_core.py:517-535`); 422 pydantic
`{"detail":[...]}`; 503 bootstrap owner misconfigured (`H:168-172`).

Side effects: `ApiKey(type="agent")`, a client row (needed to send), the agent row, default addressing
policy owner + owner's agents (`SC/addressing.py:276-306`; `agent_core.py:434`), and a Mattermost bot
named exactly `name` (`MM:2222-2232`).

Variants: `POST /agents/register-known` (`agent_type` from `GET /gateway/agents/known-types`: `claude-code,
codex, opencode, antigravity, cursor`) and `POST /agents/register-known-bulk` (`H:286-409`).

### 2.3 Calling agent routes

- `Authorization: Bearer <api_key>` on every call, including the WebSocket handshake. No query-param token.
- **[resolved]** Path-id check differs by route: `PATCH /agents/{id}` and `DELETE /agents/{id}` return 403 `Not authorized for this agent` on mismatch (`H:417-418,439-440`); `/events`, `/message`, `/ops/*`, `/connection/*` use the token's agent and ignore the path (`H:703-763`, `SC/bridges/agent/api/operations.py:306-323`). Always send the real id.
- Controller-managed agents get 409 `{"error":{"code":"managed_by_controller",...}}` on their own key (`auth.py:289-300`).
- Key cache TTL 5 s (`SC/config.py:750`), invalidated immediately in-process.

### 2.4 Re-use, rotation, delete

- No get-or-create. `overwrite=false` + existing name = 409. Persist `{id, api_key}` (chmod 600).
- `overwrite=true` by the owner = rotation: same `id`, rooms and addressing policy; new key, old key 401 at once (old row deleted `agent_core.py:839`). Description, profile, connector_type replaced; `metadata` wiped if omitted; tools/models deleted and re-added (`agent_core.py:783-870`). `icon_url`/`display_name` kept when sent as null.
- A different token-owner (e.g. bootstrap vs. admin-minted) cannot rotate: 409.
- `DELETE /agents/{id}` (own key) -> `{"ok": true}`; 404 if gone (`H:432-447`). Gateway: `DELETE /gateway/agents/{id}` or `/gateway/agents/by-name/{name}` (owner or admin, `SC/gateway/agents.py:89-156`).
- Recover a lost key without rotation: owner only, `GET /gateway/api-keys` then `GET /gateway/api-keys/{key_id}/reveal` (`SC/gateway/api_keys.py:47-69,114-140`). Not possible for bootstrap-owned agents (that user never logs in).

```python
import httpx, json, os, pathlib, tempfile

SWITCH = os.environ.get("SWITCH_URL", "http://localhost:18000")
REG = os.environ["AGENT_REGISTRATION_TOKEN"]         # or an admin-minted registration key (3.5)
CRED_DIR = pathlib.Path("/root/another_hackathon/.state/agents")

PROFILE = {"connection_model": "always_on", "message_exchange": True,
           "pre_invocation_mediation": [], "post_invocation_mediation": [], "event_reporting": []}

def register(name: str, description: str, overwrite: bool = False, token: str = REG) -> dict:
    r = httpx.post(f"{SWITCH}/agents", headers={"Authorization": f"Bearer {token}"}, timeout=30,
                   json={"name": name, "description": description, "connector_type": "custom",
                         "integration_profile": PROFILE, "overwrite": overwrite})
    if r.status_code != 200:
        raise RuntimeError(f"register {name}: {r.status_code} {r.text}")
    return r.json()                                    # {"id": ..., "api_key": ...}

def creds(name: str, description: str) -> dict:
    p = CRED_DIR / f"{name}.json"
    if p.exists():
        c = json.loads(p.read_text())
        if httpx.get(f"{SWITCH}/agents/{c['id']}/ops",
                     headers={"Authorization": f"Bearer {c['api_key']}"}).status_code == 200:
            return c
    c = register(name, description, overwrite=True)    # rotate (owner = same token owner)
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CRED_DIR)           # 0600 from creation: no readable window
    with os.fdopen(fd, "w") as f: json.dump(c, f)
    os.replace(tmp, p)
    return c
```

---

## 3. Gateway login and addressing policy

### 3.1 `POST {SWITCH_URL}/gateway/auth/login`

- Body `{"email": str, "password": str}` (`SC/gateway/schemas.py:792-794`), email case-insensitive.
- 200 body `SessionUserResponse {id, name, email, role, created_at, server}` (`SC/gateway/auth_routes.py:97-112,131`).
- Cookie `switch_auth=<HS256 JWT>; HttpOnly; Max-Age=86400; Path=/; SameSite=lax` (+`Secure` when `GATEWAY_COOKIE_SECURE` true, the default; `SC/gateway/auth.py:137-145`; `SC/config.py:340`). Verified live: no `Secure` here.
- Cookie is the only gateway credential (`SC/gateway/auth.py:196-200`). No Bearer on `/gateway/*`; the `/gateway` prefix bypasses bearer middleware (`SC/bridges/agent/auth.py:44-60`). No CSRF header.
- Errors: 401 `Invalid email or password`, 403 `Password login is disabled` (`auth_routes.py:147-156`); later calls 401 `Not authenticated` / `Invalid token` / `Token expired`.
- Tenant resolution on every call (`SC/gateway/auth.py:533-626`): single membership binds automatically (the case here).
- Robust pattern (works even with `Secure`): replay the cookie as a header, as the seeder does (`INFRA/deploy/shared_resources/setup.py:186-222`).
- Other: `GET /gateway/auth/me`, `POST /gateway/auth/refresh`, `POST /gateway/auth/logout`, `GET /gateway/auth/me/identities`.

### 3.2 `PUT /gateway/agents/{agent_id}/addressing-policy`

`SC/gateway/agents.py:685-737`. Path takes the agent **id** (`GET /gateway/agents` -> `[{id, name, owner_id, owner_name, addressing_policy, ...}]`).
Body `UpdateAddressingPolicyRequest {"policy": AddressingPolicy | null}` (`SC/gateway/schemas.py:357-362`).
Permission: agent owner or tenant admin (gateway admin qualifies, `SC/gateway/agents.py:697-711`; `SC/authz.py:134-148`),
else 403. 404 `Agent not found: ...`. Response: full `AgentDetail`. Takes effect on the next message
(`SC/delivery/recipient.py:448-458`).

| `policy` | Effect |
|---|---|
| `null` (or `{}` / omitted) | Open: anyone may address (`SC/addressing.py:158-160,249-253`). Response `addressing_policy: null`. |
| `{"rules": []}` | Also open. |
| `{"rules": [...]}` | Deny by default; allowed if any rule matches. |

Rule `AddressingRule` (`SC/addressing.py:72-93`):

```
rooms: "*" | [room_id]          (default "*")
room_groups: "*" | [group_id]   (default "*"; ungrouped room only matches "*")
users: "*" | [ExternalUser.id or member client id]   (default "*", human senders)
agents: "*" | [agent_id]        (default "*", agent senders)
owner: bool         human who claimed the agent owner's identity
owner_agents: bool  agent with the same owner
platform: bool      Switch platform/admin client as sender (denied by default even when open)
```

Ready-made (`SC/bridges/agent/protocol/agent_detail.py:362-376`):

| Choice | `policy` JSON |
|---|---|
| owner_only | `{"rules":[{"users":[],"agents":[],"owner":true}]}` |
| owner_and_owner_agents (default for new agents) | `{"rules":[{"users":[],"agents":[],"owner":true,"owner_agents":true}]}` |
| anyone | `null` |

Trap: `users`/`agents` default to `"*"`, so `{"owner": true}` alone admits everyone.
`users` takes the `ExternalUser.id` row id from `GET /gateway/collaborations/{bridge_id}/users`, not the
raw Mattermost user id (`SC/delivery/addressing.py:445-452`).

Agent-side equivalent: op `update_agent_detail {"agent_id": <other>, "addressing": "owner_only"|"owner_and_owner_agents"|"anyone"}`.
**[resolved]** It refuses the caller's own id with 403 `An agent can't change its own settings...`
(`OPS/agent_management.py:104-111`, called at `OPS/definitions.py:~2289`) and needs the same owner
(`agent_core.py:3979-3992`). The e2e harness (`core/tests/e2e/follow_the_conversation/harness.py:168-183`)
calls it on itself; that would 403. Use agent A to set B and B to set A, or use the gateway PUT.

### 3.3 The refusal texts

- `ADDRESSING_DENIED_MESSAGE` (`SC/delivery/addressing.py:63-66`): "You're not permitted to direct messages to me in this room — my operator has restricted who can address me here."
- `ADDRESSING_UNCLAIMED_MESSAGE` (`:73-77`): "I only take instructions from my owner, and this chat account isn't linked to a Switch user yet..." Used when the sender's chat account is unclaimed and some rule has `owner: true` (`:380-407`).
- Posted by the agent as `@<sender> <text>` with `AUTO_REPLY_FLAG` once per event; the message is demoted to `addressed:false` and the agent is not woken (`SC/delivery/recipient.py:1503-1535,1591-1609`).
- Budget refusals use `str(BudgetExceeded)` instead (`recipient.py:1520-1531`).
- `send_targeted_message` reports `not_permitted` for the target (`agent_core.py:2005-2030`).

### 3.4 Identity linking

`setup.py` already linked admin <-> Mattermost `user` (verified: `GET /gateway/auth/me/identities` ->
`[{bridge_type:"mattermost", external_username:"user", external_user_id:"nhf459trfify8j8axfjhhwgzsy", ...}]`).
To link manually: `GET /gateway/collaborations/{bid}/directory?query=user` (409 until the bridge runs),
then `POST /gateway/collaborations/{bid}/identities {"external_user_id": ..., "username": "user"}`
(`SC/gateway/collaborations.py:643-740,815-943`).

### 3.5 Who can address whom here (the decisive consequence)

- Agents registered with `AGENT_REGISTRATION_TOKEN` are owned by `agent-bootstrap@switch.local`.
- Mattermost `user` is claimed by the **admin**, not by the bootstrap owner.
- Under the default policy, agent-to-agent mentions work (same owner -> `owner_agents`), but **`user` posting `@agent` in Mattermost is refused with ADDRESSING_DENIED_MESSAGE** (claimed, but not the owner).

Fix, pick one:
1. After registering, as gateway admin: `PUT /gateway/agents/{id}/addressing-policy {"policy": null}` for each agent (simplest; admin is tenant admin).
2. Or register with an admin-minted registration key (`POST /gateway/api-keys`) so the admin owns the agents and `user` matches `owner`. Note `SC/main.py:1697-1707` logs a warning that admin-owned agents carry admin-equivalent authority.

```python
import httpx, os

SWITCH = os.environ.get("SWITCH_URL", "http://localhost:18000")

def gateway() -> httpx.Client:
    c = httpx.Client(base_url=SWITCH, timeout=30)
    r = c.post("/gateway/auth/login", json={"email": os.environ["GATEWAY_ADMIN_EMAIL"],
                                             "password": os.environ["GATEWAY_ADMIN_PASSWORD"]})
    r.raise_for_status()
    c.headers["Cookie"] = f"switch_auth={r.cookies.get('switch_auth')}"   # robust to Secure cookies
    return c

gw = gateway()
by_name = {a["name"]: a for a in gw.get("/gateway/agents").raise_for_status().json()}
for name in ("translator", "fuzzer", "cutover-gate"):
    d = gw.put(f"/gateway/agents/{by_name[name]['id']}/addressing-policy",
               json={"policy": None}).raise_for_status().json()
    assert d["addressing_policy"] is None

# optional: admin-owned registration key instead of the bootstrap token
admin_reg_key = gw.post("/gateway/api-keys", json={"label": "stranglerroom"}).raise_for_status().json()["key"]
```

---

## 4. Event stream and heartbeat

### 4.1 Transports (same buffer, `H:703-763,1178-1312`)

1. WebSocket `GET /agents/{id}/connection/ws` (preferred; needs the `websockets` lib, httpx has no WS).
2. SSE `GET /agents/{id}/events` with `Accept: text/event-stream` + `POST /agents/{id}/connection/beat` (legacy, kept for compatibility; fine for httpx).
3. Same GET without that Accept = long poll: `{"events":[AgentEvent]}` or 204, no connection, no cursor. Do not use.

### 4.2 `GET /agents/{id}/events` query (`H:709-722`)

| Param | Default | Meaning |
|---|---|---|
| `connection_id` | required | Client-chosen, <= 64 chars (`AC:124`); reuse across reconnects. Missing -> 400. |
| `scope` | `single` | `single` = only claimed rooms (parks silently if none, `S:320-338`); `all` = every member room except rooms a sibling connection claimed (`AC:1639-1648`). |
| `filter` | `all` | `addressed` = only notifiable events (addressed `message`, `room_join` with `listening:true`; never `command`, `SC/bridges/agent/protocol/event_buffer.py:228-241`). `all` also streams room chatter (`addressed:false`) and `command`. |
| `start_from` | `head` | Integer cursor; `Last-Event-ID` header wins over it (`H:810`). `head` skips anything missed. |
| `start_at` | none | Revision-9 JSON position `{"s":n,"r:<room>":m}`; beats both above. |
| `rooms` | none | CSV room ids claimed with takeover (evicts other claims); non-member 403, sibling-claimed 409 (`H:979-1023`). |
| `expected_generation` | none | Reattach fence; mismatch -> 409 `taken_over`. |
| `protocol`, `protocol_accepts`, `client`, `client_version`, `spawn_capable` | none | Optional declarations. Undeclared is fine and keeps the cursor a single integer. |

Limit 32 connections per agent (`AC:118`). Reusing another agent's `connection_id` -> 409.
**[resolved]** There is no `cursor` query param.

Response 200 `text/event-stream`, headers `Cache-Control: no-cache`, `X-Accel-Buffering: no` (`H:1085-1093`).
Wire format (`S:100-112`):

```
id: <int>          (only on frames with a seq)
event: <name>
data: <compact JSON, one line>

```

Keepalive is the comment `: keepalive` after 15 s idle (`S:63`). Set read timeout > 15 s.

### 4.3 Control frames (no `id:`, never filtered)

`connection_state` is always first (`S:131-163`), exact keys:

```json
{"connection_id":"<uuid>","agent_id":"<id>","generation":1,"scope":"all","filter":"addressed",
 "spawn_capable":false,"rooms":[],"cursor":12884901890,"position":{"s":12884901890},
 "protocol":9,"heartbeat_interval_seconds":2.0,
 "server":{"version":"0.31.0","contracts":{"agent-protocol":{"speaks":9,"accepts":1}}},
 "client":{"speaks":null,"accepts":null,"artifact":null,"version":null}}
```

| `event:` | data |
|---|---|
| `gap` | `{from_sequence, resumed_at, resumed_position, rooms, all_rooms, reason}`: re-read context (`S:526-533`). |
| `evicted` | `{code, reason, room_id}`, then stream ends; `code` in `taken_over \| heartbeat_lapsed \| closed \| launch_superseded` (`S:166-172`; `SC/bridges/agent/protocol/liveness.py:19,44-58`). |
| `subscription_changed` | `{rooms:[...], reason:"subscription updated"}` |
| `room_released` | `{room_id, session_id}` (protocol >= 6 only) |

### 4.4 Domain events

`event:` is `message`, `command` or `room_join` (`T:197-201`). Data = `AgentEvent.model_dump(mode="json")`
(`T:275-282`: `{type, room_id, bridge_id, channel_type, payload}`) plus `sequence` (= the `id:`),
`position` (protocol >= 9 only) and `missed` (notifiable events only) (`S:426-441`).

Addressed room message (`MessagePayload`, `T:219-241`):

```json
{"type":"message","room_id":"<switch room id>","bridge_id":"54f217a2-...","channel_type":"channel_public",
 "payload":{"addressed":true,"sender":"<opaque participant id>","sender_name":"user","sender_kind":"user",
   "on_behalf_of":null,"message_id":"<id>","body":"@translator port slice 1","timestamp":1762315200000,
   "thread_id":null,"attachments":[],"opening_message":null,"sender_role":"owner"},
 "sequence":12884901891,"missed":{"count":0,"reason":null}}
```

- `timestamp` = epoch ms. `sender_kind` in `user|agent|platform|null`. `sender_role` in `owner|admin|member|null`.
- **No `mentions` field.** Mentions are only the raw `@name` text in `body`. Use `payload.addressed`.
- `addressed` is computed server-side (`SC/delivery/addressing.py:145-223`): `direct` room, `@<agent.name>` at token boundary (case-insensitive, `*`/`~` stripped, regex `@([A-Za-z0-9._-]+)`, `SC/clients/mentions.py:6-35`), a mention pill, `@alias`, or `@role` held. Then policy and budget gate it.
- Your own posts are never echoed back (`event_buffer.py:269-274`); other agents' posts are.
- `command`: `{command, args, user_id, user_name, thread_id}` (`T:244-253`). `room_join`: `{member, member_name, timestamp, listening}` (`T:256-265`).
- Unaddressed chatter (`filter=all`): same shape, `addressed:false`, no `missed`.
- Addressed messages are stored as own events regardless of connectivity and replay on reconnect for up to 24 h (`event_buffer.py:147`; `recipient.py:539-543`).

### 4.5 Heartbeat `POST /agents/{id}/connection/beat`

- Body `ConnectionBeatRequest {"connection_id": str, "generation": int|null}` (`SC/bridges/agent/api/schemas.py:141-159`). **[resolved]** `cursor`/`position` are accepted and ignored.
- Every 2.0 s; TTL 6.0 s (`liveness.py:16-17`; `AC:476-479`); reaper every 2 s (`SC/main.py:239,334-346`).
- 200 `{"ok": true, "rooms": [...], "cursor": <int>, "position": {...}}`.
- Always echo `generation` from `connection_state`.

| Status | detail | Action |
|---|---|---|
| 404 | `"connection X is not open; reconnect and resume from where you got to"` | Reopen with last cursor. |
| 409 | `{"code":"no_stream",...}` | Reopen. |
| 409 | `{"code":"unfenced",...}` | Reopen, beat with generation. |
| 409 | `{"code":"taken_over",...}` | **Terminal**: another stream holds the id. Stop, or use a new id. |

No ack endpoint: progress is what you send on reopen (`Last-Event-ID` / `start_from`). Dedupe by `sequence`.
Reopening a live id = takeover that keeps rooms; after lapse, re-declare `rooms=` on the reopen URL.
Backoff 1 s doubling to 30 s (as the official runtime).

### 4.6 WebSocket variant

Same query (no `Last-Event-ID`). Server sends `{"event": name, "data": {...}, "id": seq?}`, a
`{"event":"ping","data":{}}` every 2 s, `{"event":"refused","data":{status,detail}}` then close `4000+status`.
Client replies `{"type":"pong"}` (a `cursor` key is ignored; only `type` is checked, `H:1330-1355`),
which is the beat, fenced automatically. Close 4401 = unauthorized.

### 4.7 always_on

Only a presence policy: LIVE when any live connection exists (`SC/bridges/agent/protocol/statuses.py:43-45,168-173`).
Not live -> sender gets "I'm currently offline — my connector isn't reporting in..." (`recipient.py:202-206`).
Recommended listener: one connection `scope=all&filter=addressed`, beating.

### 4.8 httpx SSE client (listener + beat)

```python
import asyncio, json, os, uuid, httpx

SWITCH = os.environ.get("SWITCH_URL", "http://localhost:18000")

class Listener:
    def __init__(self, agent_id, api_key, on_event, scope="all", filt="addressed", rooms=()):
        self.aid, self.h = agent_id, {"Authorization": f"Bearer {api_key}"}
        self.on_event, self.scope, self.filt, self.rooms = on_event, scope, filt, list(rooms)
        self.conn_id = str(uuid.uuid4())
        self.cursor = self.gen = None
        self.seen, self.stop, self.ready = set(), False, asyncio.Event()

    async def beat(self, c):
        while not self.stop:
            await asyncio.sleep(2.0)
            if self.gen is None:
                continue
            r = await c.post(f"{SWITCH}/agents/{self.aid}/connection/beat", headers=self.h,
                             json={"connection_id": self.conn_id, "generation": self.gen})
            if r.status_code == 409 and (r.json().get("detail") or {}).get("code") == "taken_over":
                self.stop = True
            elif r.status_code != 200:
                self.gen = None                      # stream loop reopens

    async def stream(self, c):
        backoff = 1.0
        while not self.stop:
            params = {"connection_id": self.conn_id, "scope": self.scope, "filter": self.filt}
            headers = {**self.h, "Accept": "text/event-stream"}
            if self.rooms:
                params["rooms"] = ",".join(self.rooms)
            if self.cursor is not None:
                params["start_from"] = headers["Last-Event-ID"] = str(self.cursor)
            if self.gen is not None:
                params["expected_generation"] = str(self.gen)
            try:
                async with c.stream("GET", f"{SWITCH}/agents/{self.aid}/events",
                                    params=params, headers=headers) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode()
                        if r.status_code == 409 and "taken_over" in body:
                            self.stop = True; return
                        raise RuntimeError(f"open {r.status_code}: {body}")
                    backoff, ev, eid, data = 1.0, None, None, []
                    async for line in r.aiter_lines():
                        if line == "":
                            if data:
                                d = json.loads("\n".join(data))
                                if ev == "connection_state":
                                    self.gen = d["generation"]; self.ready.set()
                                elif ev == "evicted":
                                    self.stop = d["code"] == "taken_over"; break
                                elif eid is None or int(eid) not in self.seen:
                                    await self.on_event(ev, d)       # message/command/room_join/gap
                                    if eid is not None:
                                        self.seen.add(int(eid)); self.cursor = int(eid)
                            ev, eid, data = None, None, []
                        elif line.startswith(":"):
                            continue
                        elif line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("id:"):
                            eid = line[3:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
            except (httpx.HTTPError, RuntimeError):
                pass
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 30.0)

    async def run(self):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=45)) as c:
            await asyncio.gather(self.stream(c), self.beat(c))

# usage:
# async def on_event(ev, d):
#     if ev == "message" and d["payload"]["addressed"]:
#         print(d["room_id"], d["payload"]["sender_name"], d["payload"]["body"])
# asyncio.run(Listener(AGENT_ID, API_KEY, on_event).run())
```

---

## 5. Operations

### 5.1 Door

- `GET /agents/{id}/ops` -> `{"operations": {name: {"description", "input_schema"}}}` (`SC/bridges/agent/api/operations.py:264-270`).
- `POST /agents/{id}/ops/{operation}`, body = bare args object, response `{"result": <value>}` (`operations.py:273-342`).
- Header `X-Switch-Connection-Id: <live connection id>` binds the call to that connection's claimed room. Dead/unknown/foreign id -> 409 (`operations.py:196-226`).
- Args checked by signature: unknown key 400 `X does not accept: ...`, missing 400 `X requires: ...` (`operations.py:104-122`). Omit optional fields instead of sending null.
- Error map (`operations.py:327-340`): 404 unknown op; 400 `ValueError`/bad args (including "Room not found"); 403 `PermissionError` (e.g. not a member, `agent_core.py:1553-1557`); 409 bad connection; 422 guardrail; 429 budget; 500 other.

### 5.2 Room binding (main gotcha)

Room-bound ops resolve exactly one room from the connection (`OPS/context.py:100-145`):
no room -> 400 `Not connected to a room. Call connect_to_room first.`; several rooms -> 400
`This connection covers several rooms; the operation needs one`. **[resolved, new]** So a `scope=all`
listener with no claims cannot run room-bound ops. Use a per-room connection:
`scope=single&rooms=<room_id>` (a single-scope subscribe replaces its previous room, `H:1426-1440`).
Caveat: once a sibling connection claims room X, the `scope=all` listener stops receiving X
(`AC:1639-1648`), so also consume events on the per-room connection (use `filter=addressed` there too).

- Needs bound room: `post_message`, `send_targeted_message`, `list_participants`, `create_room_document`, `update_room_document`, `delete_room_document`, `load_internal_documents`, `list_references`, `list_linked_rooms`, `list_roles`, `define_role`, `edit_role`, `delete_role`, `assume_role`, `release_role`, `read_context` (without `room_id`).
- No connection needed: `create_room`, `list_rooms`, `list_all_rooms`, `get_room_detail`, `read_context` with `room_id`, `invite_agent_to_room`, `remove_agent_from_room`, `add_users_to_room`, `update_room`, `archive_room`, `list_agents`, `get_agent_detail`, `update_agent_detail`, `list_bridges`, templates.
- `connect_to_room {room_id}` needs a connection id header and always takes over (`OPS/definitions.py:246-326`).
- Alternatives without a connection: `POST /agents/{id}/message {room_id, content, metadata?}` -> `{"ok":true,"event_id"}` (ValueError -> **404** here, `H:452-473`); `GET /agents/{id}/rooms/{room_id}/history?since=&before=&limit=` -> `{events:[{sender,sender_name,body,timestamp}], has_more}` (`H:1653-1702`); `POST /agents/{id}/connection/subscribe {connection_id, room_id, takeover=false, generation?}` (`H:1426-1506`).

### 5.3 Catalogue (`OPS/definitions.py` line; `?` = optional)

Rooms: `list_rooms(include_archived=false)` 214, `connect_to_room(room_id, include_general_instructions=true)` 246,
`list_all_rooms` 1451, `get_room_detail(room_id)` 2368, `update_room(room_id, name?, description?, instructions?, admin_mode?, join_event_listeners?, bridge_id?, channel_type?, external_channel_id?, aliases?)` 2396,
`archive_room` 2477, `unarchive_room` 2505, `create_room` 877, `list_participants` 738,
`invite_agent_to_room(room_id, agent_name, include_subagents=false)` 1369, `remove_agent_from_room` 1393,
`add_users_to_room(room_id, user_names)` 1418, `create_room_from_yaml(yaml, inputs?)` 1551, `list_bridges` 1345,
`link_rooms(source_room_id, target_room_id, label)` 1292, `unlink_rooms` 1320, `list_linked_rooms` 496,
`list_room_groups` 1479, `create_room_group(name, description?, color?, parent_group_name?)` 1499, `get_room_group_detail` 1532.

Messaging: `post_message(body, thread_id?)` 768, `send_targeted_message(body, target_names?, target_roles?, thread_id?)` 795, `read_context(limit=50, since?, before?, room_id?)` 640.

Documents: `list_references` 465, `load_internal_documents(ids)` 522, `create_room_document(name, description, instructions, content)` 549,
`update_room_document(document_id, name?, description?, instructions?, content?)` 589, `delete_room_document(document_id)` 622,
`list_reference_types` 1143, `create_reference` 1163, `attach_reference_to_room` 1227, `list_all_references` 1248.

Roles: `list_roles` 1016, `get_role_detail(room_id, role_name)` 1036, `define_role(name, instructions, exclusive=false)` 1060, `edit_role` 1081, `delete_role` 1100, `assume_role(role)` 1112, `release_role` 1134.

Templates: `get_template_guide` 1796, `list_templates(query?, kind?)` 1812, `get_template(template_id)` 1840, `run_template(template_id, inputs?, agents?)` 1876, `save_template(name, description, yaml, visibility="private", bypass_warnings=false)` 1979, `update_template` 2032, `delete_template` 2080.

Agents: `list_agents(name_contains?, owner_name?, known_agent_type?)` 2097, `get_agent_detail(agent_id)` 2134, `request_agent_deletion` 2163, `update_agent_detail(agent_id, addressing?, ...)` 2202 (not on self), `get_my_context` (`OPS/my_context.py:45`), `set_agent_service` / `remove_agent_service` (`OPS/agent_services.py:43,113`).
Gated (agent management on): `list_machines, get_advanced_config, create_agent, list_managed_agents` (`OPS/agent_management.py`).
Not registered: task ops (`delegate_task` etc.), despite the stale docs (`core/tests/switch_core/bridges/agent/test_mcp_tool_surface.py:36-68`).

### 5.4 `create_room` (`OPS/definitions.py:877-1012`; `RoomCreateConfig` `SC/room_service.py:75-145`)

```json
{"name": "Strangler Migration", "description": "translator / fuzzer / gate + human",
 "agent_names": ["translator", "fuzzer", "cutover-gate"], "user_names": ["user"],
 "channel_type": "channel_public"}
```

Optional: `bridge_id` (null = default bridge), `internal_only`, `admin_mode`, `instructions`, `roles: [{name, instructions, exclusive}]`,
`aliases: {agent_name: alias}`, `group_name`, `read_visibility`/`write_visibility`, `linked_rooms`, `join_event_listeners`, `include_subagents_for`, `reference_ids`.

Response: `{"id": "<room id>", "name", "transport_room_id", "matrix_room_id", "failed_attachments": []}`.

- The caller is **not** added automatically: include your own agent name.
- Any unknown agent name fails the whole call: 400 `Unknown agents: x` (`SC/room_service.py:942-950`). Register peers (e.g. codex/opencode/agy wrappers) first.
- `user_names` are Mattermost usernames or emails; unresolved ones land in `failed_attachments` `{"kind":"user","id":...,"error":...}` without aborting (`room_service.py:451-491`). Only listed humans become Switch participants (targetable).
- Default bridge used unless `internal_only` (`room_service.py:381-412`); not running -> 400.
- Mattermost channel: slug `re.sub(r"[^a-z0-9-]","-", name.lower()).strip("-")[:64]`, `display_name=name`, `purpose=description`, type `P` for private else `O`; collision retry `<slug[:55]>-<8 hex>` (`MM:1871-1926`). `default_member` (`user`) is always added (`MM:1925,1943-1962`). Agent bots are added (`MM:2016-2047`).
- One concurrent `create_room` per agent (`SC/agent_runs.py:154-198`).
- `direct` needs exactly one agent and one user (`room_service.py:278-287`).
- Gateway alternative: `POST /gateway/rooms` (201 `RoomDetail`, 502 platform refusal, `SC/gateway/rooms.py:281-427`).

### 5.5 Sending and mentions

- `post_message {"body", "thread_id"?}` -> `{"event_id"}`. Plain `@name` in the body *does* address that agent.
- `send_targeted_message {"body", "target_names": [...], "target_roles": [...], "thread_id"?}` -> `{"event_id", "target_statuses": {"fuzzer": "live", ...}}`. The server prepends `@a @b ` to the body (`agent_core.py:2037-2214`, built at 2180): do not add `@` yourself. Targets must be room participant names (agent name, alias, or Mattermost username of a joined human); unknown -> 400 listing participants. Humans are omitted from `target_statuses`. Statuses: `live, disconnected, awaiting_manual_poll, no_session, dormant, not_permitted` (`T:106-118`); `everyone` -> `@channel`, wakes no agent.
- `thread_id` = any message id in the thread (normalised to root).
- No-mention routing (thread/channel follow-up) needs feature flag `follow_the_conversation` (`SC/delivery/conversation.py:117-197`; `SC/config.py:240,1273`): off here, so always mention.

### 5.6 Room documents (`SC/bridges/resource/service.py`)

- `create_room_document {"name","description","instructions","content"}` -> `{"document_id"}`. Content <= 1 MiB UTF-8, name unique per room (`service.py:58,992-999`), stored verbatim.
- `update_room_document {"document_id", ...}` -> `{"document_id", "status":"ok"}`; creator agent only, else 403 (`service.py:1093-1096`).
- `delete_room_document {"document_id"}` -> `{"document_id","status":"ok"}`, creator only.
- Read: `load_internal_documents {"ids":[...]}` -> `[{"id","name","description","content","room_seq":0,"source_event_id":null}]`; ids not attached to the bound room -> 400 (`service.py:485-503`).
- Find ids: `list_references` / `connect_to_room` -> `documents: [{id, name, description, instructions, scope, created_by_agent_id}]`.
- Each create/update/delete posts a visible notice `📄 created room document “<name>”.` from the agent (`agent_core.py:4382-4469`): no `@` in doc names.

### 5.7 Reading

- `read_context {"limit":50, "since"?, "before"?, "room_id"?}` -> `{"threads":[{"root":E,"replies":[E]}], "truncated", "oldest_timestamp"}`; E = `{id, kind, sender, sender_name, body, timestamp(ms), attachments, sender_kind?, on_behalf_of?}`; `since/before` are ISO-8601 (`agent_core.py:2642-2747`).
- `list_participants` -> `[{id, name, type:"agent"|"user", status, alias}]`; human `id` = ExternalUser id.
- `get_room_detail` -> includes `bridge_id`, `external_channel_id`, `agent_names`, `connected_user_names`, `aliases`, `roles` (`T:160-195`).

### 5.8 httpx ops snippet

```python
import httpx, os

SWITCH = os.environ.get("SWITCH_URL", "http://localhost:18000")

def op(agent: dict, name: str, args: dict | None = None, conn_id: str | None = None):
    h = {"Authorization": f"Bearer {agent['api_key']}"}
    if conn_id:
        h["X-Switch-Connection-Id"] = conn_id
    r = httpx.post(f"{SWITCH}/agents/{agent['id']}/ops/{name}", json=args or {}, headers=h, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"{name} -> {r.status_code}: {r.text}")
    return r.json()["result"]

# translator, fuzzer, gate = creds(...) from section 2
room = op(translator, "create_room", {
    "name": "Strangler Migration", "description": "translator / fuzzer / gate + human",
    "agent_names": ["translator", "fuzzer", "cutover-gate"], "user_names": ["user"],
    "channel_type": "channel_public"})
room_id = room["id"]; assert not room["failed_attachments"], room

# room-bound ops: open Listener(..., scope="single", rooms=[room_id]) (section 4.8), await .ready, then:
# op(translator, "send_targeted_message", {"body": "slice 1 ready", "target_names": ["fuzzer"]}, conn_id=l.conn_id)
# doc = op(translator, "create_room_document", {"name": "migration-plan", "description": "slices",
#          "instructions": "keep current", "content": "# Plan\n"}, conn_id=l.conn_id)
# op(translator, "update_room_document", {"document_id": doc["document_id"], "content": "# Plan v2\n"}, conn_id=l.conn_id)

# connection-free send + read:
httpx.post(f"{SWITCH}/agents/{translator['id']}/message",
           headers={"Authorization": f"Bearer {translator['api_key']}"},
           json={"room_id": room_id, "content": "@fuzzer slice 1 is ready"}).raise_for_status()
print(op(translator, "read_context", {"limit": 20, "room_id": room_id}))
```

---

## 6. Mattermost bridge and driving it as user `user`

### 6.1 What `setup.py` seeded (`INFRA/deploy/shared_resources/setup.py`)

1. Mattermost admin (`POST /api/v4/users`, first user = system admin), login token from response header `Token` (`:80-110`), team `switch` (`:115-148`), human `user` added to the team (`:152-180`).
2. Gateway login with cookie replay (`:186-222`).
3. Bridge `POST /gateway/collaborations {"bridge_type":"mattermost","display_name":"Mattermost","connection_config":{"url","admin_user","admin_password","team_name","default_member":"user","callback_base_url"},"set_as_default":true,"preconfigured":true}` (`:291-323`; `SC/gateway/schemas.py:671-687`). Admin only (`SC/gateway/collaborations.py:334`). An existing bridge's `url` is never re-patched by setup (`:246-288`).
4. Admin claims Mattermost `user` (`:357-407`).

`MattermostConnectionConfig` (`MM:236-278`): `url` must include the port (no redirects followed, `SC/bridges/collaboration/mattermost/http_client.py:79-86`).

### 6.2 Inbound path (`MM:2634-2746`; `SC/bridges/collaboration/collaboration_core.py:967-1152`)

- Each agent = a bot with username == agent name, own token, own websocket (`MM:2222-2331`). A bot only sees channels it is a member of.
- Dropped: system posts (`post.type` non-empty), posts by any bridge bot **or the Mattermost admin user** (`MM:2664`), duplicates.
- Text starting with `!` = command; else message (`MM:2707-2746`). Only `posted` and `user_added` are handled; **edits are not re-delivered**.
- A puppet client posts the text into the Switch room as markdown, thread root mapped; then addressing (3.3) and policy (3.5) apply.

### 6.3 Outbound

- Agent text posted by its own bot `POST /posts {channel_id, message, root_id?}`, threaded under the mapped post (`MM:830-845,1086-1091`; `collaboration_core.py:2437-2468`).
- Extra posts you will see: typing, `eyes` reaction while working (`MM:1766-1830`), status/card posts edited in place (`MM:1309-1362`, props `switch_publication`), system notices from bot `switch-admin` (`MM:2399-2417`). Filter by bot `user_id`; status posts change via `update_at`.

### 6.4 Channel lookup for a Switch room

- Exact: `GET /gateway/rooms/{room_id}` -> `external_channel_id` (Mattermost channel id), `external_channel_url` = `mattermost://<host:port>/<team>/channels/<slug>` (`SC/gateway/rooms.py:815-829`; `MM:1973-1999`).
- Or Mattermost: `GET /api/v4/teams/name/switch/channels/name/{slug}` (what the harness does, `core/tests/e2e/follow_the_conversation/harness.py:244-251`).

### 6.5 REST as `user` (httpx)

```python
import os, time, httpx

MM = os.environ.get("MATTERMOST_URL", "http://localhost:18065")
SWITCH = os.environ.get("SWITCH_URL", "http://localhost:18000")

def mm_login(user=os.environ.get("MATTERMOST_USER", "user"),
             pw=os.environ["MATTERMOST_USER_PASSWORD"]) -> httpx.Client:
    c = httpx.Client(base_url=f"{MM}/api/v4", timeout=15)
    r = c.post("/users/login", json={"login_id": user, "password": pw}); r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.headers['Token']}"     # token is a response HEADER
    c.me = r.json()["id"]
    return c

def channel_id_for(gw: httpx.Client, room_id: str) -> str:            # gw = gateway() from 3.5
    return gw.get(f"/gateway/rooms/{room_id}").raise_for_status().json()["external_channel_id"]

def say(mm: httpx.Client, cid: str, text: str, root_id: str = "") -> dict:
    return mm.post("/posts", json={"channel_id": cid, "message": text, "root_id": root_id}
                   ).raise_for_status().json()

def wait_for_bot(mm: httpx.Client, cid: str, bot: str, after: dict, timeout=120) -> list[dict]:
    bot_id = mm.get(f"/users/username/{bot}").raise_for_status().json()["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = mm.get(f"/channels/{cid}/posts", params={"since": after["create_at"]}).raise_for_status().json()
        hits = sorted((p for p in data["posts"].values()
                       if p["user_id"] == bot_id and p["create_at"] > after["create_at"]
                       and not p.get("type")), key=lambda p: p["create_at"])
        if hits:
            return hits
        time.sleep(2)
    raise TimeoutError(f"no reply from {bot}")

# mm = mm_login(); cid = channel_id_for(gateway(), room_id)
# p = say(mm, cid, "@translator port slice 1 of billing-api")
# print([x["message"] for x in wait_for_bot(mm, cid, "translator", p)])
```

`user` is already in Switch-created channels as `default_member`; no self-join needed.

---

## 7. Template v2 YAML and validation

### 7.1 Format (`SC/template_v2.py`, `SC/rooms_yaml.py`, `SC/template_lint.py`)

- v2 if `version: 2` or `resources:` present (`template_v2.py:67-68`). Top-level keys: `version, params, resources, form` (`template_lint.py:297`).
- `resources.<key>.type` in `agent | room | group`; keys match `[A-Za-z_][A-Za-z0-9_]*` (`template_v2.py:48,182-186`).
- Params `ParamSpec` (`rooms_yaml.py:109-207`, `extra="forbid"`): `type` in `string, number, boolean, enum, agent, bridge, room, user, provider, location, machine, directory`; `default`, `required`, `input: ask|advanced|fixed`, `enum`, `multiline`, `pattern` (fullmatch), `min/max`, `label`, `description`, `if` (v2). List defaults (chains, `$first`) only for `agent, bridge, room, provider, location, machine`; `required:false` without default only for `string, bridge, room`.
- Placeholders `\{(\$?[A-Za-z_][A-Za-z0-9_]*)\}` (`rooms_yaml.py:72`); built-ins `{$creator}, {$creator_email}, {$date}, {$timestamp}`, plus machine-filled `{$agents_dir}`; `{key.name}` = a resource's created name.
- `if:` = `"{p}"`, `"{p} == x"`, `"{p} != x"` on resources, params, or field values `[{if, value}, ...]` (`template_v2.py:52-54,99-133`).
- Room (`RoomSpec`, `rooms_yaml.py:418-437`, **no** `extra=forbid`: typos silently ignored): `name`, `description` required; `instructions, bridge, channel_type, read/write_visibility, agents, users, roles, references, docs, aliases`; v2 extras `kickoff`, `group`. `docs: [{name, description, instructions, content}]` all four required (`rooms_yaml.py:411-415`). `users:` needs a bridge (`rooms_yaml.py:1002-1005`).
- Agent resource: `instructions, provider, machine, directory` required; `addressing` default **`owner-agents`** (code `SC/agent_templates.py:751`; docs say `owner`): `owner|owner-agents|anyone|{anyone_in:[room keys]}` (`SC/template_runs.py:55-59`; `template_v2.py:299-314`).
- More than one room needs a `group` resource; at most one group (`template_v2.py:344-387`). Group links need a `label` (v1 parse).

### 7.2 Running here: agent management is off

Agent resources are created as managed agents on an enrolled controller machine. With
`FEATURE_FLAGS_ENABLED` empty every plan with agents is refused `agent_management_off`
(`SC/template_runs.py:229-247`). A template **without** agent resources passes (empty plan), and
strings in `agents:` that are not resource keys pass through as names of existing agents
(`template_v2.py:283-291`). So on this stack: register agents via `POST /agents` (section 2), then
use a room-only v2 template.

```yaml
version: 2
params:
  legacy_system: { type: string, default: billing-api, pattern: "[a-z][a-z0-9-]*" }
  bridge: { type: bridge, default: [$first], required: false, input: advanced }
resources:
  strangler_room:
    type: room
    name: strangler-migration
    description: Strangler-fig migration of {legacy_system}.
    bridge: "{bridge}"
    agents: [translator, fuzzer, cutover-gate]        # names of already-registered agents
    aliases: { cutover-gate: gate }
    users:
      - if: "{bridge}"
        value: ["{$creator}"]                          # admin -> linked Mattermost "user"
      - value: []
    roles:
      - { name: approver, exclusive: true, instructions: Only the human holding this role approves a cutover. }
    instructions: |
      Pipeline: @translator -> @fuzzer -> @gate -> human approver.
    docs:
      - name: migration-plan
        description: Slice list and status for {legacy_system}.
        instructions: Translator keeps this current; the gate reads it before PROPOSE.
        content: |
          # {legacy_system} strangler migration plan
          Created {$date} by {$creator}.
          | Slice | Status | Parity % | Gate |
          |---|---|---|---|
    kickoff: |
      @translator list the slices of {legacy_system} in migration-plan, smallest first.
```

Kickoff is posted on behalf of the creator (admin) and judged as that person: with bootstrap-owned
agents under the default policy it would be refused, so open the agents' policies first (3.5).
The full agent-creating annotated example (`strangler-migration` with `{prefix}`, provider chains,
`anyone_in`) is at
`/tmp/claude-0/-root-another-hackathon/41671522-265f-4090-a55b-52ffb2ec73d0/scratchpad/strangler-migration.template.yaml`;
it only runs with agent management enabled and an enrolled controller.

### 7.3 Validate

Offline (no DB), in the repo venv:

```bash
cd /root/another_hackathon/.switch-infra/core && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I - tmpl.yaml <<'EOF'
import sys
from switch_core.template_lint import lint_template
from switch_core.agent_templates import plan_template, template_kind
from switch_core.rooms_yaml import RoomYamlService
text = open(sys.argv[1]).read()
r = lint_template(text)
for f in r.errors + r.warnings: print(f.code, f.subject, f.message)
b = {"$creator": "user", "$creator_email": "u@x", "$date": "2026-11-05", "$timestamp": "1"}
plan = plan_template(text, {}, {"bridge": "Mattermost"}, builtins=b, machines=[])
svc = RoomYamlService.__new__(RoomYamlService)
print(template_kind(text), svc.parse_template(plan.document, inputs=plan.inputs or None, builtins=b).spec)
EOF
```

Lint is weak (`template_lint.py:1-18`): only `empty, invalid_yaml, not_a_mapping` block an upload
(`:78`); missing doc fields, missing instructions, min/max violations and unlabeled links surface only in
`plan_template`/`parse_template`. Lint codes: errors `invalid_condition, invalid_resource_type,
invalid_resource_key, no_resources, resource_not_a_mapping, rooms_without_group, invalid_param_spec,
enum_without_choices, default_not_in_enum, param_has_no_spec`; warnings `unknown_reference,
undeclared_placeholder, undeclared_condition, unknown_builtin, unused_param, unknown_param_field,
unknown_top_level_key, agent_runtime_unsaid`. `save_template` refuses any warning unless `bypass_warnings`.

Over HTTP (cookie auth, `SC/gateway/templates.py:370-406`):

- `POST /gateway/templates/validate {"content": yaml}` -> `{ok, blocked, errors:[{code,message,subject,blocking}], warnings}`.
- `POST /gateway/templates {name, description, kind, content, read_visibility?, write_visibility?}` -> 201 (413 > 1 MiB, 422 blocking lint, 409 duplicate name).
- Run: `POST /gateway/rooms/from-template-document {"template": yaml, "template_name": "...", "inputs": {...}, "filled": {}}` (`SC/gateway/rooms.py:619-647`) -> 201 `{"created_agents":[...], "room":{room_id, room_name, created_document_ids, role_names, failed_attachments}}`; 409 `{code:"agents_not_created", problems:[{slot,code,message}]}` or `{code:"run_undone", left_behind}`; 400 bad param/spec; 502 bridge.
- `POST /gateway/rooms/from-yaml` rejects v2 (400 "YAML must have a top-level 'room:' or 'group:' mapping", `rooms_yaml.py:609`).
- Agent-op equivalents: `create_room_from_yaml {yaml, inputs?}`, `run_template`.

```python
gw = gateway()                                       # section 3.5
text = open("/root/another_hackathon/targets/strangler-room.template.yaml").read()   # your file
v = gw.post("/gateway/templates/validate", json={"content": text}).json()
assert v["ok"] and not v["blocked"], v
r = gw.post("/gateway/rooms/from-template-document",
            json={"template": text, "template_name": "strangler-migration", "inputs": {"legacy_system": "billing-api"}})
print(r.status_code, r.json())                       # 409 -> r.json()["problems"]
```

---

## 8. Gotchas checklist

- [ ] **G1 Ownership vs. `user`.** Bootstrap-token agents are owned by `agent-bootstrap@switch.local`; `user` is linked to the admin. Mattermost `@agent` from `user` is refused until you `PUT .../addressing-policy {"policy": null}` (or register with an admin-minted key).
- [ ] **G2 Ports.** Switch is `:18000` (gateway under `/gateway` on the same port), Mattermost `:18065`, callbacks `:18081`. Port 8000 is another app. Read `SWITCH_URL` from `/root/another_hackathon/.state/switch.env`.
- [ ] **G3 Gateway auth** is cookie `switch_auth` only; replay as `Cookie:` header. Agent routes are Bearer only.
- [ ] **G4 Error bodies differ:** middleware 401 plain text; handlers JSON `{"detail": ...}`; structured `{"detail":{"code","message"}}` for connection 409s.
- [ ] **G5 No `mentions` field and no `cursor` query param.** Use `payload.addressed`, `body`, and `Last-Event-ID`/`start_from=<last sequence>`.
- [ ] **G6 Beat** every 2 s with `{connection_id, generation}`; TTL 6 s. 404/`no_stream`/`unfenced` -> reopen; `taken_over` -> stop.
- [ ] **G7 Room-bound ops** need `X-Switch-Connection-Id` of a live connection with exactly one claimed room (`scope=single&rooms=<id>`). A `scope=all` listener loses rooms that a sibling connection claims.
- [ ] **G8 `create_room`:** list your own agent; unknown agent name = 400 for the whole call; humans by Mattermost username; check `failed_attachments`.
- [ ] **G9 `send_targeted_message`** adds `@` itself; targets must be room participants; humans not in `target_statuses`.
- [ ] **G10 Room docs:** unique name, 1 MiB, creator-only edit/delete, read only via `load_internal_documents`, each change posts a visible notice.
- [ ] **G11 `update_agent_detail`** cannot target the caller itself (403) and needs the same owner.
- [ ] **G12 Rotation** (`overwrite=true`) wipes `metadata`/tools/models not resent; old key dies at once; different token-owner gets 409.
- [ ] **G13 Never post as Mattermost `admin`** (ignored, `MM:2664`); edits are not re-delivered; `!` prefix is a command.
- [ ] **G14 Always mention.** `follow_the_conversation` is off; unmentioned messages wake nobody (except `direct` rooms).
- [ ] **G15 Templates:** agent management is off here -> only room-only v2 templates with pre-registered agents; v2 cannot go to `/gateway/rooms/from-yaml`; `RoomSpec` typos are silent; `users:` needs a bridge; kickoff is judged as the creator.
- [ ] **G16 Refusal breaker:** 8 identical 401/403/404/426 refusals on `/agents/...` within 120 s -> 429 with `Retry-After` 60 s doubling to 900 s (`SC/refusal_breaker.py:44-52`). Do not hot-loop on a dead key. Bare `POST /agents` is exempt.
- [ ] **G17 Loops:** you never get your own posts, but other agents' posts that mention you are addressed; auto-replies carry `AUTO_REPLY_FLAG` and do not trigger refusals.
- [ ] **G18 Gaps:** on a `gap` frame re-read context (`read_context`) before acting; own events are retained 24 h.
- [ ] **G19 Stale docs:** `docs/official/internals/agent-protocol.md` lists retired task events/ops and says beats carry a cursor. Trust the code.
