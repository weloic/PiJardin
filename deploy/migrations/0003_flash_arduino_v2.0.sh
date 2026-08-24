#!/usr/bin/env bash
# Run-once: flash arduino/firmware.bin (version 2.0) onto the XIAO SAMD21.
# Mandatory on this branch, not just an upgrade: sensors/read_puit.py now speaks
# proto 2, so until the board carries this image every measurement fails the
# handshake with proto_mismatch. Retried on the next deploy tick if it fails.
# All logic lives in the reusable flash script; --notify-admins reports the
# result on Telegram since this runs unattended during a deploy.
set -euo pipefail
exec "$VENV_DIR/bin/python" "$REPO_DIR/arduino/flash_firmware.py" --notify-admins
