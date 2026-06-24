#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/pi/PiJardin"        #TODO: update
cd "$REPO_DIR"

# Capture current HEAD before pulling
OLD=$(git rev-parse HEAD)
git fetch origin main --quiet
git reset --hard origin/main   # or `git pull --ff-only` if you prefer
NEW=$(git rev-parse HEAD)

if [ "$OLD" = "$NEW" ]; then
    echo "No changes."
    exit 0
fi

echo "Updating $OLD -> $NEW"

# Update Python deps if they changed
if git diff --name-only "$OLD" "$NEW" | grep -q "deploy/requirements.txt"; then
    /home/pi/venv/bin/pip install -r deploy/requirements.txt
fi

# Re-bootstrap if systemd units or the bootstrap script itself changed.
# bootstrap.sh handles copy + daemon-reload + enable, so no duplication here.
if git diff --name-only "$OLD" "$NEW" | grep -qE "^systemd/|^deploy/bootstrap\.sh"; then
    bash "$REPO_DIR/deploy/bootstrap.sh"
fi

# Restart your sensor service
sudo systemctl restart sensors.service

echo "Deploy done."