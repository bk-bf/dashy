#!/usr/bin/env bash
# dashy-set-restart.sh — write a systemd dropin to override Restart= for a unit.
# Called by dashy server via sudo. Installed to /usr/local/bin/dashy-set-restart.
# Usage: dashy-set-restart <unit> <policy>
#   policy: always | on-failure | no

set -euo pipefail

UNIT="${1:-}"
POLICY="${2:-}"

if [[ -z "$UNIT" || -z "$POLICY" ]]; then
  echo "usage: dashy-set-restart <unit> <policy>" >&2
  exit 1
fi

if [[ "$POLICY" != "always" && "$POLICY" != "on-failure" && "$POLICY" != "no" ]]; then
  echo "invalid policy '$POLICY' — must be: always | on-failure | no" >&2
  exit 1
fi

# Basic unit name validation — no path traversal
if [[ "$UNIT" =~ [^a-zA-Z0-9_.@-] ]]; then
  echo "invalid unit name '$UNIT'" >&2
  exit 1
fi

DROPIN_DIR="/etc/systemd/system/${UNIT}.d"
DROPIN_FILE="${DROPIN_DIR}/dashy-restart.conf"

mkdir -p "$DROPIN_DIR"
printf '[Service]\nRestart=%s\n' "$POLICY" > "$DROPIN_FILE"
systemctl daemon-reload
echo "Restart=${POLICY} set for ${UNIT} via ${DROPIN_FILE}"
