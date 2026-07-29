#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../.env
source "$SCRIPT_DIR/../.env"

cd "$REPO_DIR"

# Capture current HEAD before pulling
OLD=$(git rev-parse HEAD)
# TEMP (dev test): one-shot hand-off to v2_arduino. The Pi runs the deploy.sh already
# on its disk, so this tick only installs the pointer -- the next tick reads it and
# moves the checkout. Revert this commit once the Pi has moved; v2_arduino carries its
# own pointer from there. Bare `fetch origin` so origin/v2_arduino exists on a Pi that
# has only ever fetched main.
git fetch origin --quiet
git reset --hard origin/v2_arduino
NEW=$(git rev-parse HEAD)

if [ "$OLD" = "$NEW" ]; then
    echo "No changes."
    exit 0
fi

echo "Updating $OLD -> $NEW"

# Update Python deps if they changed. Non-fatal: a failed install must not abort
# the deploy before the service restarts below, or the bot keeps running stale
# code in memory (a missing lib fails loudly at import instead, and graph.py is
# already import-guarded).
if git diff --name-only "$OLD" "$NEW" | grep -q "deploy/requirements.txt"; then
    if ! "$VENV_DIR/bin/pip" install -r deploy/requirements.txt; then
        echo "WARNING: pip install failed; continuing so services still restart."
    fi
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

# Kick off a sensor run only when its code changed. --no-block: the service is a
# oneshot that performs a full measurement, and a transient measurement failure
# must not abort the deploy (sensors.timer re-runs it within 5 minutes anyway).
if git diff --name-only "$OLD" "$NEW" | grep -qE "^sensors/"; then
    sudo systemctl restart --no-block sensors.service
fi

# Restart the bot if its code, the sensor module it imports, or the Python deps
# changed (token picked up from .env automatically)
if git diff --name-only "$OLD" "$NEW" | grep -qE "^telegram_bot/|^sensors/|^deploy/requirements.txt"; then
    sudo systemctl restart telegram-bot.service
fi

# Run any pending one-time migrations. A migration is a script in deploy/migrations/
# that must execute exactly once on this Pi (e.g. a data backfill, a firmware flash).
# Applied names are recorded in a gitignored ledger so `git reset --hard` above never
# re-runs them. Placed last (before the notification) so migrations see fully-updated
# code, deps and services. Non-fatal to the deploy; a failed migration stays unrecorded
# and is retried on the next deploy tick.
MIGRATIONS_DIR="$REPO_DIR/deploy/migrations"
LEDGER="$REPO_DIR/deploy/.migrations_applied"
touch "$LEDGER"

if [ -d "$MIGRATIONS_DIR" ]; then
    # Glob expands sorted, so the NNNN_ prefix defines run order.
    for path in "$MIGRATIONS_DIR"/*; do
        [ -f "$path" ] || continue
        name="$(basename "$path")"
        [ "$name" = "README.md" ] && continue
        grep -qxF "$name" "$LEDGER" && continue          # already applied

        echo "Running migration: $name"
        # Env passed explicitly (not exported), matching the notification call below.
        # `bash "$path"` avoids depending on the file's exec bit surviving git/Windows.
        if REPO_DIR="$REPO_DIR" VENV_DIR="$VENV_DIR" \
           TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}" bash "$path"; then
            echo "$name" >> "$LEDGER"
        else
            echo "WARNING: migration $name failed; will retry next deploy."
            if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
                TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" "$VENV_DIR/bin/python" -c \
                    "import sys; sys.path.insert(0, '$REPO_DIR/telegram_bot'); import alerts; \
                     alerts.send_telegram(alerts.admin_recipients(), '⚠️ Migration $name failed — check journalctl -u deploy.service')" \
                    || echo "WARNING: could not send migration-failure notification."
            fi
            # Stop at the first failure: later migrations may depend on this one, so
            # preserve ordering and retry this + all following ones next deploy.
            break
        fi
    done
fi

# Notify admins over Telegram that a new version is live. Sent from here (not the
# bot, which just restarted) via alerts.py's direct API call. The token is a shell
# var from .env, not exported, so pass it explicitly to the subprocess. Non-fatal:
# the deploy has already succeeded, so a notification failure must not abort it.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    git log --oneline "$OLD..$NEW" \
        | TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
          "$VENV_DIR/bin/python" "$REPO_DIR/telegram_bot/alerts.py" deploy "$OLD" "$NEW" \
        || echo "WARNING: could not send deploy notification."
fi

# Record a Point('version') marker in InfluxDB (Pi commit, Arduino firmware, Grafana config)
# so a version change can be overlaid as a Grafana annotation. The InfluxDB vars are shell
# vars from .env (sourced above, not exported), so pass them explicitly to the subprocess.
# Non-fatal: the deploy has already succeeded.
INFLUX_URL="${INFLUX_URL:-}" INFLUXDB_TOKEN="${INFLUXDB_TOKEN:-}" \
INFLUX_ORG="${INFLUX_ORG:-}" INFLUX_BUCKET="${INFLUX_BUCKET:-}" \
    "$VENV_DIR/bin/python" "$REPO_DIR/sensors/read_puit.py" record-version deploy \
    || echo "WARNING: could not record version marker."

echo "Deploy done."
