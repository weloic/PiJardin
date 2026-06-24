#!/usr/bin/env bash
# Run once on a fresh Pi to register all systemd units and start the automation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Create .env from the example if it doesn't exist yet, filling in the
# detected repo path so the user only needs to edit other values.
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "Creating .env from .env.example with REPO_DIR=$REPO_DIR"
    sed "s|/home/pi/PiJardin|$REPO_DIR|g" "$REPO_DIR/.env.example" > "$REPO_DIR/.env"
fi

# shellcheck source=../.env
source "$REPO_DIR/.env"

# Expand @@PLACEHOLDER@@ tokens in each unit file before copying to systemd.
for f in "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer; do
    sed \
        -e "s|@@REPO_DIR@@|$REPO_DIR|g" \
        -e "s|@@VENV_DIR@@|${VENV_DIR:-/home/pi/venv}|g" \
        -e "s|@@PYTHON_BIN@@|${PYTHON_BIN:-/usr/bin/python3}|g" \
        -e "s|@@SERVICE_USER@@|${SERVICE_USER:-pi}|g" \
        "$f" | sudo tee "/etc/systemd/system/$(basename "$f")" > /dev/null
done

sudo systemctl daemon-reload

sudo systemctl enable --now deploy.timer
sudo systemctl enable --now sensors.timer

echo "Bootstrap done. Active timers:"
systemctl list-timers deploy.timer sensors.timer
