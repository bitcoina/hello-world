#!/usr/bin/env bash
# First-time setup on Raspberry Pi OS (Bookworm, 64-bit). Run as root from the repo checkout.
# Assumes: the SSD is mounted (ideally LUKS-encrypted) and Tailscale is installed and logged in.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update
apt-get install -y python3-venv python3-dev build-essential ffmpeg

id barometer >/dev/null 2>&1 || useradd --system --home /srv/barometer --shell /usr/sbin/nologin barometer
install -d -m 700 -o barometer -g barometer /srv/barometer /srv/barometer/data
install -d -m 700 -o barometer -g barometer /mnt/barometer-backup

python3 -m venv /srv/barometer/venv
/srv/barometer/venv/bin/pip install --upgrade pip
/srv/barometer/venv/bin/pip install "$REPO[voice]"

if [[ ! -f /srv/barometer/data/.env ]]; then
  install -m 600 -o barometer -g barometer "$REPO/deploy/env.example" /srv/barometer/data/.env
  echo "Edit /srv/barometer/data/.env (API key, ntfy, backups) before starting."
fi

echo "Create the owner login and the Private store:"
sudo -u barometer /srv/barometer/venv/bin/barometer --data-dir /srv/barometer/data init

install -m 644 "$REPO/deploy/barometer.service" /etc/systemd/system/barometer.service
systemctl daemon-reload
systemctl enable --now barometer

# HTTPS on your tailnet only (needed for the microphone and the installable app).
tailscale serve --bg --https=443 http://127.0.0.1:8000
echo "Done. Open https://$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')/ on your phone."
