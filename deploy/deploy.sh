#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../.env
source "$SCRIPT_DIR/../.env"

cd "$REPO_DIR"

# Capture current HEAD before pulling
OLD=$(git rev-parse HEAD)
git fetch origin main --quiet
git reset --hard origin/main
NEW=$(git rev-parse HEAD)

if [ "$OLD" = "$NEW" ]; then
    echo "No changes."
    exit 0
fi

echo "Updating $OLD -> $NEW"

# Update Python deps if they changed
if git diff --name-only "$OLD" "$NEW" | grep -q "deploy/requirements.txt"; then
    "$VENV_DIR/bin/pip" install -r deploy/requirements.txt
fi

# Re-bootstrap if systemd units or the bootstrap script itself changed.
if git diff --name-only "$OLD" "$NEW" | grep -qE "^systemd/|^deploy/bootstrap\.sh"; then
    bash "$REPO_DIR/deploy/bootstrap.sh"
fi

# Reinstall Grafana provisioning config if it changed (dashboard JSONs are hot-reloaded by Grafana).
if git diff --name-only "$OLD" "$NEW" | grep -q "^grafana/provisioning/"; then
    GRAFANA_PROV_DIR="/etc/grafana/provisioning/dashboards"
    if [ -d "$GRAFANA_PROV_DIR" ]; then
        sed "s|@@REPO_DIR@@|$REPO_DIR|g" \
            "$REPO_DIR/grafana/provisioning/dashboards/pijardin.yaml" \
            | sudo tee "$GRAFANA_PROV_DIR/pijardin.yaml" > /dev/null
        sudo systemctl restart grafana-server
    fi
fi

# Restart the sensor service
sudo systemctl restart sensors.service

echo "Deploy done."
