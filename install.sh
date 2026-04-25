#!/usr/bin/env bash
# install.sh — install or uninstall dashy
# Usage:
#   sudo bash install.sh            # install / upgrade
#   sudo bash install.sh --uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME=dashy
SERVICE_FILE=/etc/systemd/system/${SERVICE_NAME}.service
SUDOERS_FILE=/etc/sudoers.d/${SERVICE_NAME}
PORT=7800

# ── must run as root via sudo ─────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "error: run as root — sudo bash $0 $*"
  exit 1
fi

# The user who invoked sudo — dashy will run as this user and start services as them
DASHY_USER="${SUDO_USER:-ubuntu}"
DASHY_HOME="$(getent passwd "$DASHY_USER" | cut -d: -f6)"
echo "==> dashy will run as: $DASHY_USER"

# ── uninstall ─────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  echo "==> Uninstalling dashy..."

  systemctl stop    ${SERVICE_NAME} 2>/dev/null || true
  systemctl disable ${SERVICE_NAME} 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload

  rm -f "$SUDOERS_FILE"

  # Remove old unit name if present
  systemctl disable dev-dashboard 2>/dev/null || true
  rm -f /etc/systemd/system/dev-dashboard.service
  systemctl daemon-reload

  echo "==> dashy uninstalled."
  exit 0
fi

# ── install / upgrade ─────────────────────────────────────────────────────────
echo "==> Installing dashy..."

# Remove old unit name if present
systemctl disable dev-dashboard 2>/dev/null || true
rm -f /etc/systemd/system/dev-dashboard.service

# sudoers: allow dashy user to kill ports and stop systemd units
cat > "$SUDOERS_FILE" <<EOF
# dashy: cross-user port kill
${DASHY_USER} ALL=(root) NOPASSWD: /usr/bin/fuser
# dashy: stop/start systemd units (for services managed by systemd)
${DASHY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop *
${DASHY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start *
${DASHY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart *
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"
echo "    sudoers rule written: $SUDOERS_FILE (user: $DASHY_USER)"

# systemd unit — patch User= to the installing user
sed "s|^User=.*|User=${DASHY_USER}|" "$SCRIPT_DIR/${SERVICE_NAME}.service" > "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}
echo "    systemd unit installed (User=${DASHY_USER})"

# health check
echo "==> Waiting for dashy to respond..."
for i in $(seq 1 10); do
  if curl -sf http://localhost:${PORT}/ > /dev/null 2>&1; then
    echo "==> dashy is up — http://localhost:${PORT}/"
    exit 0
  fi
  sleep 1
done
echo "FAIL: dashy did not respond within 10s — check: journalctl -u ${SERVICE_NAME} -n 50"
exit 1
