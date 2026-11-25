#!/usr/bin/env bash
# Stop the detached paper runner. Open positions are left as-is in the DB
# (they will settle if you start the bot again before their windows end).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data
touch data/STOP
echo "Wrote $ROOT/data/STOP (wrapper will not restart)."

kill_pidfile() {
  local file="$1" name="$2"
  if [[ ! -f "$file" ]]; then
    echo "No $name pid file ($file)."
    return 0
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$file")"
  if [[ -z "$pid" ]]; then
    echo "$name pid file empty."
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "Sending SIGTERM to $name pid $pid"
    kill -TERM "$pid" 2>/dev/null || true
  else
    echo "$name pid $pid is not running."
  fi
}

kill_pidfile "$ROOT/data/wrapper.pid" "wrapper"
# Give the wrapper a moment to TERM the child, then TERM the bot too if needed.
sleep 1
kill_pidfile "$ROOT/data/bot.pid" "bot"
echo "To only block new paper entries without stopping the process:  touch $ROOT/data/KILL"
