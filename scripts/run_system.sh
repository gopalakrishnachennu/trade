#!/usr/bin/env bash
# Detached fail-closed supervisor (paper only). Does not start the dashboard tunnel.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data/runtime
export PYTHONPATH="$ROOT/src"
export PYTHONUNBUFFERED=1
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
LOG="$ROOT/data/supervisor.log"
STOP_FILE="$ROOT/data/SYSTEM_STOP"

echo $$ > "$ROOT/data/supervisor_wrapper.pid"

log() {
  ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '%s %s\n' "$ts" "$*" | tee -a "$LOG"
}

CHILD=""
shutdown() {
  log "wrapper received stop"
  if [[ -n "$CHILD" ]] && kill -0 "$CHILD" 2>/dev/null; then
    kill -TERM "$CHILD" 2>/dev/null || true
    wait "$CHILD" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT

log "system wrapper start pid=$$ (paper only, fail-closed)"
while true; do
  if [[ -f "$STOP_FILE" ]]; then
    log "data/SYSTEM_STOP present — exiting"
    exit 0
  fi
  "$PYTHON" -m polymarket_bot.runtime.supervisor --config "$ROOT/config.yaml" >> "$LOG" 2>&1 &
  CHILD=$!
  echo "$CHILD" > "$ROOT/data/supervisor.pid"
  log "supervisor pid=$CHILD"
  wait "$CHILD"
  code=$?
  CHILD=""
  if [[ -f "$STOP_FILE" ]]; then
    log "supervisor exited $code; STOP present"
    exit 0
  fi
  # If we are halted, do not tight-loop a trading resume — supervisor itself
  # stays up when halted, so an exit here is unexpected. Restart it; it will
  # see HALT.json and will not start trading processes.
  log "supervisor exited $code — restarting in 3s (latch, if set, stays set)"
  sleep 3
done
