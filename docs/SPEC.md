# StranglerRoom — Build Contract (SPEC)

This is the single source of truth every builder (human, Claude, codex, opencode, agy) implements against.
If code and this file disagree, this file wins until it is deliberately amended.

Python 3.12, project venv at `.venv` (`fastapi==0.115.12`, `uvicorn[standard]==0.34.2`, `httpx==0.28.1`,
`hypothesis==6.131.0`, `pytest==8.3.5`, `web.py==0.62`, `pydantic==2.11.4`, `pyyaml`, `httpx-sse`).
Run tests with `.venv/bin/pytest`. Repo root = `/root/another_hackathon` (call it `$ROOT`).
All imports are rooted at `$ROOT` (tests run with `rootdir=$ROOT`, `pythonpath = .` in `pytest.ini`).

## 0. Ports & processes

| Port | Process | Module |
|------|---------|--------|
| 9001 | Legacy Open Library (web.py / cheroot WSGI) | `python -m targets.legacy_openlibrary.server --port 9001` |
| 9002 | Modern FastAPI (uvicorn `--reload`, watches `targets/modern_fastapi/app`) | `python -m targets.modern_fastapi.server --port 9002` |
| 9000 | Strangler reverse proxy | `python -m targets.proxy.router --port 9000` |
| 18000 | Switch core API (native; 8000 in docker mode) | env `SWITCH_URL` |
| 18065 | Mattermost | env `MATTERMOST_URL` |

All services bind `127.0.0.1`. Env overrides: `LEGACY_URL` (default `http://127.0.0.1:9001`),
`MODERN_URL` (`http://127.0.0.1:9002`), `PROXY_URL` (`http://127.0.0.1:9000`).

## 1. Shared dataset — `targets/common/dataset.py`

Both targets read the same seed data (as both real stacks read the same Open Library DB), each process keeping
its own mutable copy.

```python
SEED_LISTS: dict[tuple[str, str], dict]   # (username, list_id) -> list record
SEED_EDITIONS: dict[str, dict]            # olid ("OL7353617M") -> edition record
USERS: set[str]                           # {"george08", "openlibrary", "mekBot"}
class Store:                              # thread-safe (threading.Lock)
    def __init__(self): self.reset()
    def reset(self) -> None               # deep-copies SEED_* into self.lists / self.editions
    def user_exists(self, user) -> bool
    def get_list(self, user, list_id) -> dict | None
    def user_lists(self, user) -> list[dict]          # sorted by list_id natural order (OL1L < OL2L < OL10L)
    def apply_seed_changes(self, user, list_id, add: list[str], remove: list[str]) -> dict  # returns updated list
    def get_edition(self, olid) -> dict | None
```

List record: `{"key": "/people/george08/lists/OL1L", "name": str, "description": str, "seeds": [str, ...], "last_update": "2024-03-01T10:00:00"}`.
Seeds are keys starting with `/works/`, `/books/`, `/authors/` or `/subjects/`.
Edition record: `{"key": "/books/OL7353617M", "title": str, "authors": [str], "publish_date": str, "price": float}` (prices like 12.99, 7.49, 0.1, 19.95, 3.33 — include at least one price whose `round(p*q, 2)` is float-sensitive, e.g. 0.1, 3.33).

Fixed data (exact):
- `george08/OL1L` "Sci-Fi Classics" — exactly, in order: `/works/OL45883W`, `/books/OL7353617M`, `/authors/OL26320A`, `/books/OL1M`, `/subjects/science_fiction`
- `george08/OL2L` "Reading 2025" — 2 seeds (`/works/OL27258W`, `/books/OL2M`)
- `george08/OL3L` "Empty Shelf" — 0 seeds
- `openlibrary/OL10L` "Staff Picks" — 6 seeds (≥3 `/books/` editions)
- `mekBot/OL20L` "Bot Import Queue" — 1 seed
- Editions: `OL7353617M`, `OL1M`, `OL2M`, `OL3M`, `OL4M`, `OL5M` (all with prices); every `/books/` seed above exists in `SEED_EDITIONS`.

`apply_seed_changes`: appends each `add` seed not already present (order preserved), removes each `remove` seed present, sets `last_update` to the fixed string `"2025-01-01T00:00:00"` (deterministic, so both targets agree).

## 2. Endpoint contract (the LEGACY behaviour = ground truth)

`slug(name)` = lowercase, non-alnum runs → `-`, strip `-`.
`legacy_int(v, default)`: `None`→default; `"none"`/`"all"` (case-insens.) → `None` (unlimited); else `int(v.strip())`, and on ValueError → default.
`legacy_bool(v)`: `None`→False; else `v.strip().lower() not in {"", "0", "false", "no", "off", "n", "f", "none"}`.
Paging: `entries[offset : (offset + limit) if limit is not None else None]` with plain Python slicing (negative values behave like Python slicing — identical in both targets, NOT a divergence).

### E1 `lists_json` — `GET /people/{user}/lists` and `GET /people/{user}/lists.json`
Query: `limit` (legacy_int, default 100), `offset` (legacy_int, default 0; `None` treated as 0), `details` (legacy_bool).
Unknown user → `404 {"error": "not_found", "key": "/people/{user}"}`.
200 → `{"links": {"self": "/people/{user}/lists.json", "user": "/people/{user}"}, "size": <total lists>, "entries": [E...]}`
entry: `{"url": key, "full_url": key + "/" + slug(name), "name": name, "seed_count": len(seeds), "last_update": ...}`; if details also `"description"` and `"seeds"`.

### E2 `list_seeds` GET — `GET /people/{user}/lists/{list_id}/seeds[.json]`
Unknown user or list → **legacy quirk D-3**: `200 {"error": "not_found", "entries": []}`.
200 → `{"links": {"self": key + "/seeds.json", "list": key}, "size": n, "entries": [{"url": seed, "type": <"work"|"edition"|"author"|"subject">}...]}` (type from prefix: /works/→work, /books/→edition, /authors/→author, /subjects/→subject).

### E3 `list_seeds` POST — `POST /people/{user}/lists/{list_id}/seeds[.json]`, JSON body `{"add": [str], "remove": [str]}` (both optional)
Order of checks (legacy):
1. unknown user/list → D-3 body `200 {"error": "not_found", "entries": []}`
2. body parsed with `json.loads(web.data() or b"{}")`; `add = data.get("add", [])`, `remove = data.get("remove", [])`
3. any seed (add or remove) without a valid prefix → `400 {"error": "invalid_seed", "seed": s}` (first offender, add before remove)
4. legacy analytics line `churn = len(remove) / len(add + remove)` → **ZeroDivisionError when both empty → web.py unhandled 500** (`text/html`, body "internal server error") = **quirk D-5**
5. apply → `200 {"success": true, "size": <new seed count>, "added": <n actually added>, "removed": <n actually removed>}`

### E4 `list_editions_json` — `GET /people/{user}/lists/{list_id}/editions[.json]`
Query `limit` (default 50), `offset` (default 0) via legacy_int. Unknown → D-3 body (200).
200 → `{"links": {"self": key + "/editions.json", "list": key}, "size": <n edition seeds>, "entries": [edition records (no "price") for /books/ seeds, in seed order, paged]}`

### E5 `edition_quote_json` — `GET /books/{olid}/quote[.json]?quantity=N`
`quantity` = legacy_int(default 1) (non-int → 1; `none`/`all` → 1).
Unknown olid → `404 {"error": "not_found", "key": "/books/{olid}"}`.
Range: legacy allows **-10 ≤ q ≤ 100** (negative = credit reversal used by warehouse scripts = **quirk D-4**); outside → `400 {"error": "quantity_out_of_range", "min": -10, "max": 100}`.
200 → `{"key": "/books/{olid}", "title": t, "unit_price": p, "quantity": q, "total": round(p * q, 2), "currency": "USD", "type": "credit_reversal" if q < 0 else ("noop" if q == 0 else "sale")}`

All JSON responses: `Content-Type: application/json` (no charset suffix required; comparison strips params).

## 3. The five quirks (what a textbook FastAPI v1 port gets wrong)

| ID | quirk tag | Legacy | Modern v1 (naive) | Demo decision |
|----|-----------|--------|-------------------|---------------|
| D-1 | `json_suffix` | `/….json` aliases served | `.json` routes missing → `404 {"detail":"Not Found"}` | preserve |
| D-2 | `loose_query` | `limit=none`, `limit=`, `details=maybe`, `quantity=abc` coerced | strict `int`/`bool` → `422` | preserve |
| D-3 | `missing_resource_200` | unknown list → `200 {"error":"not_found","entries":[]}` | `404 {"error":"not_found","entries":[]}` | deprecate (404 becomes contract) |
| D-4 | `non_positive_quantity` | quote `quantity` in −10..0 → 200 | `PositiveInt` → `422` | preserve |
| D-5 | `empty_body_500` | POST seeds with empty add+remove → `500 text/html` | pydantic validator → `422` | deprecate (422 becomes contract) |

Modern v1 is otherwise byte-for-byte equivalent in JSON (same keys, values, floats, statuses, incl. unknown-user 404 bodies, invalid_seed 400, quantity_out_of_range 400 for q > 100 — note q < −10 on v1 is 422 (non-positive) and is classified D-4).

## 4. Modern target — `targets/modern_fastapi/`

- `app/main.py` (static, hand-written): FastAPI app; `GET /health` → `{"status":"ok"}`; `GET /_meta` → `{"rev": sha256(lists.py bytes)[:12] or null, "routes": [sorted "METHOD path" of non-underscore routes]}`; `POST /_admin/reset` → resets its Store, `{"reset": true}`; includes `app.lists.router` **if** `app/lists.py` exists and imports (wrap in try; on failure `/_meta` shows `"import_error": str`).
- `app/lists.py` — **generated by @translator** (absent initially; the repo ships `app/lists.py` absent and `.gitignore`s it).
- `app/store.py` — module-level `STORE = Store()`.
- `server.py` — `uvicorn.run("targets.modern_fastapi.app.main:app", host, port, reload=True, reload_dirs=[<app dir>])`.

## 5. Legacy target — `targets/legacy_openlibrary/`

Faithful web.py shape so @translator can AST-extract it:
- `infogami_shim.py` — tiny `delegate.page` base class + registry; `build_app()` builds a `web.application` whose url patterns are `path + r"(?:\.json)?"` for classes with `encoding = "json"` (this is how infogami serves the implicit `.json` alias). Handlers get regex groups as args.
- `openlibrary/plugins/openlibrary/lists.py` — classes `lists_json`, `list_seeds` (GET+POST), `list_editions_json`, `edition_quote_json`, each `class X(delegate.page): path = r"..."; encoding = "json"`, using `web.input(...)`, `web.data()`, `web.header("Content-Type", "application/json")`, `raise web.HTTPError("404 Not Found", data=json.dumps(...))` / `web.notfound(...)` etc.
- Internal endpoints: `POST /_admin/reset` → `{"reset": true}`; `GET /health` → `{"status": "ok", "stack": "web.py"}`.
- `server.py` — `web.httpserver.runsimple`-style cheroot server on `--port` (default 9001), `numthreads=30`.
- web.py's default 500 handler must stay in effect for D-5 (web.config.debug = False).

## 6. Proxy — `targets/proxy/router.py`

ASGI app (no framework required; Starlette OK) + `httpx.AsyncClient` upstream forwarding of method, path, query, body, headers (minus hop-by-hop). Adds response header `X-Strangler-Upstream: legacy|modern`.
State file `ROUTER_STATE_FILE`, default `<STRANGLER_STATE_DIR or $ROOT/.state>/proxy/router.json` (the proxy's private state directory, created on demand): `{"target": "legacy"|"modern", "flipped_at": iso|null, "approved_by": str|null, "evidence": str|null}`, default legacy.
Admin (never forwarded): `GET /_router/status` → state + `"upstream_url"`; `POST /_router/flip` with header `X-Router-Token: <token>` (`ROUTER_ADMIN_TOKEN` env if set, else a random per-deployment token generated on first use in `ROUTER_TOKEN_FILE` or `<STRANGLER_STATE_DIR or .state>/router.token`, mode 600 — the same rule in the proxy and @cutover-gate, `targets/common/router_token.py`; there is no public default, and a token file that is not a regular non-symlinked file owned by the current user with no group/other access is refused) and body `{"target": "modern"|"legacy", "approved_by": str, "evidence": str}` → 200 new state; wrong/missing token → 403; bad target → 400. Upstream unreachable → 502 `{"error":"upstream_unavailable"}`.

## 7. Divergence engine (fuzzer core) — `agents/divergence.py`

`compare(legacy_resp, modern_resp) -> Diff | None` over: status, normalized content-type, JSON body deep diff (keys, value types, values — floats compared exactly; reports first N paths). Ignore `date`, `server`, `content-length`, `x-*` headers.
`classify(request, legacy, modern) -> quirk tag` using request/response *features* (no hard-coded IDs):
1. path ends `.json` and modern answers the exact **routing** 404 `{"detail": "Not Found"}` (no route matched) and legacy serves the path (≠ 404) or answers its *application* 404 `{"error": "not_found", …}` (e.g. an unknown user) → `json_suffix`. Any other modern 404 body means the route exists: a `not_found` payload falls to rule 3, a wrong body stays unclassified (never absorbed into D-1).
2. POST and legacy 500 and add/remove both empty → `empty_body_500`
3. legacy 200 body has `"error": "not_found"` and modern 404 → `missing_resource_200`
4. quote endpoint, quantity param parses as int ≤ 0, modern 422 → `non_positive_quantity`
5. GET and modern 422 and legacy 2xx/4xx(non-422) → `loose_query`
6. otherwise `unclassified:<method>:<route-template>:<legacy_status>:<modern_status>`
Cluster IDs (`D-n`) are assigned from a persistent registry (`<STRANGLER_STATE_DIR or $ROOT/.state>/fuzzer/divergence_registry.json`: tag → ID) in order of first appearance in the deterministic probe corpus, which is ordered D-1…D-5 as in §3, so the five known quirks get D-1..D-5 and anything new gets D-6+.

Decisions: `preserve` → modern must now match legacy exactly for that cluster's probes; `deprecate` → modern's status at discovery for that cluster's probes is the new contract, and the cluster is "approved deviation" iff every divergence of it still has that status AND no corpus probe of that quirk has silently converged back to legacy behaviour (that would be an inconsistent API → `deprecated-violated`).
"100% Approved Parity (k/k)" ⇔ every cluster ever seen is resolved (preserved+0 diffs, or deprecated+matching) **and** the randomized campaign produced 0 unresolved/unclassified diffs.

Campaign: (a) deterministic quirk probes (≥3 per quirk) + (b) ≥100 Hypothesis-generated GET requests fired concurrently (`asyncio.gather`, both targets per request) + (c) ≥10 POST probes executed sequentially with `/_admin/reset` on both targets before each. Hypothesis strategies stay inside the documented contract (add/remove are lists of strings; no malformed JSON).

## 8. Switch room protocol (agents)

See `docs/SWITCH_PROTOCOL.md` for exact API. Room name `strangler-migration`; members: agents `translator`, `fuzzer`, `cutover-gate` + Mattermost human `user`.
Commands (case-insensitive, mention-addressed):
- human → `@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI`
- translator → `@fuzzer shadow-test rev=<rev>`
- fuzzer → human: divergence table + `@user decide: reply "@translator preserve D-… and deprecate D-…"`
- human → `@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5` (**only human senders accepted** for decisions/approvals; agent-sent decisions are refused and logged)
- translator → `@fuzzer re-verify rev=<rev>`
- fuzzer → `✅ 100% Approved Parity (5/5)` + `@cutover-gate benchmark rev=<rev>`
- cutover-gate → p50/p95/p99 card + `@user reply "@cutover-gate approve-cutover"`
- human → `@cutover-gate approve-cutover` → proxy flip → `🚦 Cutover complete: :9000 → :9002`
Room document title: **`Parity & Cutover Audit Ledger`** (rendered Markdown + an embedded fenced ```` ```json stranglerroom-state ```` block holding machine-readable state, so any agent can rehydrate).
