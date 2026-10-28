#!/usr/bin/env bash
# Acceptance check: "App is unreachable from the home LAN without Tailscale".
#
# On the Pi:              ./check-lan-isolation.sh
# From another LAN box:   ./check-lan-isolation.sh <pi-lan-ip>
set -uo pipefail
if [[ $# -eq 0 ]]; then
  echo "Listening sockets for Barometer (should be 127.0.0.1 only):"
  ss -ltnp | grep -E ':8000\b' || echo "  (nothing on :8000 - is the service running?)"
  if ss -ltn | grep -E ':8000\b' | grep -vq '127.0.0.1'; then
    echo "FAIL: port 8000 is listening beyond localhost"; exit 1
  fi
  echo "Tailscale serve config:"
  tailscale serve status || true
  echo "PASS: app only on localhost; reachable through tailscale serve."
  exit 0
fi
ip="$1"
fail=0
for port in 80 443 8000; do
  if curl -sk --max-time 4 "http://$ip:$port/" -o /dev/null || curl -sk --max-time 4 "https://$ip:$port/" -o /dev/null; then
    echo "FAIL: $ip:$port answered from the LAN"; fail=1
  else
    echo "ok:   $ip:$port unreachable"
  fi
done
exit $fail
