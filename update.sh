#!/usr/bin/env bash
set -euo pipefail

# midihub safe update script.
# Run after copying a new midihub folder over /opt/midihub or from the source tree.
# Preserves your existing config.json, backups existing unit files, and restarts services.

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/midihub
SVC_USER=midihub
CONFIG="$DEST/config.json"
UNIT="/etc/systemd/system/midihub.service"
QUNIT="/etc/systemd/system/qmidinet.service"

log() { printf '[update] %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  log "Installing rsync..."
  apt-get install -y rsync
fi

if [[ ! -d "$DEST" ]]; then
  echo "Existing installation not found at $DEST" >&2
  echo "Run the full setup script first, or copy the midihub folder to /opt/midihub." >&2
  exit 1
fi

ts=$(date +%Y%m%d-%H%M%S)
backup_dir="/opt/midihub-backup-$ts"
mkdir -p "$backup_dir"

[[ -f "$CONFIG" ]] && cp -a "$CONFIG" "$backup_dir/config.json"
[[ -f "$UNIT" ]] && cp -a "$UNIT" "$backup_dir/midihub.service"
[[ -f "$QUNIT" ]] && cp -a "$QUNIT" "$backup_dir/qmidinet.service"

log "Stopping services..."
systemctl stop midihub.service || true
systemctl stop qmidinet.service || true

log "Syncing files to $DEST..."
rsync -a --delete \
  --exclude='config.json' \
  --exclude='.midihub-secret' \
  --exclude='personal' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='*.bak' \
  --exclude='*.backup' \
  "$SRC"/ "$DEST"/

# Keep a config file if the Pi did not have one yet.
if [[ ! -f "$CONFIG" ]] && [[ -f "$DEST/config.example.json" ]]; then
  cp -a "$DEST/config.example.json" "$CONFIG"
fi
chown -R "$SVC_USER:$SVC_USER" "$DEST" 2>/dev/null || true

log "Installing service units..."
if [[ -f "$SRC/midihub.service" ]]; then
  cp -a "$SRC/midihub.service" "$UNIT"
fi
if [[ -f "$SRC/qmidinet.service" ]]; then
  cp -a "$SRC/qmidinet.service" "$QUNIT"
fi

systemctl daemon-reload
systemctl enable --now midihub
if [[ -f "$QUNIT" ]]; then
  systemctl enable --now qmidinet
fi

log "Restarting services..."
systemctl restart qmidinet || true
systemctl restart midihub

sleep 1
if systemctl is-active --quiet midihub; then
  log "Update complete."
  log "Web UI: http://$(hostname -I | awk '{print $1}'):8080"
else
  log "ERROR: midihub failed to start. Restoring previous config..."
  if [[ -f "$backup_dir/config.json" ]]; then
    cp -a "$backup_dir/config.json" "$CONFIG"
  fi
  systemctl restart midihub || true
  echo "See: sudo journalctl -u midihub -n 50" >&2
  exit 1
fi
