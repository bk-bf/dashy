#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Disable and remove old unit name if present
if systemctl is-enabled dev-dashboard &>/dev/null 2>&1; then
  sudo systemctl disable dev-dashboard || true
fi
sudo rm -f /etc/systemd/system/dev-dashboard.service

sudo cp "$SCRIPT_DIR/dashy.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dashy
sudo systemctl start dashy

echo "Waiting for dashy to start..."
for i in $(seq 1 10); do
  if curl -sf http://localhost:7800/ > /dev/null 2>&1; then
    echo "dashy is up — http://localhost:7800/"
    exit 0
  fi
  sleep 1
done
echo "FAIL: dashy did not respond within 10s"
exit 1
