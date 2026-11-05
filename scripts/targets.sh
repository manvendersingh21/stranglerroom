#!/usr/bin/env bash
# Start/stop the three migration targets in the background:
#   :9001 legacy web.py Open Library, :9002 modern FastAPI (hot reload), :9000 strangler proxy
#
#   scripts/targets.sh start [--fresh]   # --fresh: stop ours, require free ports, remove generated modern lists.py,
#                                        #          reset the proxy to legacy and the divergence registry
#   scripts/targets.sh stop | status
#
# PIDs/logs live in .state/run/<name>.{pid,log}. Only a pid read from our own pid file is ever signalled, and only
# after /proc/<pid>/cmdline proves it is still the expected `python -m <module>` (a stale pid file whose pid was
# reused must never make us signal an unrelated process). start/stop are serialised with flock.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
RUN="$ROOT/.state/run"
mkdir -p "$RUN"
export PYTHONPATH="$ROOT"

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

# True if something accepts TCP connections on 127.0.0.1:<port>.
port_open() { # port
  (echo >"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

svc() { # name module port health
  local name="$1" module="$2" port="$3" health="$4" pid
  if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$port$health" 2>/dev/null; then echo "  $name already up on :$port"; return; fi
  # Background ONLY the fully redirected server command. A backgrounded `cd && nohup …` list runs in a
  # wrapper bash subshell that keeps this script's stdout/stderr open while it waits for the server, so a
  # caller capturing our output (run_e2e_demo.py's pre-flight) never sees EOF; $! would also name that
  # wrapper instead of the server. `9>&-`: the server must not inherit the flock descriptor.
  (
    cd "$ROOT"
    nohup "$PY" -m "$module" --port "$port" >"$RUN/$name.log" 2>&1 </dev/null 9>&- &
    echo $! >"$RUN/$name.pid"
  )
  pid="$(cat "$RUN/$name.pid")"
  for _ in $(seq 1 100); do  # <= 100 x (2 s curl + 0.2 s): bounded even if the target accepts but never answers
    if ! kill -0 "$pid" 2>/dev/null; then  # e.g. the port was taken meanwhile: never adopt someone else's server
      rm -f "$RUN/$name.pid"
      echo "  $name exited during start-up — see $RUN/$name.log" >&2; tail -20 "$RUN/$name.log" >&2; exit 1
    fi
    curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$port$health" 2>/dev/null && { echo "  $name up on :$port"; return; }
    sleep 0.2
  done
  echo "  $name failed to start — see $RUN/$name.log" >&2; tail -20 "$RUN/$name.log" >&2; exit 1
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
  rm -f "$RUN/$name.pid"
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
  exec 9>"$RUN/.targets.lock"
  flock -w 60 9 || { echo "error: $RUN/.targets.lock is held by another targets.sh start/stop for 60 s — giving up" >&2; exit 1; }
}

case "${1:-start}" in
  start)
    lock
    if [ "${2:-}" = "--fresh" ]; then
      stop
      ports_free 9000 9001 9002
      rm -f "$ROOT/targets/modern_fastapi/app/lists.py" "$ROOT/.state/router.json" "$ROOT/.state/divergence_registry.json"
    fi
    echo "starting targets"
    svc legacy targets.legacy_openlibrary.server 9001 /health
    svc modern targets.modern_fastapi.server 9002 /health
    svc proxy targets.proxy.router 9000 /_router/status
    ;;
  stop) lock; stop; echo "targets stopped" ;;
  status)
    for p in 9001:/health 9002:/_meta 9000:/_router/status; do
      printf '%-6s ' "${p%%:*}"; curl -fsS -m 5 "http://127.0.0.1:${p%%:*}${p#*:}" 2>/dev/null || printf 'down'; echo
    done ;;
  *) echo "usage: $0 start [--fresh] | stop | status" >&2; exit 2 ;;
esac
