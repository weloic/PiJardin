#!/usr/bin/env bash
# Run once on a fresh Pi to register all systemd units and start the automation.
set -euo pipefail

REPO_DIR="/home/pi/PiJardin"        # TODO: update if cloned elsewhere

sudo cp "$REPO_DIR"/systemd/*.service \
        "$REPO_DIR"/systemd/*.timer \
        /etc/systemd/system/

sudo systemctl daemon-reload

sudo systemctl enable --now deploy.timer
sudo systemctl enable --now sensors.timer

echo "Bootstrap done. Active timers:"
systemctl list-timers deploy.timer sensors.timer
