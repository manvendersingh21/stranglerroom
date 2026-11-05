#!/usr/bin/env bash
# Manage the three StranglerRoom Switch agents.
#
#   scripts/agents.sh start|stop|restart|status
#
# Each agent runs as `$PYTHON -m <prefix><module>` in the background, with
# PYTHONPATH set to the repository root and its PID/log files stored under
# $STRANGLER_RUN_DIR (default: $ROOT/.state/run).  Only PIDs read from our own
# PID files are ever signalled, and only after verifying both that the process
# is alive and that its /proc/<pid>/cmdline still names the expected module.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
RUN="${STRANGLER_RUN_DIR:-$ROOT/.state/run}"
PREFIX="${AGENTS_MODULE_PREFIX:-agents.}"

# Keep paths independent of the caller's current working directory.
case "$RUN" in
  /*) ;;
  *) RUN="$ROOT/$RUN" ;;
esac

AGENT_NAMES=(translator fuzzer cutover-gate)

# Map the public agent name to the Python module that implements it.
module_for() {
  case "$1" in
    translator)   printf '%s\n' "${PREFIX}translator_agent" ;;
    fuzzer)       printf '%s\n' "${PREFIX}fuzzer_agent" ;;
    cutover-gate) printf '%s\n' "${PREFIX}cutover_agent" ;;
  esac
}

pid_file() { printf '%s/%s.pid\n' "$RUN" "$1"; }
log_file() { printf '%s/%s.log\n' "$RUN" "$1"; }

# Echo the PID stored in <name>.pid, but only if it is a plain positive number.
# Anything else (missing file, empty file, garbage) is treated as stale.
read_pid() {
  local file pid
  file="$(pid_file "$1")"
  [ -f "$file" ] || return 0
  pid="$(cat "$file" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 0 ;;
  esac
  printf '%s\n' "$pid"
}

# True only if <pid> is alive AND its argv contains the exact pair `-m <module>`.
# This guards against PID reuse and against PID files for unrelated processes
# (a module name merely appearing somewhere in another process's args is not enough).
is_agent_pid() {
  local pid="$1" module="$2" args=() i
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [ -r "/proc/$pid/cmdline" ] || return 1
  mapfile -d '' -t args <"/proc/$pid/cmdline" 2>/dev/null || return 1
  for ((i = 0; i + 1 < ${#args[@]}; i++)); do
    if [ "${args[i]}" = "-m" ] && [ "${args[i+1]}" = "$module" ]; then return 0; fi
  done
  return 1
}

# Echo the live PID for <name> if (and only if) it belongs to our agent module.
agent_pid() {
  local name="$1" module pid
  module="$(module_for "$name")"
  pid="$(read_pid "$name")"
  if is_agent_pid "$pid" "$module"; then
    printf '%s\n' "$pid"
  fi
}

# Start one agent unless it is already running.  Stale PID files are ignored
# and replaced.  The log is appended to, beginning with a timestamped header.
start_one() {
  local name="$1" module pid log
  module="$(module_for "$name")"
  log="$(log_file "$name")"

  pid="$(agent_pid "$name")"
  if [ -n "$pid" ]; then
    echo "$name already running pid=$pid"
    return 0
  fi

  # Stale (dead or foreign) PID file: drop it and launch fresh.
  rm -f "$(pid_file "$name")"
  mkdir -p "$RUN"
  printf '=== %s start %s (%s) ===\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$name" "$module" >>"$log"

  (
    cd "$ROOT"
    export PYTHONPATH="$ROOT"
    export PYTHONUNBUFFERED=1
    nohup "$PYTHON" -m "$module" >>"$log" 2>&1 </dev/null 9>&- &
    printf '%s\n' "$!" >"$(pid_file "$name")"
  )

  pid="$(read_pid "$name")"
  sleep 0.5
  if ! is_agent_pid "$pid" "$module"; then
    echo "error: $name failed to start; see $log" >&2
    exit 1
  fi
  echo "$name started pid=$pid"
}

# Stop all agents: TERM every live one, wait up to 5 s in total, KILL survivors,
# then remove every PID file. Idempotent; never signals a pid that is not ours.
stop_all() {
  local name pid module deadline survivors=()
  for name in "${AGENT_NAMES[@]}"; do
    pid="$(agent_pid "$name")"
    [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
  done
  deadline=$((SECONDS + 5))
  while [ "$SECONDS" -lt "$deadline" ]; do
    survivors=()
    for name in "${AGENT_NAMES[@]}"; do
      [ -n "$(agent_pid "$name")" ] && survivors+=("$name")
    done
    [ "${#survivors[@]}" -eq 0 ] && break
    sleep 0.1
  done
  for name in "${AGENT_NAMES[@]}"; do
    pid="$(agent_pid "$name")"
    [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null || true
    rm -f "$(pid_file "$name")"
  done
}

status_one() {
  local name="$1" pid
  pid="$(agent_pid "$name")"
  if [ -n "$pid" ]; then
    echo "$name running pid=$pid"
  else
    echo "$name stopped"
  fi
}

# Serialise start/stop/restart so concurrent invocations cannot spawn untracked duplicates.
lock() {
  mkdir -p "$RUN"
  exec 9>"$RUN/.lock"
  flock 9
}

case "${1:-}" in
  start)
    lock
    for name in "${AGENT_NAMES[@]}"; do
      start_one "$name"
    done
    ;;
  stop)
    lock
    stop_all
    ;;
  restart)
    lock
    stop_all
    for name in "${AGENT_NAMES[@]}"; do
      start_one "$name"
    done
    ;;
  status)
    for name in "${AGENT_NAMES[@]}"; do
      status_one "$name"
    done
    ;;
  *)
    echo 'usage: agents.sh start|stop|restart|status' >&2
    exit 2
    ;;
esac
