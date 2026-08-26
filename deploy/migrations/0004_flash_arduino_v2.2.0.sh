#!/usr/bin/env bash
# Run-once: flash arduino/firmware.bin (version 2.2.0) onto the XIAO SAMD21.
# Unlike 0003 this is an upgrade, not a rescue: proto stays 2, so the board still
# on 2.1.0 keeps answering sensors/read_puit.py correctly and measurements never
# break while this is pending. What 2.2.0 adds is a `"role":"puit"` field on the
# boot banner and the status response, plus a `PiJardin Puit` USB product string
# — board identity, which starts to matter now that a second XIAO (the RP2040
# pump board) can share the bus. Retried on the next deploy tick if it fails.
# All logic lives in the reusable flash script; --notify-admins reports the
# result on Telegram since this runs unattended during a deploy.
set -euo pipefail
exec "$VENV_DIR/bin/python" "$REPO_DIR/arduino/flash_firmware.py" --notify-admins
