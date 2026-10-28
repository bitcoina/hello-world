#!/usr/bin/env bash
# Drop everything that isn't coming in over Tailscale. Run once as root on the Pi.
set -euo pipefail
apt-get install -y ufw
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0
ufw --force enable
ufw status verbose
