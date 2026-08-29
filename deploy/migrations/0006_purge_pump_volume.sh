#!/usr/bin/env bash
# Erase every pump_volume point and rebuild it.
#
# WHY: `quality` started life as a TAG and is now a FIELD. Tags are part of a point's identity in
# InfluxDB, so the runs already recorded kept their old tagged series and every new write landed
# BESIDE the old one instead of on top of it — the same cycle appearing twice in /pertes and in
# the Grafana table, for ever. Rewriting cannot fix that; only deleting the old series can.
#
# It also picks up the noise-floor fix in the same pass: runs costed before it carry a sigma of
# 1 cm (40 L) taken from the fallback, which made every figure read as pure noise.
#
# Safe: pump_volume is a derived cache. pump_run and height_measure are untouched, and the sweep
# rebuilds every run from them. Safe to re-run for the same reason — worst case it rebuilds what
# it just rebuilt.
set -euo pipefail

# 90 days, comfortably longer than the pump board has been recording anything.
exec "$VENV_DIR/bin/python" "$REPO_DIR/sensors/pump_volume.py" --purge --since 90d
