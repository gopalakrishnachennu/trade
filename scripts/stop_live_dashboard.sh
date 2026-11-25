#!/usr/bin/env bash
# Stop the live dashboard and its Cloudflare tunnel. Does not touch the paper bot.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STOP_FILE="$ROOT/data/DASHBOARD_STOP"
mkdir -p "$ROOT/data"
touch "$STOP_FILE"

log() {
  ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '%s %s\n' "$ts" "$*" | tee -a "$ROOT/data/dashboard.log"
}

kill_pidfile() {
  local file="$1" name="$2"
  if [[ ! -f "$file" ]]; then
    return
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$file" || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "stopping ${name} pid=${pid}"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "${name} still running; sending KILL"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
}

log "stop_live_dashboard: writing data/DASHBOARD_STOP"
kill_pidfile "$ROOT/data/tunnel.pid" "public tunnel"
kill_pidfile "$ROOT/data/cloudflared.pid" "cloudflared"
kill_pidfile "$ROOT/data/dashboard.pid" "dashboard server"
kill_pidfile "$ROOT/data/dashboard_wrapper.pid" "dashboard wrapper"

# Catch any leftover listeners on the dashboard port
PORT="${DASHBOARD_PORT:-8787}"
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
fi
# Also stop stray cloudflared tunnels started from this project
pkill -f "$ROOT/bin/cloudflared tunnel" 2>/dev/null || true
pkill -f "nokey@localhost.run" 2>/dev/null || true
pkill -f "a.pinggy.io" 2>/dev/null || true
pkill -f "serveo.net" 2>/dev/null || true
pkill -f "uvicorn app:app --app-dir $ROOT/web" 2>/dev/null || true
pkill -f "$ROOT/scripts/run_live_dashboard.sh" 2>/dev/null || true
log "dashboard stop complete (paper bot is unchanged)"
