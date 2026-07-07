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

# Sync Grafana dashboards and provisioning config if either changed.
if git diff --name-only "$OLD" "$NEW" | grep -q "^grafana/"; then
    GRAFANA_PROV_DIR="/etc/grafana/provisioning/dashboards"
    GRAFANA_DS_DIR="/etc/grafana/provisioning/datasources"
    GRAFANA_DASH_DIR="/var/lib/grafana/dashboards"
    if [ -d "$GRAFANA_PROV_DIR" ]; then
        # Datasource provisioning: inject the InfluxDB token from .env into the
        # committed template ('|' as sed delimiter: base64 tokens may contain '/')
        sudo mkdir -p "$GRAFANA_DS_DIR"
        sed "s|__INFLUXDB_TOKEN__|$INFLUXDB_TOKEN|" \
            "$REPO_DIR/grafana/provisioning/datasources/influxdb.yaml" \
            | sudo tee "$GRAFANA_DS_DIR/influxdb.yaml" > /dev/null
        sudo chown root:grafana "$GRAFANA_DS_DIR/influxdb.yaml"
        sudo chmod 640 "$GRAFANA_DS_DIR/influxdb.yaml"

        sudo cp "$REPO_DIR/grafana/provisioning/dashboards/pijardin.yaml" "$GRAFANA_PROV_DIR/pijardin.yaml"
        sudo mkdir -p "$GRAFANA_DASH_DIR"

        sudo cp "$REPO_DIR"/grafana/dashboards/*.json "$GRAFANA_DASH_DIR/"
        sudo chown -R grafana:grafana "$GRAFANA_DASH_DIR"
        sudo systemctl restart grafana-server
    fi
fi

# Restart the sensor service
sudo systemctl restart sensors.service

# Restart the bot if anything in its folder changed (token picked up from .env automatically)
if git diff --name-only "$OLD" "$NEW" | grep -q "^telegram_bot/"; then
    sudo systemctl restart telegram-bot.service
fi

echo "Deploy done."
