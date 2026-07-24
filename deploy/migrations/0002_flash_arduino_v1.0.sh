#!/usr/bin/env bash
# Run-once: flash arduino/firmware.bin (version 1.0) onto the XIAO SAMD21.
# All logic lives in the reusable flash script; --notify-admins reports the
# result on Telegram since this runs unattended during a deploy.
set -euo pipefail
exec "$VENV_DIR/bin/python" "$REPO_DIR/arduino/flash_firmware.py" --notify-admins
