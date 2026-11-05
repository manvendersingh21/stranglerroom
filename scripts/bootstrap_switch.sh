#!/usr/bin/env bash
# Bootstrap a self-hosted Switch 0.31.0 stack (switch-core + Mattermost + Gateway + Postgres) and wait
# until it is healthy and the Mattermost seeder has exited 0.
#
#   scripts/bootstrap_switch.sh            # auto: docker compose if a Docker daemon is reachable, else native
#   SWITCH_MODE=docker scripts/bootstrap_switch.sh
#   SWITCH_MODE=native scripts/bootstrap_switch.sh
#   scripts/bootstrap_switch.sh stop       # stop native processes (or `docker compose down`)
#
# Docker mode is exactly the upstream recipe: clone sandbox-quantum/switch into .switch-infra, copy
# .env.example -> .env, generate secrets, pin SWITCH_VERSION=0.31.0, then
#   docker compose -f deploy/local/standalone-docker-compose.yml --project-directory . \
#     --profile collab --profile gateway up -d
# Native mode runs the same components from the switch-v0.31.0 source tag for hosts without a Docker
# daemon (e.g. unprivileged Kubernetes pods): Postgres 16, Mattermost 11.9.0 tarball, switch-core via uv
# (serving the built Gateway UI), and the same deploy/shared_resources/setup.py seeder.
#
# Writes $ROOT/.state/switch.env with SWITCH_URL / MATTERMOST_URL / creds for the agents and demo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/.switch-infra"
NATIVE="$ROOT/.native"
STATE="${STRANGLER_STATE_DIR:-$ROOT/.state}"  # the same state directory the Python side resolves
SWITCH_TAG="switch-v0.31.0"
SWITCH_VERSION="0.31.0"
MM_VERSION="11.9.0"
MM_PORT="${MATTERMOST_HOST_PORT:-18065}"
PGDATA_DIR="${PGDATA_DIR:-/var/lib/stranglerroom/pgdata}"
mkdir -p "$STATE"

log() { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[bootstrap] %s\033[0m\n' "$*" >&2; exit 1; }

port_busy() { (echo >"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_http() { # url name timeout
  local url="$1" name="$2" t="${3:-180}" i=0
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    i=$((i + 1)); [ "$i" -ge "$t" ] && die "$name did not become healthy at $url within ${t}s"
    sleep 1
  done
  log "$name healthy ($url)"
}

clone_infra() {
  if [ ! -d "$INFRA/.git" ]; then
    log "cloning sandbox-quantum/switch -> .switch-infra"
    git clone -q https://github.com/sandbox-quantum/switch.git "$INFRA"
  fi
  git -C "$INFRA" fetch -q --tags || true
}

gen_secrets() { # env-file label
  local file="$1" label="$2" key
  if [ -f "$INFRA/deploy/local/generate-env-secrets.sh" ]; then
    bash "$INFRA/deploy/local/generate-env-secrets.sh" "$file" "$label"; return
  fi
  # switch-v0.31.0 predates that script; this is the same key list as its `just init-env` recipe
  # (and as the later upstream script), generated locally so CI never depends on upstream `main`.
  command -v openssl >/dev/null 2>&1 || die "openssl is required to generate secrets"
  local keys=(DB_PASSWORD DB_OWNER_PASSWORD AGENT_REGISTRATION_TOKEN GATEWAY_ADMIN_PASSWORD
              MATTERMOST_ADMIN_PASSWORD MATTERMOST_USER_PASSWORD SECRET_KEYS) line tmp
  for key in "${keys[@]}"; do grep -q "^${key}=" "$file" || die "$file has no ${key}= line to fill"; done
  # Rewrite the file line by line: a generated value only ever travels through a pipe and builtins, never through
  # a command line (/proc/<pid>/cmdline is world-readable), and the new file is 0600 from creation (mktemp).
  tmp="$(mktemp "$file.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    case "${line%%=*}" in
      SECRET_KEYS) printf 'SECRET_KEYS=%s:%s\n' "$label" "$(openssl rand -hex 32)" ;;
      DB_PASSWORD|DB_OWNER_PASSWORD|AGENT_REGISTRATION_TOKEN|GATEWAY_ADMIN_PASSWORD|MATTERMOST_ADMIN_PASSWORD|MATTERMOST_USER_PASSWORD)
        printf '%s=%s\n' "${line%%=*}" "$(openssl rand -hex 24)" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done <"$file" >"$tmp"
  mv -f "$tmp" "$file"
}

prepare_env() { # mode
  local mode="$1"
  if [ ! -f "$INFRA/.env" ]; then
    (umask 077; cp "$INFRA/.env.example" "$INFRA/.env")  # 0600 before any secret is written into it
    gen_secrets "$INFRA/.env" local                     # sed -i keeps the file's mode
    log "generated .env with fresh secrets"
  fi
  chmod 600 "$INFRA/.env"  # also tightens a .env from an older bootstrap: it holds the DB and admin secrets
  sed -i "s|^SWITCH_VERSION=.*|SWITCH_VERSION=${SWITCH_VERSION}|" "$INFRA/.env"
  sed -i "s|^MATTERMOST_HOST_PORT=.*|MATTERMOST_HOST_PORT=${MM_PORT}|" "$INFRA/.env"
  if [ "$mode" = docker ]; then
    API_PORT="${API_HOST_PORT:-8000}"
  else
    API_PORT="${API_HOST_PORT:-18000}"
    port_busy "$API_PORT" && ! curl -fsS "http://127.0.0.1:$API_PORT/health" 2>/dev/null | grep -q '"ok"' \
      && die "port $API_PORT is taken by something that is not Switch; set API_HOST_PORT"
  fi
  sed -i "s|^API_HOST_PORT=.*|API_HOST_PORT=${API_PORT}|" "$INFRA/.env"
}

write_state_env() {
  set -a; . "$INFRA/.env"; set +a
  mkdir -p "$STATE"
  touch "$STATE/switch.env" && chmod 600 "$STATE/switch.env"  # 0600 before the secrets are written
  cat >"$STATE/switch.env" <<EOF
SWITCH_URL=http://localhost:${API_PORT}
MATTERMOST_URL=http://localhost:${MM_PORT}
AGENT_REGISTRATION_TOKEN=${AGENT_REGISTRATION_TOKEN}
GATEWAY_ADMIN_EMAIL=${GATEWAY_ADMIN_EMAIL}
GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD}
MATTERMOST_TEAM_NAME=${MATTERMOST_TEAM_NAME}
MATTERMOST_USER=${MATTERMOST_USER}
MATTERMOST_USER_PASSWORD=${MATTERMOST_USER_PASSWORD}
MATTERMOST_ADMIN_USER=${MATTERMOST_ADMIN_USER}
MATTERMOST_ADMIN_PASSWORD=${MATTERMOST_ADMIN_PASSWORD}
EOF
  chmod 600 "$STATE/switch.env"
  # The public part (no secret): all an agent process needs. Agents read this file, never switch.env.
  printf 'SWITCH_URL=http://localhost:%s\nMATTERMOST_URL=http://localhost:%s\n' "$API_PORT" "$MM_PORT" \
    >"$STATE/connection.env"
  chmod 644 "$STATE/connection.env"
  log "wrote .state/switch.env and .state/connection.env"
}

# ---------------------------------------------------------------- docker mode
docker_up() {
  clone_infra
  git -C "$INFRA" checkout -q "$SWITCH_TAG"
  prepare_env docker
  (cd "$INFRA" && docker compose -f deploy/local/standalone-docker-compose.yml --project-directory . \
      --profile collab --profile gateway up -d)
  wait_http "http://localhost:${API_PORT}/health" switch-core 240
  wait_http "http://localhost:${MM_PORT}/api/v4/system/ping" mattermost 240
  log "waiting for the setup container to exit 0"
  local cid="" status="" code="" i=0
  while :; do
    cid="$(cd "$INFRA" && docker compose -f deploy/local/standalone-docker-compose.yml --project-directory . \
            --profile collab --profile gateway ps -a -q setup)"
    if [ -n "$cid" ]; then
      status="$(docker inspect -f '{{.State.Status}}' "$cid")"; code="$(docker inspect -f '{{.State.ExitCode}}' "$cid")"
      [ "$status" = exited ] && [ "$code" = 0 ] && break
    fi
    i=$((i + 1)); [ "$i" -ge 300 ] && die "setup container did not exit 0 (status=$status code=$code)"
    sleep 1
  done
  log "setup exited 0"
  write_state_env
}

docker_down() { (cd "$INFRA" && docker compose -f deploy/local/standalone-docker-compose.yml --project-directory . --profile collab --profile gateway down); }

# ---------------------------------------------------------------- native mode
native_install() {
  export PATH="$HOME/.local/bin:$PATH"
  if [ ! -x /usr/lib/postgresql/16/bin/postgres ]; then
    log "installing postgresql-16"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-16 postgresql-client-16 >/dev/null
  fi
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  mkdir -p "$NATIVE"
  if [ ! -x "$NATIVE/mattermost/bin/mattermost" ]; then
    log "downloading Mattermost ${MM_VERSION}"
    curl -fsSL -o "$NATIVE/mattermost.tar.gz" "https://releases.mattermost.com/${MM_VERSION}/mattermost-team-${MM_VERSION}-linux-amd64.tar.gz"
    tar -C "$NATIVE" -xzf "$NATIVE/mattermost.tar.gz"
  fi
  if [ ! -x "$INFRA/core/.venv/bin/python" ]; then
    log "uv sync switch-core ${SWITCH_VERSION}"
    (cd "$INFRA/core" && uv sync --frozen --no-dev -q)
  fi
  cp "$INFRA/console/packages/shared/src/session-v1/examples.activity.json" \
     "$INFRA/core/switch_core/bridges/collaboration/session/" 2>/dev/null || true
  if [ ! -f "$INFRA/gateway/dist/index.html" ]; then
    log "building Gateway UI"
    (cd "$INFRA/gateway" && npm ci --silent && npm run build >/dev/null)
  fi
}

native_postgres() {
  set -a; . "$INFRA/.env"; set +a
  local PGB=/usr/lib/postgresql/16/bin
  if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
    log "initdb $PGDATA_DIR"
    mkdir -p "$PGDATA_DIR"; chown -R postgres:postgres "$(dirname "$PGDATA_DIR")"
    # The owner password goes to initdb in a file that is 0600 and owned by postgres from the start, inside a
    # root-created private directory (0711: postgres can reach its file, nobody can list or replace anything).
    local pwdir pw; pwdir="$(mktemp -d)"; pw="$pwdir/pw"
    (umask 077; printf '%s\n' "$DB_OWNER_PASSWORD" >"$pw"); chown postgres "$pw"; chmod 0711 "$pwdir"
    if ! su postgres -c "$PGB/initdb -D $PGDATA_DIR -U $DB_OWNER_USER --pwfile=$pw -A scram-sha-256 >/dev/null"; then
      rm -rf "$pwdir"; die "initdb failed"
    fi
    rm -rf "$pwdir"
  fi
  if ! su postgres -c "$PGB/pg_ctl -D $PGDATA_DIR status" >/dev/null 2>&1; then
    su postgres -c "$PGB/pg_ctl -D $PGDATA_DIR -l $(dirname "$PGDATA_DIR")/pg.log -o '-p ${POSTGRES_HOST_PORT:-5432} -c listen_addresses=127.0.0.1 -c max_connections=300 -k /tmp' -w start" >/dev/null
  fi
  export PGPASSWORD="$DB_OWNER_PASSWORD"
  local q="psql -h 127.0.0.1 -p ${POSTGRES_HOST_PORT:-5432} -U $DB_OWNER_USER -d postgres -tAc"
  [ "$($q "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")" = 1 ] || $q "CREATE DATABASE $DB_NAME" >/dev/null
  [ "$($q "SELECT 1 FROM pg_database WHERE datname='mattermost'")" = 1 ] || $q "CREATE DATABASE mattermost" >/dev/null
  # Statements carrying a password go to psql on stdin (printf is a builtin), never in its argv.
  local sql="psql -h 127.0.0.1 -p ${POSTGRES_HOST_PORT:-5432} -U $DB_OWNER_USER -d postgres -qtA -v ON_ERROR_STOP=1 -f -"
  [ "$($q "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'")" = 1 ] || \
    printf "CREATE ROLE \"%s\" LOGIN PASSWORD '%s' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;\n" \
      "$DB_USER" "$DB_PASSWORD" | $sql >/dev/null
  printf "ALTER ROLE \"%s\" PASSWORD '%s';\n" "$DB_USER" "$DB_PASSWORD" | $sql >/dev/null
  log "postgres ready"
}

native_mattermost() {
  set -a; . "$INFRA/.env"; set +a
  if curl -fsS "http://localhost:${MM_PORT}/api/v4/system/ping" >/dev/null 2>&1; then log "mattermost already running"; return; fi
  mkdir -p "$NATIVE/mmdata" "$NATIVE/mmlogs"
  (
    export MM_SQLSETTINGS_DRIVERNAME=postgres
    export MM_SQLSETTINGS_DATASOURCE="postgres://${DB_OWNER_USER}:${DB_OWNER_PASSWORD}@127.0.0.1:${POSTGRES_HOST_PORT:-5432}/mattermost?sslmode=disable"
    export MM_SERVICESETTINGS_SITEURL="http://localhost:${MM_PORT}" MM_SERVICESETTINGS_LISTENADDRESS=":${MM_PORT}"
    export MM_SERVICESETTINGS_ENABLELOCALMODE=true MM_SERVICESETTINGS_LOCALMODESOCKETLOCATION=/var/tmp/mattermost_local.socket
    export MM_SERVICESETTINGS_ENABLEONBOARDINGFLOW=false MM_DISPLAYSETTINGS_CUSTOMURLSCHEMES=switchdash
    export MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION=true MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS=true
    export MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS="localhost 127.0.0.1"
    export MM_TEAMSETTINGS_ENABLEOPENSERVER=true MM_TEAMSETTINGS_TEAMMATENAMEDISPLAY=full_name
    export MM_LOGSETTINGS_ENABLEDIAGNOSTICS=false MM_LOGSETTINGS_ENABLESENTRY=false MM_SERVICESETTINGS_ENABLESECURITYFIXALERT=false
    export MM_ANNOUNCEMENTSETTINGS_ADMINNOTICESENABLED=false MM_ANNOUNCEMENTSETTINGS_USERNOTICESENABLED=false
    export MM_PLUGINSETTINGS_ENABLEREMOTEMARKETPLACE=false MM_PLUGINSETTINGS_ENABLE=false
    export MM_FILESETTINGS_DIRECTORY="$NATIVE/mmdata/" MM_LOGSETTINGS_FILELOCATION="$NATIVE/mmlogs"
    export MM_RATELIMITSETTINGS_ENABLE=false
    cd "$NATIVE/mattermost" || exit 1
    # `exec` makes the background job the server itself, so $! (the recorded pid) is what `stop` signals.
    exec nohup ./bin/mattermost >"$NATIVE/mm.out" 2>&1 </dev/null &
    echo $! >"$NATIVE/mattermost.pid"
  )
  wait_http "http://localhost:${MM_PORT}/api/v4/system/ping" mattermost 180
}

native_switch_core() {
  if curl -fsS "http://localhost:${API_PORT}/health" 2>/dev/null | grep -q '"ok"'; then log "switch-core already running"; return; fi
  (
    set -a; . "$INFRA/.env"; set +a
    export DB_HOST=127.0.0.1 DB_PORT="${POSTGRES_HOST_PORT:-5432}" ID_SERVER_NAME=localhost MATRIX_SERVER_NAME=localhost
    export SERVER_HOST=127.0.0.1 SERVER_PORT="$API_PORT" COLLABORATION_CALLBACK_HOST=127.0.0.1 COLLABORATION_CALLBACK_PORT="${CALLBACK_PORT:-18081}"
    export GATEWAY_COOKIE_SECURE=false OUTBOUND_ALLOWED_PRIVATE_HOSTS=localhost,127.0.0.1
    export GATEWAY_UI_DIR="$INFRA/gateway/dist" FRONTEND_BASE_URL="http://localhost:${API_PORT}"
    export TELEMETRY_ENABLED=false THIRD_PARTY_AVATARS_ENABLED=false
    unset SWITCH_REGISTRY SWITCH_IMAGE_NAMESPACE GATEWAY_HOST_PORT API_HOST_PORT MATTERMOST_HOST_PORT POSTGRES_HOST_PORT SWITCH_BIND_ADDR FEATURE_FLAGS_ENABLED JWT_SECRET_KEY CONTROLLER_TOKEN_SECRET
    cd "$INFRA/core" || exit 1
    exec nohup .venv/bin/python -m switch_core.main >"$NATIVE/core.out" 2>&1 </dev/null &
    echo $! >"$NATIVE/switch-core.pid"
  )
  wait_http "http://localhost:${API_PORT}/health" switch-core 180
}

native_setup() {
  (
    set -a; . "$INFRA/.env"; set +a
    export SWITCH_URL="http://localhost:${API_PORT}" MATTERMOST_URL="http://localhost:${MM_PORT}"
    export MATTERMOST_URL_FOR_SWITCH="http://localhost:${MM_PORT}" MATTERMOST_CALLBACK_BASE_URL="http://127.0.0.1:${CALLBACK_PORT:-18081}"
    local i=0
    until "$INFRA/core/.venv/bin/python" "$INFRA/deploy/shared_resources/setup.py" >"$NATIVE/setup.out" 2>&1; do
      i=$((i + 1)); [ "$i" -ge 5 ] && { tail -20 "$NATIVE/setup.out"; exit 1; }
      sleep 3   # same restart: on-failure semantics as the compose service
    done
  ) || die "setup seeder failed (see .native/setup.out)"
  log "setup exited 0"
}

native_up() {
  clone_infra
  git -C "$INFRA" checkout -q "$SWITCH_TAG"
  prepare_env native
  native_install
  native_postgres
  native_mattermost
  native_switch_core
  native_setup
  write_state_env
}

native_server_pid() { # name pid -> exit 0 iff pid is that server of THIS installation (exact identity)
  local name="$1" pid="$2"
  [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
  case "$name" in
    mattermost) [ "$(readlink -f "/proc/$pid/exe" 2>/dev/null)" = "$(readlink -f "$NATIVE/mattermost/bin/mattermost")" ] ;;
    switch-core) [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" = "$(readlink -f "$INFRA/core")" ] \
                   && tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null | grep -qxF switch_core.main ;;
    *) return 1 ;;
  esac
}

native_servers_alive() { # any switch-core / Mattermost of this installation still running?
  local pid
  for pid in $(pgrep -f 'switch_core\.main|bin/mattermost' 2>/dev/null || true); do
    native_server_pid switch-core "$pid" && return 0
    native_server_pid mattermost "$pid" && return 0
  done
  return 1
}

native_down() { # stop switch-core and Mattermost (identity-verified, TERM then KILL), then Postgres
  local p pid i
  for p in switch-core mattermost; do
    pid="$(cat "$NATIVE/$p.pid" 2>/dev/null || true)"
    if ! native_server_pid "$p" "$pid"; then
      log "$p: no live server recorded in .native/$p.pid — not started by this script (left alone)"
      rm -f "$NATIVE/$p.pid"
      continue
    fi
    kill "$pid" 2>/dev/null || true
    for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && { kill -9 "$pid" 2>/dev/null || true; sleep 0.5; }
    kill -0 "$pid" 2>/dev/null && die "$p (pid $pid) did not exit"
    rm -f "$NATIVE/$p.pid"
    log "$p stopped"
  done
  # Never pull the database from under a server of this installation that is still running (e.g. untracked).
  native_servers_alive && die "a switch-core or Mattermost of this installation is still running without a pid file; stop it first (Postgres left up)"
  su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $PGDATA_DIR stop" >/dev/null 2>&1 || true
  log "postgres stopped"
}

mode="${SWITCH_MODE:-auto}"
if [ "$mode" = auto ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then mode=docker; else mode=native; fi
fi
case "${1:-up}" in
  up) log "mode=$mode"; "${mode}_up"; log "Switch ${SWITCH_VERSION} ready — API $(grep SWITCH_URL "$STATE/switch.env" | cut -d= -f2-), Mattermost http://localhost:${MM_PORT}" ;;
  stop|down) "${mode}_down" ;;
  *) die "usage: $0 [up|stop]" ;;
esac
