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

# bossac uploads firmware to the Arduino XIAO SAMD21 (used by arduino/flash_firmware.py).
# Guarded so re-bootstraps stay fast; deploy.sh re-runs this script whenever it changes,
# so pushing the change installs bossac on the Pi with no SSH.
if ! command -v bossac > /dev/null; then
    echo "Installing bossa-cli (bossac) for Arduino flashing..."
    sudo apt-get install -y bossa-cli || echo "WARNING: bossa-cli install failed; /flash will not work until it is installed."
fi
# Always report the result so it is visible in `/logs deploy` on every deploy.
echo "bossac: $(command -v bossac || echo 'MISSING')"

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
sudo systemctl enable --now telegram-bot.service
# Continuous listener for the pump board. Enabled unconditionally: with no board plugged in
# it just logs "pump board not available" and backs off, which is the correct behaviour and
# is visible in `/logs pump` — quieter than a unit nobody remembered to enable.
sudo systemctl enable --now pump.service

# Install Grafana provisioning config and seed dashboards
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

    sudo cp "$REPO_DIR"/grafana/dashboards/*.json "$GRAFANA_DASH_DIR/" 2>/dev/null || true
    sudo chown -R grafana:grafana "$GRAFANA_DASH_DIR"
    sudo systemctl restart grafana-server
fi

echo "Bootstrap done. Active timers:"
systemctl list-timers deploy.timer sensors.timer
