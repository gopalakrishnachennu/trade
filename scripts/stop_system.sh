#!/usr/bin/env bash
# Stop trading supervisor and workers. Does not stop the dashboard or tunnel.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data"
touch "$ROOT/data/SYSTEM_STOP"
log() { ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"; printf '%s %s\n' "$ts" "$*"; }

kill_pidfile() {
  local f="$1" n="$2"
  [[ -f "$f" ]] || return
  local pid; pid="$(tr -d '[:space:]' < "$f" || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "stopping $n pid=$pid"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  fi
}

kill_pidfile "$ROOT/data/supervisor.pid" "supervisor"
kill_pidfile "$ROOT/data/supervisor_wrapper.pid" "supervisor wrapper"
# workers
for role in discovery index_feed book_feed engine execution; do
  kill_pidfile "$ROOT/data/runtime/${role}.pid" "$role"
done
# also the old monolith bot, if any, but never the dashboard wrapper
kill_pidfile "$ROOT/data/bot.pid" "legacy bot"
pkill -f "polymarket_bot.runtime.supervisor" 2>/dev/null || true
pkill -f "polymarket_bot.runtime.workers" 2>/dev/null || true
log "trading system stopped (dashboard/tunnel left running)"
