#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── sudoers: allow dashy (runs as ubuntu) to kill any port via fuser ──────────
SUDOERS_FILE=/etc/sudoers.d/dashy
cat > "$SUDOERS_FILE" <<'EOF'
# Allow dashy service (ubuntu user) to kill processes on any port
ubuntu ALL=(root) NOPASSWD: /usr/bin/fuser
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"  # validate — aborts if syntax error

# ── systemd unit ──────────────────────────────────────────────────────────────
# Disable and remove old unit name if present
if systemctl is-enabled dev-dashboard &>/dev/null 2>&1; then
  systemctl disable dev-dashboard || true
fi
rm -f /etc/systemd/system/dev-dashboard.service

cp "$SCRIPT_DIR/dashy.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable dashy
systemctl restart dashy

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
