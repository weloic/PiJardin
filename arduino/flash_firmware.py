#!/usr/bin/env python3
"""Flash the Arduino XIAO SAMD21 with arduino/firmware.bin, over its USB serial port.

Reuses the Pi-side serial machinery in sensors/read_puit.py: the same cross-process
flock (so a scheduled measurement or a /mesure never fights the flash for the port),
the same board-reset/boot-banner logic for post-flash verification.

Procedure:
  1. Refuse to run if there is no firmware to flash (fail fast, no side effects).
  2. Stop sensors.timer, then take the serial-port flock — nothing else touches the
     port while we flash. Both are always undone on exit.
  3. 1200-baud "touch" to drop the SAMD21 into its SAM-BA bootloader.
  4. bossac writes the image at offset 0x2000 (above the bootloader) and resets.
  5. Verify: reopen at the normal baud, expect the boot banner and a real reading.

Exit codes: 0 OK or WARN (bossac verified the write; readback is only an extra
health check, so a WARN still exits 0 to avoid re-flashing an already-verified
binary), 1 no firmware, 2 no bootloader port, 3 bossac failed, 5 serial port busy,
6 board not found on the USB bus, 7 the target board is not flashed with bossac.
Every path prints one final line starting "FLASH OK|WARN|FAIL:" for the bot /
journal to relay.

Usage:  python flash_firmware.py [--notify-admins] [--port /dev/ttyACM0]

--port bypasses board resolution, for a board too bricked to advertise itself (its
descriptor comes from the firmware, so an erased or half-written image has none).
"""
import os
import sys
import time
import logging
import subprocess

import serial

# read_puit.py lives in sensors/, alerts.py in telegram_bot/, the `common` package at the repo
# root; when run as a script only arduino/ is on sys.path (same pattern as bot.py / read_puit.py).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, 'sensors'))
sys.path.insert(0, os.path.join(_REPO, 'telegram_bot'))

import read_puit
import alerts
from common import boards
from common.logging_setup import setup_logging

log = logging.getLogger('flash_firmware')

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

_HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_BIN = os.path.join(_HERE, 'firmware.bin')
VERSION_FILE = os.path.join(_HERE, 'VERSION')

## Which board this flasher targets. The device node is resolved from the registry at flash
## time rather than being a constant — and the flash parameters come from the same entry, so a
## board on a different chip family can bring its own tool without a special case here.
BOARD = read_puit.BOARD
LOCK_TIMEOUT_S = 90                   # a measurement in flight finishes well under this
REENUM_TIMEOUT_S = 15                 # USB re-enumeration after a reset

# -------------------------------------------------------------------------------------------------
# HELPERS

def read_version():
    """Return the `version=` value from arduino/VERSION, or 'unknown' if absent."""
    try:
        with open(VERSION_FILE) as f:
            for line in f:
                if line.startswith('version='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return 'unknown'

def systemctl(action, unit):
    """Best-effort `sudo systemctl <action> <unit>`; never raises."""
    try:
        subprocess.run(['sudo', 'systemctl', action, unit], check=False, timeout=30)
    except Exception as e:
        log.warning(f"systemctl {action} {unit} failed: {e}")

def enter_bootloader(port):
    """1200-baud open/close 'touch' — resets a SAMD21 into its SAM-BA bootloader.

    A failure here is non-fatal: the board may already be sitting in the bootloader
    from a previous interrupted flash, in which case the port check afterwards still
    passes and we go straight to bossac. This is what makes the script re-runnable.
    """
    baud = boards.config(BOARD)['flash']['touch_baud']
    try:
        s = serial.Serial(port, baud)
        s.dtr = False
        time.sleep(0.1)
        s.close()
        log.info(f"{baud}-baud touch sent on {port}; board should reset into the bootloader.")
    except (serial.SerialException, OSError) as e:
        log.warning(f"{baud}-baud touch could not open {port} ({e}); "
                    "assuming the board may already be in the bootloader.")

def wait_for_port(port, timeout):
    """Wait for the serial device node to (re)appear after a reset. No open/DTR toggle
    — that could disturb the bootloader; bossac and open_arduino() handle the port.

    Waits on the node, not on the board's identity: the SAM-BA bootloader has its own USB
    stack and descriptor, so the product string the firmware advertises is absent for as
    long as the flash lasts. (With a second board attached the bootloader could also come
    back on a different node than the application had; following it would mean diffing the
    port list across the touch. Not needed while one board is attached.)
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(port):
            return True
        time.sleep(0.3)
    return False

def run_bossac(port):
    """Upload FIRMWARE_BIN with bossac. Returns (ok, combined_output)."""
    # bossac -p wants a bare device name, not a path; realpath first so a symlinked
    # node (passed via --port) still resolves to the real ttyACM* it points at.
    cmd = ['bossac', '-p', os.path.basename(os.path.realpath(port)), '-i',
           f"--offset={boards.config(BOARD)['flash']['offset']}", '-e', '-w', '-v', '-R',
           FIRMWARE_BIN]
    log.info(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, "bossac not found — install bossa-cli (deploy/bootstrap.sh does this)."
    except subprocess.TimeoutExpired:
        return False, "bossac timed out after 120 s."
    out = (result.stdout + result.stderr).strip()
    log.info(out)
    return result.returncode == 0, out

def verify_firmware(port=None, attempts=3, settle=2.0):
    """Reopen the port after the flash and read once. Returns the reading (cm) or None.

    The `bossac -R` reset re-enumerates the USB device: for a second or two the node
    can be absent, or present but still tearing down (open → Errno 32 Broken pipe).
    So retry a few times with a settle delay before giving up. Reuses open_arduino()
    (DTR reset + waits for the JSON ready banner, which also checks `proto` and that the
    board reports the role we expect) and get_sensor_data(); a reading back means the image
    we just wrote speaks the protocol this Pi expects — a mismatched firmware fails the
    handshake instead.

    port=None re-resolves through the registry, which is the normal path and the point: the
    image just written may advertise a USB product string the previous one did not, so the
    node is looked up afresh rather than assumed. A caller that forced --port keeps it."""
    for attempt in range(1, attempts + 1):
        time.sleep(settle)
        try:
            arduino = read_puit.open_arduino(port)
        except boards.BoardError as e:
            log.info(f"Verify attempt {attempt}/{attempts}: board not resolvable yet: {e}")
            continue
        except (serial.SerialException, OSError) as e:
            log.warning(f"Verify attempt {attempt}/{attempts}: could not open the board: {e}")
            continue
        except read_puit.PuitError as e:
            # Wrong or stale firmware, or a board that never came back up: report it as a
            # failed verification rather than a traceback out of the flash script.
            log.warning(f"Verify attempt {attempt}/{attempts}: no valid handshake: {e}")
            continue
        try:
            reading = read_puit.get_sensor_data(arduino, retries=3)
        finally:
            arduino.close()
        if reading is not None:
            return reading
        log.info(f"Verify attempt {attempt}/{attempts}: opened port but got no reading.")
    return None

# -------------------------------------------------------------------------------------------------
# FLASH

def _flash_locked(port, forced_port):
    """Run steps 3–5 with the flock already held. Returns (exit_code, message).

    port is the node to flash, already resolved. forced_port is the --port override or None,
    and is what verification gets: with no override, verification re-resolves deliberately,
    because the image just written may advertise a product string the old one did not.
    """
    enter_bootloader(port)
    time.sleep(1.5)
    if not wait_for_port(port, REENUM_TIMEOUT_S):
        return 2, ("FLASH FAIL: port did not re-enumerate after the bootloader touch — "
                   "power-cycle the XIAO or double-tap its reset button, then /flash again.")

    ok, out = run_bossac(port)
    if not ok:
        tail = out[-1500:]
        return 3, ("FLASH FAIL: bossac error — the board is probably still in the bootloader "
                   "(old firmware erased). Measurements fail until a flash succeeds; safe to "
                   f"re-run /flash.\n{tail}")

    # bossac's own "-v" already verified the written bytes, so the flash itself
    # succeeded here. The readback below is an extra health check; if it can't get a
    # reading we WARN but still exit 0 (success) — re-flashing the identical, already
    # verified binary on the next deploy would only loop, and a genuinely misbuilt
    # image is fixed by pushing a new binary, not by retrying this one.
    version = read_version()
    reading = verify_firmware(forced_port)
    if reading is None:
        return 0, (f"FLASH WARN: firmware version={version} written and verified by bossac, but "
                   "the post-flash readback got no reading (board may still have been settling). "
                   "sensors.timer restarted — confirm with /mesure.")
    return 0, f"FLASH OK version={version}: sensor answered {reading:.2f} cm."

def _do_flash(port, forced_port):
    """Acquire the serial-port flock, then flash. Returns (exit_code, message)."""
    try:
        with read_puit.puit_lock(LOCK_TIMEOUT_S):
            return _flash_locked(port, forced_port)
    except TimeoutError:
        return 5, "FLASH FAIL: a measurement is holding the serial port; try /flash again shortly."

def main(notify_admins=False, port=None):
    setup_logging()

    # 0. This flasher drives bossac and nothing else. Guarded rather than assumed, because the
    #    registry now holds a board it cannot flash: the pump board is an RP2040 whose
    #    bootloader exposes no SAM-BA interface, only the RPI-RP2 mass-storage drive. Asking
    #    bossac to write a SAMD21 image at offset 0x2000 of an RP2040 would be a confident,
    #    silent mistake, and BOARD is one edit away from pointing at it.
    tool = boards.config(BOARD).get('flash', {}).get('tool')
    if tool != 'bossac':
        return _finish(7, f"FLASH FAIL: the {BOARD!r} board is flashed with {tool!r}, not "
                          f"bossac — this script only speaks bossac. Flash it from the "
                          f"firmware repo instead (tools/flash_uf2.py).", notify_admins)

    # 1. Fail fast before any side effect if there is nothing to flash.
    if not os.path.isfile(FIRMWARE_BIN) or os.path.getsize(FIRMWARE_BIN) == 0:
        return _finish(1, f"FLASH FAIL: no firmware.bin at {FIRMWARE_BIN} (nothing to flash).",
                       notify_admins)

    # 2. Locate the board *before* the touch: once it is in the bootloader its descriptor is
    #    the bootloader's, so it can no longer be found by the string its firmware advertises.
    forced_port = port
    if port is None:
        try:
            port = boards.resolve(BOARD)
        except boards.BoardError as e:
            return _finish(6, f"FLASH FAIL: {e} Pass --port to flash it anyway.", notify_admins)
    log.info(f"Flashing the {BOARD} board on {port}.")

    # 3. Stop scheduled runs for the duration; always restart them, even on crash.
    systemctl('stop', 'sensors.timer')
    try:
        code, message = _do_flash(port, forced_port)
    finally:
        systemctl('start', 'sensors.timer')

    # A successful flash changes the Arduino firmware version — record a marker so Grafana
    # can annotate it (best-effort; never let a DB hiccup fail the flash).
    if code == 0:
        try:
            read_puit.write_influx_version('flash')
        except Exception as e:
            log.warning(f"Could not record flash version marker: {e}")

    return _finish(code, message, notify_admins)

def _finish(code, message, notify_admins):
    # This is the machine-readable relay contract (FLASH OK|WARN|FAIL:) the bot/journal
    # parse — keep it a bare print, not a log line with a level prefix.
    print(message)
    if notify_admins:
        try:
            alerts.send_telegram(alerts.admin_recipients(), message)
        except Exception as e:
            log.warning(f"Could not notify admins: {e}")
    return code

if __name__ == '__main__':
    _args = sys.argv[1:]
    _port = None
    if '--port' in _args:
        _i = _args.index('--port')
        if _i + 1 >= len(_args):
            print("FLASH FAIL: --port needs a device node, e.g. --port /dev/ttyACM0")
            sys.exit(1)
        _port = _args[_i + 1]
    sys.exit(main(notify_admins='--notify-admins' in _args, port=_port))
