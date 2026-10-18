#!/usr/bin/env bash
set -euo pipefail

# midihub uninstall script.
# Default mode stops services and removes the installation while preserving a
# config backup in /opt/midihub-backup-<timestamp>.
# Use --purge to remove config and backups too.

DEST=/opt/midihub
PURGE=false

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

log() { printf '[uninstall] %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

for svc in midihub qmidinet; do
  if [[ -f "/etc/systemd/system/${svc}.service" ]]; then
    log "Stopping and disabling ${svc}..."
    systemctl stop "${svc}.service" || true
    systemctl disable "${svc}.service" || true
    rm -f "/etc/systemd/system/${svc}.service"
  fi
done

systemctl daemon-reload

if [[ -d "$DEST" ]]; then
  if [[ "$PURGE" == true ]]; then
    log "Removing $DEST..."
    rm -rf "$DEST"
  else
    ts=$(date +%Y%m%d-%H%M%S)
    backup="/opt/midihub-backup-$ts"
    log "Preserving $DEST at $backup..."
    mv "$DEST" "$backup"
    echo "To purge this backup, run: sudo rm -rf $backup"
  fi
fi

log "Uninstall complete."
if [[ "$PURGE" != true ]]; then
  echo "Your config/data was preserved in the backup above."
  echo "To reinstall, copy the midihub folder to /opt/midihub and run sudo ./update.sh."
fi
