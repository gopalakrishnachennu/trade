#!/usr/bin/env bash
# Detached live dashboard + public HTTPS tunnel, with auto-restart.
# Read-only. Never places orders. Binds only to 127.0.0.1. Does not touch trading.
#
# This box's resolver sinkholes most tunnel hostnames to 198.18.0.1, so SSH
# looks up the real endpoint via DNS-over-HTTPS (dns.google).
# Preference: pinggy.io, then serveo.net, then localhost.run.
# TUNNEL=pinggy|serveo|localhost.run|cloudflared|auto (default auto).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data bin
export PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8787}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
CLOUDFLARED="${CLOUDFLARED:-$ROOT/bin/cloudflared}"
TUNNEL_KIND="${TUNNEL:-auto}"
TOKEN_FILE="$ROOT/data/dashboard_token.txt"
URL_FILE="$ROOT/data/dashboard_url.txt"
KIND_FILE="$ROOT/data/tunnel_kind.txt"
STOP_FILE="$ROOT/data/DASHBOARD_STOP"
LOG="$ROOT/data/dashboard.log"
TUNNEL_LOG="$ROOT/data/tunnel.log"
KNOWN_HOSTS="$ROOT/data/tunnel_known_hosts"

echo $$ > "$ROOT/data/dashboard_wrapper.pid"
rm -f "$STOP_FILE"

log() {
  ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '%s %s\n' "$ts" "$*" | tee -a "$LOG"
}

if [[ ! -s "$TOKEN_FILE" ]]; then
  "$PYTHON" - "$TOKEN_FILE" <<'PY'
import secrets, sys
from pathlib import Path
p = Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(secrets.token_urlsafe(24) + "\n")
p.chmod(0o600)
PY
  log "generated access token in data/dashboard_token.txt"
fi
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
export DASHBOARD_TOKEN="$TOKEN"

kill_pidfile() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    return
  fi
  local pid
  pid="$(tr -d "[:space:]" < "$file" || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
}

SERVER_LOOP=""
TUNNEL_LOOP=""
WATCH=""
HEALTH=""
shutdown() {
  log "dashboard wrapper received stop signal"
  touch "$STOP_FILE"
  kill_pidfile "$ROOT/data/tunnel.pid"
  kill_pidfile "$ROOT/data/cloudflared.pid"
  kill_pidfile "$ROOT/data/dashboard.pid"
  if [[ -n "$WATCH" ]]; then kill -TERM "$WATCH" 2>/dev/null || true; fi
  if [[ -n "$HEALTH" ]]; then kill -TERM "$HEALTH" 2>/dev/null || true; fi
  if [[ -n "$TUNNEL_LOOP" ]]; then kill -TERM "$TUNNEL_LOOP" 2>/dev/null || true; fi
  if [[ -n "$SERVER_LOOP" ]]; then kill -TERM "$SERVER_LOOP" 2>/dev/null || true; fi
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

log "dashboard wrapper start pid=$$ bind=${HOST}:${PORT} tunnel=${TUNNEL_KIND} (paper only, localhost)"
# Do not keep a previous session's public URL; it may already be dead.
rm -f "$URL_FILE"

extract_url() {
  # Only parse the current attempt (after the last start/attempt/watchdog marker),
  # never a stale hostname leftover from an earlier session.
  # Assigned hostname only — never marketing links.
  awk '
    /======= dashboard tunnel start / { buf=""; next }
    /======= watchdog restart / { buf=""; next }
    /======= attempt / { buf=""; next }
    { buf = buf $0 "\n" }
    END { printf "%s", buf }
  ' "$TUNNEL_LOG" 2>/dev/null     | grep -oE 'https://[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)*\.(trycloudflare\.com|app\.lhr\.life|lhr\.life|pinggy\.link|a\.pinggy\.io|free\.pinggy\.online|a\.free\.pinggy\.online|pinggy\.app|serveo\.net|serveousercontent\.com)'     | grep -vE 'admin\.|docs\.|www\.|dashboard\.|console\.serveo'     | tail -1 || true
}

write_url() {
  local base="$1"
  if [[ -z "$base" ]]; then
    return
  fi
  base="${base%/}"
  printf '%s\n' "${base}/?token=${TOKEN}" > "$URL_FILE"
  chmod 600 "$URL_FILE"
  log "public URL: ${base}/?token=…  (full URL in data/dashboard_url.txt; changes if the tunnel restarts)"
}

doh_first_ip() {
  local name="$1"
  curl -sS --max-time 8 "https://dns.google/resolve?name=${name}&type=A" \
    | "$PYTHON" -c 'import json,sys
d=json.load(sys.stdin)
for a in d.get("Answer") or []:
    if a.get("type")==1 and a.get("data"):
        print(a["data"]); raise SystemExit
raise SystemExit(1)'
}

# Launch uvicorn under a restart loop in the background
(
  while true; do
    if [[ -f "$STOP_FILE" ]]; then
      exit 0
    fi
    log "launching dashboard server on ${HOST}:${PORT}"
    "$PYTHON" -m uvicorn app:app --app-dir "$ROOT/web" --host "$HOST" --port "$PORT" --log-level warning >> "$LOG" 2>&1 &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$ROOT/data/dashboard.pid"
    wait "$SERVER_PID"
    code=$?
    if [[ -f "$STOP_FILE" ]]; then
      log "dashboard server exited ${code}; STOP present"
      exit 0
    fi
    log "dashboard server exited ${code} — restarting in 2s"
    sleep 2
  done
) &
SERVER_LOOP=$!

ssh_common() {
  # Usage after this: ... -p PORT -o HostName=IP user@alias  — extra args passed in
  return 0
}

start_pinggy() {
  local ip
  ip="$(doh_first_ip a.pinggy.io)" || true
  if [[ -z "$ip" ]]; then
    log "ERROR: could not resolve a.pinggy.io via DNS-over-HTTPS"
    return 1
  fi
  local key="$ROOT/data/tunnel_ssh_key"
  if [[ ! -f "$key" ]]; then
    ssh-keygen -t ed25519 -f "$key" -N "" -C "pm-dashboard-tunnel" >/dev/null
    chmod 600 "$key"
  fi
  log "connecting pinggy via TLS+SNI to DoH IP ${ip}"
  # Port 443 is TLS (SNI a.pinggy.io). Direct TCP to the IP gets HTTP 400.
  ssh \
    -o ProxyCommand="python3 -u $ROOT/scripts/tls_sni_proxy.py ${ip} a.pinggy.io 443" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o HostName=a.pinggy.io \
    -o IdentitiesOnly=yes \
    -i "$key" \
    -o NumberOfPasswordPrompts=0 \
    -T \
    -R "0:127.0.0.1:${PORT}" \
    a.pinggy.io >> "$TUNNEL_LOG" 2>&1
}

start_serveo() {
  local ip
  ip="$(doh_first_ip serveo.net)" || true
  if [[ -z "$ip" ]]; then
    log "ERROR: could not resolve serveo.net via DNS-over-HTTPS"
    return 1
  fi
  local key="$ROOT/data/tunnel_ssh_key"
  if [[ ! -f "$key" ]]; then
    ssh-keygen -t ed25519 -f "$key" -N "" -C "pm-dashboard-tunnel" >/dev/null
    chmod 600 "$key"
  fi
  log "connecting serveo.net via DoH IP ${ip} as user tunnel"
  ssh \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o HostName="$ip" \
    -o HostKeyAlias=serveo.net \
    -o User=tunnel \
    -o PreferredAuthentications=publickey,keyboard-interactive \
    -o KbdInteractiveAuthentication=yes \
    -o PubkeyAuthentication=yes \
    -o IdentitiesOnly=yes \
    -i "$key" \
    -o NumberOfPasswordPrompts=1 \
    -T \
    -R "80:127.0.0.1:${PORT}" \
    serveo.net >> "$TUNNEL_LOG" 2>&1
}

start_localhost_run() {
  local ip
  ip="$(doh_first_ip localhost.run)" || true
  if [[ -z "$ip" ]]; then
    log "ERROR: could not resolve localhost.run via DNS-over-HTTPS"
    return 1
  fi
  log "connecting localhost.run via DoH IP ${ip} (fallback)"
  ssh \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o HostName="$ip" \
    -o PreferredAuthentications=none \
    -o PubkeyAuthentication=no \
    -o PasswordAuthentication=no \
    -T \
    -R "80:127.0.0.1:${PORT}" \
    nokey@localhost.run >> "$TUNNEL_LOG" 2>&1
}

start_cloudflared() {
  if [[ ! -x "$CLOUDFLARED" ]]; then
    log "ERROR: cloudflared not found at $CLOUDFLARED"
    return 1
  fi
  log "launching cloudflared quick tunnel (may fail if argotunnel.com is sinkholed)"
  "$CLOUDFLARED" tunnel --no-autoupdate --url "http://${HOST}:${PORT}" >> "$TUNNEL_LOG" 2>&1
}

start_one_tunnel() {
  local kind="$1"
  case "$kind" in
    pinggy) start_pinggy ;;
    serveo) start_serveo ;;
    localhost.run|lhr) start_localhost_run ;;
    cloudflared) start_cloudflared ;;
    *) log "unknown tunnel kind $kind"; return 1 ;;
  esac
}

# Rotating preference. Cloudflare is last because this box usually sinkholes it.
ALL_KINDS=(serveo pinggy localhost.run)
if [[ "$TUNNEL_KIND" == "cloudflared" ]]; then
  ALL_KINDS=(cloudflared pinggy serveo localhost.run)
elif [[ "$TUNNEL_KIND" != "auto" ]]; then
  ALL_KINDS=("$TUNNEL_KIND")
fi

{
  ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '\n======= dashboard tunnel start %s kind=%s =======\n' "$ts" "$TUNNEL_KIND"
} >> "$TUNNEL_LOG"

CURRENT_KIND=""
(
  idx=0
  fails=0
  while true; do
    if [[ -f "$STOP_FILE" ]]; then
      exit 0
    fi
    n=${#ALL_KINDS[@]}
    kind="${ALL_KINDS[$((idx % n))]}"
    CURRENT_KIND="$kind"
    printf '%s\n' "$kind" > "$KIND_FILE"
    {
      ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
      printf '\n======= attempt %s kind=%s =======\n' "$ts" "$kind"
    } >> "$TUNNEL_LOG"
    log "starting tunnel backend: $kind"
    start_one_tunnel "$kind" &
    TUNNEL_PID=$!
    echo "$TUNNEL_PID" > "$ROOT/data/tunnel.pid"
    if [[ "$kind" == "cloudflared" ]]; then
      echo "$TUNNEL_PID" > "$ROOT/data/cloudflared.pid"
    fi
    got=""
    for _ in $(seq 1 25); do
      if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        break
      fi
      u="$(extract_url)"
      if [[ -n "$u" ]]; then
        write_url "$u"
        got="$u"
        fails=0
        break
      fi
      sleep 1
    done
    if [[ -z "$got" ]]; then
      log "tunnel $kind produced no URL in time — rotating"
      kill -TERM "$TUNNEL_PID" 2>/dev/null || true
      sleep 0.5
      kill -KILL "$TUNNEL_PID" 2>/dev/null || true
      wait "$TUNNEL_PID" 2>/dev/null || true
      fails=$((fails + 1))
      idx=$((idx + 1))
      sleep 2
      continue
    fi
    wait "$TUNNEL_PID"
    code=$?
    if [[ -f "$STOP_FILE" ]]; then
      log "tunnel exited ${code}; STOP present"
      exit 0
    fi
    log "tunnel ($kind) exited ${code} — URL will change on restart; retrying"
    fails=$((fails + 1))
    # After two drops of the same backend, try the next one.
    if [[ $fails -ge 2 ]]; then
      idx=$((idx + 1))
      fails=0
    fi
    sleep 3
  done
) &
TUNNEL_LOOP=$!

# Keep dashboard_url.txt in sync if the hostname rotates in-place.
(
  last=""
  while true; do
    if [[ -f "$STOP_FILE" ]]; then
      exit 0
    fi
    u="$(extract_url)"
    if [[ -n "$u" && "$u" != "$last" ]]; then
      write_url "$u"
      last="$u"
    fi
    sleep 2
  done
) &
WATCH=$!

# Health watchdog: every ~15s, fetch the public URL. On 502 / "no tunnel" / hang, restart the tunnel.
(
  sleep 20
  cooldown=0
  while true; do
    if [[ -f "$STOP_FILE" ]]; then
      exit 0
    fi
    if [[ $cooldown -gt 0 ]]; then
      cooldown=$((cooldown - 15))
      sleep 15
      continue
    fi
    url=""
    if [[ -s "$URL_FILE" ]]; then
      url="$(tr -d '[:space:]' < "$URL_FILE")"
    fi
    unhealthy=0
    reason=""
    if [[ -z "$url" ]]; then
      unhealthy=1
      reason="no url file"
    else
      tmp="$(mktemp)"
      code="$(curl -sS --max-time 12 -o "$tmp" -w '%{http_code}' "$url" || true)"
      body="$(head -c 200 "$tmp" 2>/dev/null || true)"
      rm -f "$tmp"
      if [[ "$code" == "000" || -z "$code" ]]; then
        unhealthy=1
        reason="curl failed"
      elif [[ "$code" == "502" || "$code" == "503" || "$code" == "504" ]]; then
        unhealthy=1
        reason="http $code"
      elif echo "$body" | grep -qiE 'no tunnel|tunnel not found|tunnel removed|could not be found|502 Bad Gateway'; then
        unhealthy=1
        reason="body: $(echo "$body" | tr '\n' ' ' | head -c 80)"
      fi
    fi
    if [[ $unhealthy -eq 1 ]]; then
      log "tunnel health FAILED (${reason}) — restarting tunnel"
      {
        ts="$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
        printf '\n======= watchdog restart %s reason=%s =======\n' "$ts" "$reason"
      } >> "$TUNNEL_LOG"
      if [[ -f "$ROOT/data/tunnel.pid" ]]; then
        tpid="$(tr -d '[:space:]' < "$ROOT/data/tunnel.pid" || true)"
        if [[ -n "$tpid" ]]; then
          kill -TERM "$tpid" 2>/dev/null || true
          sleep 1
          kill -KILL "$tpid" 2>/dev/null || true
        fi
      fi
      cooldown=25
    fi
    sleep 15
  done
) &
HEALTH=$!

wait "$SERVER_LOOP" "$TUNNEL_LOOP"
touch "$STOP_FILE"
kill "$WATCH" 2>/dev/null || true
kill "$HEALTH" 2>/dev/null || true
wait "$WATCH" 2>/dev/null || true
wait "$HEALTH" 2>/dev/null || true
log "dashboard wrapper exiting"
