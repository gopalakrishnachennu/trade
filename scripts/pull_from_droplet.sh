#!/usr/bin/env bash
# Rsync paper data back from the DigitalOcean droplet so box-side
# daily_report.py, dashboard_data.py, and Page refresh keep working.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${DROPLET_ENV:-$ROOT/deploy/droplet.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE (droplet IP / ssh key). Deploy the droplet first." >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${DROPLET_IP:?DROPLET_IP must be set in $ENV_FILE}"
KEY="${DROPLET_SSH_KEY:-$ROOT/deploy/do_ed25519}"
REMOTE_USER="${DROPLET_USER:-root}"
REMOTE_DIR="${DROPLET_DIR:-/opt/polymarket-bot}"
SSH_CMD="ssh -i ${KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
mkdir -p "$ROOT/data"
# --delete keeps CSVs/runtime in sync, but never remove box-side URL pointers
# or transient atomic-write tmp files from a live worker.
# Integration keys (data/secrets/) stay on the droplet only; archive tarballs
# go to the Mac via scripts/local_sync/pull_archives.sh, not to this box.
rsync -az --delete \
  -e "$SSH_CMD" \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.tmp' \
  --exclude 'dashboard_url.txt' \
  --exclude 'last_sent_dashboard_url.txt' \
  --exclude 'tunnel.log' \
  --exclude 'tunnel.pid' \
  --exclude 'cloudflared.log' \
  --exclude 'cloudflared.pid' \
  --exclude 'SYSTEM_STOP' \
  --exclude 'DASHBOARD_STOP' \
  --exclude 'secrets/' \
  --exclude 'archive/part-*.tar.*' \
  "${REMOTE_USER}@${DROPLET_IP}:${REMOTE_DIR}/data/" \
  "$ROOT/data/"
echo "pulled ${REMOTE_USER}@${DROPLET_IP}:${REMOTE_DIR}/data/ -> $ROOT/data/"
