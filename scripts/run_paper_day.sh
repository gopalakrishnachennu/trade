#!/usr/bin/env bash
# Detached all-day paper runner with auto-restart. Never places live orders.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data
export PYTHONPATH="$ROOT/src"
export PYTHONUNBUFFERED=1
STOP_AT="${STOP_AT:-23:00}"
CONFIG="${CONFIG:-config.yaml}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
WRAPPER_LOG="${WRAPPER_LOG:-$ROOT/data/wrapper.log}"
STOP_FILE="$ROOT/data/STOP"

echo $$ > "$ROOT/data/wrapper.pid"

log() {
  # America/New_York stamp, same zone as the bot
  ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '%s %s\n' "$ts" "$*" | tee -a "$WRAPPER_LOG"
}

past_stop() {
  "$PYTHON" - "$STOP_AT" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
hh, mm = map(int, sys.argv[1].split(":")[:2])
now = datetime.now(ZoneInfo("America/New_York"))
raise SystemExit(0 if (now.hour > hh or (now.hour == hh and now.minute >= mm)) else 1)
PY
}

CHILD=""
shutdown() {
  log "wrapper received stop signal"
  if [[ -n "$CHILD" ]] && kill -0 "$CHILD" 2>/dev/null; then
    kill -TERM "$CHILD" 2>/dev/null || true
    wait "$CHILD" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT

log "wrapper start pid=$$ stop_at=${STOP_AT} ET config=${CONFIG} (paper only)"

while true; do
  if [[ -f "$STOP_FILE" ]]; then
    log "data/STOP present — not starting another bot process"
    exit 0
  fi
  if past_stop; then
    log "stop_at ${STOP_AT} America/New_York reached — wrapper exiting"
    exit 0
  fi
  log "launching paper bot"
  # Bot already writes data/bot.log itself. Do not also redirect stdout there
  # (that duplicated every line). Capture stdout/stderr on the wrapper log.
  "$PYTHON" -m polymarket_bot --config "$CONFIG" --duration 0 --stop-at "$STOP_AT" >> "$WRAPPER_LOG" 2>&1 &
  CHILD=$!
  echo "$CHILD" > "$ROOT/data/bot.pid"
  log "bot pid=$CHILD"
  wait "$CHILD"
  code=$?
  CHILD=""
  if [[ -f "$STOP_FILE" ]]; then
    log "bot exited ${code}; data/STOP present — wrapper exiting"
    exit 0
  fi
  if past_stop; then
    log "bot exited ${code}; stop_at reached — wrapper exiting"
    exit 0
  fi
  log "bot exited ${code} — restarting in 5s"
  sleep 5
done
