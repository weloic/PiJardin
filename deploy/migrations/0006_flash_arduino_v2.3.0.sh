#!/usr/bin/env bash
# Run-once: flash arduino/firmware.bin (version 2.3.0) onto the XIAO SAMD21.
# 2.3.0 merges the two measurement commands: `sampling` is gone and `read_puit`
# now carries the per-ping arrays on every reply, which is what lets the Pi take
# one median over all 30 pings of a measurement instead of medianing the three
# burst medians.
#
# Proto stays 2, so this is not a flag day: the change only ADDED fields, the
# board still on 2.2.0 keeps answering `read_puit` correctly while this is
# pending, and sensors/read_puit.py falls back to the old median of burst medians
# (logging a warning that names this flash) until the new image is on. Ordering
# inside deploy.sh therefore costs nothing — the service restarts before this runs.
# Retried on the next deploy tick if it fails.
# All logic lives in the reusable flash script; --notify-admins reports the
# result on Telegram since this runs unattended during a deploy.
set -euo pipefail
exec "$VENV_DIR/bin/python" "$REPO_DIR/arduino/flash_firmware.py" --notify-admins
