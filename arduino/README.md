# Arduino XIAO SAMD21 firmware

The well-level sensor firmware is developed in a **separate PlatformIO repo**. This
folder holds the prebuilt binary that runs on the board and the tooling to flash it
onto the Pi's Arduino remotely — no SSH, no physical access.

```
arduino/
├── firmware.bin        # prebuilt image (committed; absent until the first real firmware)
├── VERSION             # version / source_commit / built — bump on every firmware change
├── flash_firmware.py   # flashes firmware.bin onto /dev/ttyACM0 via bossac
└── README.md           # this file
```

## How flashing works

The XIAO SAMD21 has an Arduino-Zero-style **SAM-BA bootloader**. `flash_firmware.py`:
1. takes the same serial-port flock as measurements (so nothing reads the sensor mid-flash),
   after stopping `sensors.timer`;
2. does a **1200-baud "touch"** on `/dev/ttyACM0`, which resets the board into its bootloader;
3. runs `bossac -p ttyACM0 --offset=0x2000 -e -w -v -R firmware.bin`
   (the app lives at `0x2000`; the bootloader below it is never overwritten);
4. reopens the port and confirms the boot banner + a real reading.

Requires `bossac` (Debian package `bossa-cli`, ≥ 1.8 for `--offset`) — installed
automatically by `deploy/bootstrap.sh`. The Pi user must be in the `dialout` group
(already the case).

## Shipping a new firmware version

1. In the PlatformIO repo: `pio run -e seeed_xiao`.
2. Copy `.pio/build/seeed_xiao/firmware.bin` into this folder as `firmware.bin`.
3. Bump `VERSION` (set `version=`, `source_commit=` to the firmware repo's short SHA, `built=`).
4. Add a run-once migration `deploy/migrations/000N_flash_arduino_v<ver>.sh` (copy the
   previous one, new number + version in the name). This is what triggers the flash on deploy.
5. Commit + push. Within ≤15 min the Pi deploys, the migration runs, and admins get a
   `FLASH OK version=…` (or a failure) message on Telegram.

You can also flash the currently-committed binary any time with the Telegram `/flash`
command (admin only) — handy for a manual retry.

## Recovery

A flash never bricks the board: the bootloader is untouched, so you can always push a
corrected `firmware.bin` and re-flash. If the board is wedged in the bootloader after a
failed flash, `/flash` again once a good binary is committed. Worst case (board
unresponsive on USB): **double-tap the XIAO's reset button** to force the bootloader,
then `/flash`.
