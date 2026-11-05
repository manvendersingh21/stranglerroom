#!/usr/bin/env bash
# Start/stop the three migration targets in the background:
#   :9001 legacy web.py Open Library, :9002 modern FastAPI (hot reload), :9000 strangler proxy
#
#   scripts/targets.sh start [--fresh]   # --fresh: stop ours, require free ports, remove the generated modern module
#                                        #          (MODERN_LISTS_PATH), reset the proxy to legacy (ROUTER_STATE_FILE or
#                                        #          $STATE/proxy/router.json) and the divergence registry
#                                        #          ($STATE/fuzzer/divergence_registry.json and its .staging copy);
#                                        #          when isolated, also the same files in the service area
#   scripts/targets.sh stop | status
#
# STATE is ${STRANGLER_STATE_DIR:-$ROOT/.state}, as on the Python side. PIDs/logs live in $STATE/run/<name>.{pid,log}.
# Only a pid read from our own pid file is ever signalled, and only after /proc/<pid>/cmdline proves it is still the
# expected `python -m <module>` (a stale pid file whose pid was reused must never make us signal an unrelated
# process). start/stop are serialised with flock.
#
# Isolation (docs/ISOLATION.md §4): `python -m agents.isolation mode` decides once whether each target runs as its own
# OS user. When it is on, `mode --run-dir` has already checked (creating it if need be) $RUN as a root-owned 0700
# directory on a path no service can modify, before this script touches any file in it. Each target is then launched
# through `agents.isolation exec`, which execs into the target itself, so the recorded pid stays the server's.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

# A path override the way the Python processes read it: relative ones against $ROOT (their working directory), and
# with "home", a leading ~ expanded (they expanduser STRANGLER_STATE_DIR and ROUTER_STATE_FILE, not MODERN_LISTS_PATH).
resolve_path() { # path [home]
  local p="$1"
  if [ "${2:-}" = home ]; then
    case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#"~/"}" ;; esac
  fi
  case "$p" in /*) ;; *) p="$ROOT/$p" ;; esac
  printf '%s\n' "$p"
}

STATE="$(resolve_path "${STRANGLER_STATE_DIR:-$ROOT/.state}" home)"
RUN="$STATE/run"
export PYTHONPATH="$ROOT"
ISOLATE=""
MODE_UNKNOWN=""
if ! mode="$(cd "$ROOT" && "$PY" -m agents.isolation mode --run-dir "$RUN")"; then mode=unknown; fi
case "$mode" in
  on) ISOLATE=1 ;;
  off) ;;
  *)
    case "${1:-start}" in
      stop|status)  # never leave components running because the mode is unknown (e.g. a broken interpreter)
        MODE_UNKNOWN=1
        echo "warning: cannot determine the isolation mode; ${1} acts on the tracked pids and on every sr-* user's" \
             "processes" >&2 ;;
      *) echo "error: cannot determine the isolation mode (see above); refusing to start" >&2; exit 2 ;;
    esac ;;
esac
# Isolated: pid files and logs are root's alone (a service could otherwise reopen its log via /proc/self/fd/1).
if [ -n "$ISOLATE" ]; then umask 077; fi
if [ -z "$MODE_UNKNOWN" ]; then mkdir -p "$RUN"; fi

# True only if <pid> is alive AND its argv holds the exact pairs `-m <module>` and `--port <port>` (a reused pid
# that happens to run the same module for another port — e.g. a test's modern target — is not ours either).
is_target_pid() { # pid module port
  local pid="$1" module="$2" port="$3" args=() i mod=0 prt=0
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1   # never 0 / negative: `kill 0` / `kill -1` mean process groups
  kill -0 "$pid" 2>/dev/null || return 1
  mapfile -d '' -t args 2>/dev/null <"/proc/$pid/cmdline" || return 1
  for ((i = 0; i + 1 < ${#args[@]}; i++)); do
    if [ "${args[i]}" = "-m" ] && [ "${args[i+1]}" = "$module" ]; then mod=1; fi
    if [ "${args[i]}" = "--port" ] && [ "${args[i+1]}" = "$port" ]; then prt=1; fi
  done
  [ "$mod" = 1 ] && [ "$prt" = 1 ]
}

# Isolated only: true if <pid> has finished the isolated exec. All four of its uids are sr-<name>'s, and it no longer
# runs the root launcher, whose argv (`-m agents.isolation exec <name> -- … -m <module> --port <port>`) would satisfy
# is_target_pid() too.
isolated_pid() { # pid name
  local pid="$1" want uids args=() i
  want="$(id -u "${STRANGLER_SERVICE_USER_PREFIX:-sr-}$2" 2>/dev/null)" || return 1
  uids="$(awk '/^Uid:/ {print $2, $3, $4, $5}' "/proc/$pid/status" 2>/dev/null)" || return 1
  [ "$uids" = "$want $want $want $want" ] || return 1
  mapfile -d '' -t args 2>/dev/null <"/proc/$pid/cmdline" || return 1
  for ((i = 0; i + 1 < ${#args[@]}; i++)); do
    if [ "${args[i]}" = "-m" ] && [ "${args[i+1]}" = "agents.isolation" ]; then return 1; fi
  done
}

# Isolated only: true if every TCP listener on <port> belongs to sr-<name>, so a health answer really came from the
# component (/proc/net/tcp{,6}; root here may lack CAP_SYS_PTRACE to map another uid's sockets to its pid).
isolated_listener() { # port name
  local want hex uids
  want="$(id -u "${STRANGLER_SERVICE_USER_PREFIX:-sr-}$2" 2>/dev/null)" || return 1
  hex="$(printf ':%04X' "$1")"
  uids="$(awk -v p="$hex" '$4 == "0A" && substr($2, length($2) - 4) == p {print $8}' /proc/net/tcp /proc/net/tcp6 \
          2>/dev/null | sort -u)"
  [ "$uids" = "$want" ]
}

# Kill every process of service user <user> (e.g. a daemon a compromised component forked), repeating until none is
# left (at most 50 rounds); report any survivor.
# The service users are ${STRANGLER_SERVICE_USER_PREFIX:-sr-}<name>; the variable is a test seam (tests set a prefix
# that names no real user, so not even an unstubbed pkill can reach a live deployment).
kill_service_user() { # user
  local _
  id -u "$1" >/dev/null 2>&1 || return 0
  for _ in $(seq 1 50); do
    pgrep -u "$1" >/dev/null 2>&1 || return 0
    pkill -KILL -u "$1" 2>/dev/null || true
    sleep 0.1
  done
  echo "warning: processes of $1 survive repeated KILL: $(pgrep -u "$1" | tr '\n' ' ')" >&2
}

# True if something accepts TCP connections on 127.0.0.1:<port>.
port_open() { # port
  (echo >"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

svc() { # name module port health
  local name="$1" module="$2" port="$3" health="$4" pid
  if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$port$health" 2>/dev/null; then
    if [ -n "${ISOLATE:-}" ]; then  # isolated: a healthy port counts only as our tracked, isolated process
      pid="$(cat "$RUN/$name.pid" 2>/dev/null || true)"
      if is_target_pid "$pid" "$module" "$port" && isolated_pid "$pid" "$name" && isolated_listener "$port" "$name"; then
        echo "  $name already up on :$port (pid $pid, sr-$name)"; return
      fi
      echo "error: :$port answers, but not as a tracked $name running as sr-$name — run 'scripts/targets.sh stop'" \
           "(or stop whatever holds :$port), then start again" >&2
      exit 1
    fi
    echo "  $name already up on :$port"; return
  fi
  # Background ONLY the fully redirected server command. A backgrounded `cd && nohup …` list runs in a
  # wrapper bash subshell that keeps this script's stdout/stderr open while it waits for the server, so a
  # caller capturing our output (run_e2e_demo.py's pre-flight) never sees EOF; $! would also name that
  # wrapper instead of the server. `9>&-`: the server must not inherit the flock descriptor. Isolated, the same
  # command runs through `agents.isolation exec`, which execs (the pid stays the server's); unset, the prefix is empty.
  # Log tails shown below come from the service: control characters are dropped before they reach a terminal.
  if [ -n "${ISOLATE:-}" ]; then : >"$RUN/$name.log"; chmod 0600 "$RUN/$name.log"; fi  # an older log may be 0644
  (
    cd "$ROOT"
    # isolated: the launcher must find isolation on too (a marker gone meanwhile is an error, never "off")
    if [ -n "${ISOLATE:-}" ]; then export STRANGLER_ISOLATION=on; fi
    nohup ${ISOLATE:+"$PY" -m agents.isolation exec "$name" --} "$PY" -m "$module" --port "$port" >"$RUN/$name.log" 2>&1 </dev/null 9>&- &
    echo $! >"$RUN/$name.pid"
  )
  pid="$(cat "$RUN/$name.pid")"
  for _ in $(seq 1 100); do  # <= 100 x (2 s curl + 0.2 s): bounded even if the target accepts but never answers
    if ! kill -0 "$pid" 2>/dev/null; then  # e.g. the port was taken meanwhile: never adopt someone else's server
      rm -f "$RUN/$name.pid"
      echo "  $name exited during start-up — see $RUN/$name.log" >&2
      tail -20 "$RUN/$name.log" | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177' >&2; exit 1
    fi
    if curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$port$health" 2>/dev/null; then
      # Isolated: the answer must come from our pid, exec'd into sr-<name>, owning the listener (not a squatter).
      if [ -n "${ISOLATE:-}" ] && ! { isolated_pid "$pid" "$name" && isolated_listener "$port" "$name"; }; then
        pkill -KILL -P "$pid" 2>/dev/null || true  # ours, but not running isolated: never leave it (or its
        kill -KILL "$pid" 2>/dev/null || true     # children, e.g. a reload worker) serving
        rm -f "$RUN/$name.pid"
        echo "  $name: :$port answers, but not from pid $pid running as sr-$name — see $RUN/$name.log" >&2; exit 1
      fi
      echo "  $name up on :$port"; return
    fi
    sleep 0.2
  done
  echo "  $name failed to start — see $RUN/$name.log" >&2
  tail -20 "$RUN/$name.log" | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177' >&2; exit 1
}

# Stop the target recorded in <name>.pid if it is still ours: TERM it (and uvicorn's reload worker), wait up to
# 5 s, then KILL. A stale pid file is reported and removed, never acted on.
stop_one() { # name module port
  local name="$1" module="$2" port="$3" pid
  pid="$(cat "$RUN/$name.pid" 2>/dev/null || true)"
  if is_target_pid "$pid" "$module" "$port"; then
    pkill -TERM -P "$pid" 2>/dev/null || true   # uvicorn --reload spawns a child worker
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    if kill -0 "$pid" 2>/dev/null; then
      echo "  $name (pid $pid) ignored TERM for 5 s — sending KILL" >&2
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill -KILL "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    fi
  elif [ -n "$pid" ]; then
    echo "  $name: stale pid file ($pid is not a running $module --port $port) — not signalling it" >&2
  fi
  # Isolated (or mode unknown): whatever else runs as sr-<name> goes too.
  if [ -n "${ISOLATE:-}${MODE_UNKNOWN:-}" ]; then kill_service_user "${STRANGLER_SERVICE_USER_PREFIX:-sr-}$name"; fi
  rm -f "$RUN/$name.pid"
}

# --fresh: remove what the processes actually use (overrides resolved the way they resolve them), and, isolated, the
# same files in the service area.
fresh_reset() {
  rm -f "$(resolve_path "${MODERN_LISTS_PATH:-$ROOT/targets/modern_fastapi/app/lists.py}")" \
        "$(resolve_path "${ROUTER_STATE_FILE:-$STATE/proxy/router.json}" home)" \
        "$STATE/fuzzer/divergence_registry.json" "$STATE/fuzzer/divergence_registry.json.staging"
  if [ -n "${ISOLATE:-}" ]; then (cd "$ROOT" && "$PY" -m agents.isolation reset); fi
}

# One status line per target. Isolated, an answering target is reported as isolated only if it passes the checks
# start applies (tracked pid, sr-<name>, owner of the listener).
target_status() { # name module port path
  local name="$1" module="$2" port="$3" path="$4" pid body
  printf '%-6s ' "$port"
  if ! body="$(curl -fsS -m 5 "http://127.0.0.1:$port$path" 2>/dev/null)"; then echo down; return 0; fi
  if [ -z "${ISOLATE:-}" ]; then printf '%s\n' "$body"; return 0; fi
  pid="$(cat "$RUN/$name.pid" 2>/dev/null || true)"
  if is_target_pid "$pid" "$module" "$port" && isolated_pid "$pid" "$name" && isolated_listener "$port" "$name"; then
    printf '%s  [%s running (isolated), pid %s]\n' "$body" "$name" "$pid"
  else
    printf '%s  [%s running, NOT isolated — run restart: scripts/targets.sh stop; scripts/targets.sh start]\n' \
      "$body" "$name"
  fi
}

stop() {
  stop_one proxy targets.proxy.router 9000
  stop_one modern targets.modern_fastapi.server 9002
  stop_one legacy targets.legacy_openlibrary.server 9001
}

# Fail (non-zero) naming every port that still accepts connections.
ports_free() { # port...
  local port busy=()
  for port in "$@"; do
    if port_open "$port"; then busy+=("$port"); fi
  done
  if [ "${#busy[@]}" -gt 0 ]; then
    echo "error: port(s) ${busy[*]} still in use by a process targets.sh does not track (pid file missing or stale)" \
         "— stop it, then retry; --fresh never reuses a running target" >&2
    return 1
  fi
}

lock() {
  if [ -n "$MODE_UNKNOWN" ]; then  # the run dir was not validated: never create or open anything in it
    echo "warning: not taking $RUN/.targets.lock (isolation mode unknown)" >&2
    return 0
  fi
  exec 9>"$RUN/.targets.lock"
  flock -w 60 9 || { echo "error: $RUN/.targets.lock is held by another targets.sh start/stop for 60 s — giving up" >&2; exit 1; }
}

case "${1:-start}" in
  start)
    lock
    if [ "${2:-}" = "--fresh" ]; then
      stop
      ports_free 9000 9001 9002
      fresh_reset
    fi
    echo "starting targets"
    svc legacy targets.legacy_openlibrary.server 9001 /health
    svc modern targets.modern_fastapi.server 9002 /health
    svc proxy targets.proxy.router 9000 /_router/status
    ;;
  stop) lock; stop; echo "targets stopped" ;;  # mode unknown: stop_one also kills every sr-<target> process
  status)
    target_status legacy targets.legacy_openlibrary.server 9001 /health
    target_status modern targets.modern_fastapi.server 9002 /_meta
    target_status proxy targets.proxy.router 9000 /_router/status ;;
  *) echo "usage: $0 start [--fresh] | stop | status" >&2; exit 2 ;;
esac
