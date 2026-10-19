#!/usr/bin/env bash
set -euo pipefail

# midihub installer for Debian-based systems (Raspberry Pi OS, Debian, Ubuntu).
# Installs to /opt/midihub, runs as the "midihub" system user and starts on boot.
#   sudo ./setup.sh               install with network MIDI (qmidinet)
#   sudo ./setup.sh --no-qmidinet install without network MIDI

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/midihub
SVC_USER=midihub
QMIDINET=true

for arg in "$@"; do
  case "$arg" in
    --no-qmidinet) QMIDINET=false ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

pkgs=(python3 python3-rtmidi python3-flask alsa-utils rsync)
$QMIDINET && pkgs+=(qmidinet)
apt-get update
apt-get install -y "${pkgs[@]}"

id "$SVC_USER" >/dev/null 2>&1 || useradd -r -M -d /nonexistent -G audio -s /usr/sbin/nologin "$SVC_USER"
usermod -aG audio "$SVC_USER"

mkdir -p "$DEST"
rsync -a --exclude='.git' --exclude='config.json' --exclude='personal' --exclude='__pycache__' "$SRC"/ "$DEST"/
if [[ ! -f "$DEST/config.json" ]]; then
  echo '{"devices": {}, "routes": [], "mappings": [], "init": {}, "network": {}}' > "$DEST/config.json"
fi
chown -R "$SVC_USER:$SVC_USER" "$DEST"

cp "$SRC/midihub.service" /etc/systemd/system/
if $QMIDINET; then
  cp "$SRC/qmidinet.service" /etc/systemd/system/
fi
systemctl daemon-reload

echo
echo "The web page lets anyone who can reach it change your routing."
echo "Set a password to open it to your network, or press Enter to keep it"
echo "reachable from this machine only (you can set one later)."
sudo -u "$SVC_USER" python3 "$DEST/web.py" --set-password || true

if $QMIDINET; then
  systemctl enable --now qmidinet
fi
systemctl enable --now midihub

echo
echo "Installed. midihub starts on every boot."
if grep -q password_hash "$DEST/config.json"; then
  echo "Web UI: http://$(hostname -I | awk '{print $1}'):8080"
else
  echo "Web UI: http://localhost:8080 on this machine only."
  echo "Later: sudo -u $SVC_USER python3 $DEST/web.py --set-password && sudo systemctl restart midihub"
fi
