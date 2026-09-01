#!/usr/bin/env bash
# Bring up the local debug stack, provisioned from THIS repo's production Grafana config.
#
# The provisioning is generated rather than copied. A hand-maintained local copy would drift
# from the Pi's the first time a panel or a datasource setting changed, and a dashboard that
# quietly differs from the one being debugged is worse than no dashboard at all. So the two
# values that genuinely cannot be shared — the datasource URL and the token — are substituted
# out of the production files on every run, and nothing else is touched.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=dev.env
source ./dev.env

if ! command -v docker > /dev/null 2>&1; then
    echo "docker is not installed. On Debian/Ubuntu:" >&2
    echo "  sudo apt install docker.io docker-compose-v2" >&2
    echo "  sudo usermod -aG docker \$USER   # then log out and back in" >&2
    exit 1
fi

PROD_DATASOURCE="../grafana/provisioning/datasources/influxdb.yaml"
PROD_DASHBOARDS="../grafana/provisioning/dashboards/pijardin.yaml"
OUT="./provisioning"

for f in "$PROD_DATASOURCE" "$PROD_DASHBOARDS"; do
    [ -f "$f" ] || { echo "missing $f — run this from a full checkout" >&2; exit 1; }
done

mkdir -p "$OUT/datasources" "$OUT/dashboards"

# Running `docker compose up` by hand instead of this script leaves Docker to create the
# bind-mount source itself, as an empty directory owned by root: Grafana then starts with no
# datasource, and this script's next run fails on a bare "permission denied" that says nothing
# about why. Name the cause instead.
if [ ! -w "$OUT/datasources" ]; then
    echo "$OUT is not writable — Docker likely created it as root after a bare" >&2
    echo "\`docker compose up\`. Remove it and use this script:" >&2
    echo "  sudo rm -rf $OUT && ./up.sh" >&2
    exit 1
fi

# Same substitution deploy/bootstrap.sh performs on the Pi ('|' as the sed delimiter because a
# token may contain '/'), plus the host: inside compose the database answers to a service name.
# The datasource uid is deliberately NOT touched — all seven panels reference it by uid, and
# it is what makes the committed dashboard JSON work here untouched.
sed -e "s|__INFLUXDB_TOKEN__|$DEV_TOKEN|" \
    -e "s|http://localhost:8086|http://influxdb:8086|" \
    "$PROD_DATASOURCE" > "$OUT/datasources/influxdb.yaml"

# The one deliberate divergence from production. The Pi sets allowUiUpdates: false so the repo
# stays the single source of truth for what the dashboard is; locally the opposite is wanted,
# since rearranging a panel to see something is the whole reason it is on your screen. Edits
# made here are local and vanish with `docker compose down -v`.
sed -e "s|allowUiUpdates: false|allowUiUpdates: true|" \
    "$PROD_DASHBOARDS" > "$OUT/dashboards/pijardin.yaml"

docker compose --env-file dev.env up -d

cat <<EOF

Grafana  : http://localhost:3000   (dashboard "PiJardin", no login)
InfluxDB : http://localhost:8086

The database is empty until an export is loaded into it:
  1. on Telegram   /export 8d
  2. here          ./import.sh ~/Downloads/pijardin_8d_*.lp.gz

Tear down, data included: docker compose down -v
EOF
