#!/bin/bash
# Pull archive parts from the droplet to this Mac, verify sha256, then acknowledge.
#
# Config: ~/.config/polymarket/sync.env (chmod 600) with
#   PM_URL="https://104-236-203-158.sslip.io"
#   PM_TOKEN="<dashboard token>"
# Optional: PM_DEST (default ~/PolymarketData), PM_NO_ACK=1 (download + verify only),
#           PM_CURL_OPTS (extra curl options, e.g. "--resolve host:443:ip")
#
# Uses only tools that ship with macOS (bash, curl, shasum, awk). Safe to run
# repeatedly: parts already present with a matching sha256 are not downloaded
# again; an ack is sent only after the local copy verifies. The droplet never
# deletes a part until it has been acknowledged with the matching sha256.
set -euo pipefail

CONF="${PM_SYNC_CONF:-$HOME/.config/polymarket/sync.env}"
if [ ! -f "$CONF" ]; then echo "missing $CONF" >&2; exit 2; fi
# shellcheck disable=SC1090
. "$CONF"
: "${PM_URL:?PM_URL not set in $CONF}"
: "${PM_TOKEN:?PM_TOKEN not set in $CONF}"
DEST="${PM_DEST:-$HOME/PolymarketData}"
ARCH="$DEST/archives"
LOG="$DEST/sync.log"
mkdir -p "$ARCH"
CLIENT="$(scutil --get ComputerName 2>/dev/null || hostname)"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" | tee -a "$LOG"; }
# shellcheck disable=SC2086
api() { curl -fsS --max-time 120 --retry 3 --retry-delay 5 ${PM_CURL_OPTS:-} -H "X-Dashboard-Token: $PM_TOKEN" "$@"; }
sha() { shasum -a 256 "$1" | awk '{print $1}'; }

list="$(api "$PM_URL/api/archives?format=tsv")" || { log "ERROR: cannot list archives"; exit 1; }
n_new=0; n_ack=0
while IFS=$'\t' read -r name want acked; do
  [ -z "${name:-}" ] && continue
  case "$name" in part-[0-9][0-9][0-9][0-9]-*.tar.zst|part-[0-9][0-9][0-9][0-9]-*.tar.gz) ;; *) log "skip odd name $name"; continue;; esac
  f="$ARCH/$name"
  if [ ! -f "$f" ] || [ "$(sha "$f")" != "$want" ]; then
    log "downloading $name"
    api -o "$f.part" "$PM_URL/api/archives/$name"
    got="$(sha "$f.part")"
    if [ "$got" != "$want" ]; then log "ERROR: sha256 mismatch for $name ($got != $want); not acknowledging"; rm -f "$f.part"; continue; fi
    mv "$f.part" "$f"
    man="${name%.tar.*}.manifest.json"
    api -o "$ARCH/$man" "$PM_URL/api/archives/$man" || log "warning: manifest download failed for $name"
    n_new=$((n_new+1))
  fi
  if [ "$acked" != "1" ] && [ "${PM_NO_ACK:-0}" != "1" ]; then
    if api -X POST -H "Content-Type: application/json" \
         -d "{\"sha256\":\"$want\",\"client\":\"$CLIENT\"}" "$PM_URL/api/archives/$name/ack" >/dev/null; then
      log "acknowledged $name"; n_ack=$((n_ack+1))
    else
      log "ERROR: ack failed for $name (will retry next run)"
    fi
  fi
done <<< "$list"
log "done: $n_new new, $n_ack acknowledged, $(ls "$ARCH"/part-*.tar.* 2>/dev/null | wc -l | tr -d ' ') parts stored in $ARCH"
