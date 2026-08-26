# Arduino XIAO SAMD21 firmware

The well-level sensor firmware is developed in a **separate PlatformIO repo**. This
folder holds the prebuilt binary that runs on the board and the tooling to flash it
onto the Pi's Arduino remotely — no SSH, no physical access.

```
arduino/
├── firmware.bin        # prebuilt image (committed; absent until the first real firmware)
├── firmware_v1.bin     # previous image, kept for a manual rollback (proto 1 — needs the
│                       #   matching read_puit.py, so it is not a drop-in downgrade)
├── VERSION             # version / source_commit / built — bump on every firmware change
├── flash_firmware.py   # flashes firmware.bin onto the resolved board port via bossac
└── README.md           # this file
```

## How flashing works

The XIAO SAMD21 has an Arduino-Zero-style **SAM-BA bootloader**. `flash_firmware.py`:
1. **resolves which port the board is on** (`common/boards.py`, by the USB product string the
   firmware advertises) — this happens *first*, because the SAM-BA bootloader has its own USB
   stack and descriptor, so once the board is in the bootloader it can no longer be found by
   what its firmware says. Then takes the same serial-port flock as measurements (so nothing
   reads the sensor mid-flash), after stopping `sensors.timer`;
2. does a **1200-baud "touch"** on that port, which resets the board into its bootloader;
3. runs `bossac -p ttyACM0 --offset=0x2000 -e -w -v -R firmware.bin`
   (the app lives at `0x2000`; the bootloader below it is never overwritten);
4. reopens the port and confirms the boot banner + a real reading. Resolution is redone here
   deliberately — the image just written may advertise a product string the old one did not.
   The banner carries the protocol version and the board's own role, so an image that does not
   match what `sensors/read_puit.py` speaks, or that belongs to a *different* board, fails
   verification here instead of quietly recording numbers from a different contract.

Requires `bossac` (Debian package `bossa-cli`, ≥ 1.8 for `--offset`) — installed
automatically by `deploy/bootstrap.sh`. The Pi user must be in the `dialout` group
(already the case).

`--port /dev/ttyACM0` bypasses resolution, for a board too bricked to advertise itself (its
descriptor comes from the firmware, so a half-written image has none). Use `/boards` to see
what the Pi can currently find.

## Shipping a new firmware version

Every image must declare which board it is, in two places that have to agree:

```ini
board_build.usb_product = PiJardin Puit          # USB descriptor -> how the Pi finds the port
build_flags = -DPIJARDIN_ROLE=\"puit\"           # banner "role" field -> what the Pi verifies
```

The first is read by the Arduino core and becomes the USB product string; the second is read by
the sketch and goes into the ready banner. **`usb_product` must match `common/boards.py` byte
for byte** — it is compared with `==`, so a differing space or case just misses and the Pi
silently falls back to `/dev/ttyACM0`. `/boards` makes that obvious: it lists every other port
it found, so a near-miss shows up beside the expected string.

Keep the two adjacent — the realistic way they diverge is copying an env block for a second
board and updating only one, which produces a board advertising one role while reporting
another. That is what the Pi's `wrong_board` check exists to catch.

Note the vocabulary: the firmware's banner field is `role`, while the Pi side calls the concept
`board` (`common/boards.py`, `BOARD` in `read_puit.py`) because `role` already means the
admin/viewer permission level in `telegram_bot/`. Both are set from `PIJARDIN_ROLE`.

Confirm the descriptor reached the compiler with `pio run -v | grep USB_PRODUCT`;
`board_build.usb_product` is silently ignored by older `platform-atmelsam` versions.

1. In the PlatformIO repo: `pio run -e seeed_xiao`.
2. Copy `.pio/build/seeed_xiao/firmware.bin` into this folder as `firmware.bin`.
3. Bump `VERSION` (set `version=`, `source_commit=` to the firmware repo's short SHA, `built=`).
4. Add a run-once migration `deploy/migrations/000N_flash_arduino_v<ver>.sh` (copy the
   previous one, new number + version in the name). This is what triggers the flash on deploy.
5. **If the serial contract changed**, update `sensors/read_puit.py` in the *same* push —
   `PROTO` and the request/response handling. The two sides are versioned together: deploying
   a Pi that speaks a different `proto` than the flashed board stops measurements dead (the
   handshake fails with `proto_mismatch`) until one of them catches up. Ordering within the
   deploy is not a worry — the migration flashes the board before the next scheduled run.
6. Commit + push. Within ≤15 min the Pi deploys, the migration runs, and admins get a
   `FLASH OK version=…` (or a failure) message on Telegram.

You can also flash the currently-committed binary any time with the Telegram `/flash`
command (admin only) — handy for a manual retry.

## Recovery

A flash never bricks the board: the bootloader is untouched, so you can always push a
corrected `firmware.bin` and re-flash. If the board is wedged in the bootloader after a
failed flash, `/flash` again once a good binary is committed. Worst case (board
unresponsive on USB): **double-tap the XIAO's reset button** to force the bootloader,
then `/flash`.
