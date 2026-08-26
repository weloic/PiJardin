# PiJardin
Micro domotic project for water management, using a raspberry pi and arduino for sensors.


## Pi Initialisation
Run once on a fresh Pi to register all systemd units and start the automation:

```bash
bash ~/PiJardin/deploy/bootstrap.sh
```

After that, `deploy.timer` pulls and redeploys the repo automatically every 15 minutes, and `sensors.timer` reads sensor data every 5 minutes on the clock.

## Manual testing
You can force data collection with:
```
systemctl start sensors.service
```

And visualize output using:
```
journalctl -u sensors.service -n 30
```

## Logging

All Python components log through the standard `logging` module (configured once in
`common/logging_setup.py`) to **stdout → journald** — no log files, no rotation; systemd's
journal owns retention. Lines are formatted `LEVEL [module] message`; journald adds the
timestamp. Set `LOG_LEVEL` in `.env` (default `INFO`) to change verbosity without a code change.

Read logs on the Pi with `journalctl -u <unit>`, or remotely (admin only) via the Telegram
`/logs` command:

```
/logs [bot|sensors|deploy] [N | 2h|30m|3d] [since <t>] [until <t>]
```

- No argument → the last 15 lines of `telegram-bot.service` (a compact message).
- `N` → last N lines (no upper cap). `2h`/`30m`/`3d` → a relative window (last 2 hours, etc.).
- `since <t>` / `until <t>` → an explicit range; `<t>` is a single token: `HH:MM`,
  `YYYY-MM-DD`, `today`/`yesterday`, or a duration like `2h` (meaning "ago").

Output is **redacted** of known secrets (bot/InfluxDB tokens) before sending. A compact result
(≤ 15 lines) is rendered as a monospace message; anything larger is sent as a `.txt` file
attachment (no truncation).

**InfluxDB records the measurements plus only serious events** (the DB is for managing the
project; the Telegram bot is an add-on and never writes to it):
- **`height_measure` measurement** — one point per reading, timestamp rounded to 5 min, tag
  `resampled`. Field `lenght_median` is the distance in cm the Grafana dashboards read; the
  rest is health and future repair, all from the same burst: `pulse_us` (the raw echo width,
  the only thing the board actually measures) and `temp_c` (the air temperature it was
  converted with) make a later correction to the µs→cm divisor replayable over history, and
  `n`/`n_valid`/`n_timeout`/`n_rejected`/`n_no_response` say how the burst went. Graphing
  `n_valid / n` is the best early warning there is: a sensor on its way out trends down for
  weeks before it fails outright.
- **`log` measurement** — every `WARNING`+ from the scheduled `sensors.service` data path
  (`read_puit`/`alerts`), written automatically. Tags: `level`, `source`; field: `message`.
- **`version` measurement** — a marker written by `deploy.sh` on each deploy (`event=deploy`)
  and by `flash_firmware.py` on a successful flash (`event=flash`). Fields: `pi_version`
  (repo git hash), `arduino_version` (`arduino/VERSION`), `grafana_version` (grafana/ subtree
  hash). Add it as a Grafana annotation query to overlay version changes on any panel.
- **service down** — if `telegram-bot.service` fails, systemd's `OnFailure` hook
  (`pijardin-onfailure@.service`) writes a `CRITICAL` `log` point (`source=<unit>`), so a dead
  bot still leaves a trace even though it can't report on itself.

## Telegram bot deploy
Once .env is set, run following to deploy:
```
sudo systemctl restart telegram-bot.service
```

Users register themselves by messaging the bot `/start <password>`. The password determines their role:
- `TELEGRAM_PASSWORD_ADMIN` in `.env` — registers as `admin`
- `TELEGRAM_PASSWORD_VIEWER` in `.env` — registers as `viewer`

Registered chat IDs and roles are stored in `telegram_bot/.telegram_users.json` (gitignored, not committed).

Alert delivery is independent of role and off by default. Once registered, a user toggles it with `/alertes on` or `/alertes off` (or `/alertes` with no argument to check the current setting).

## Low-volume alerts
After each measurement (every 5 minutes, and on `/mesure`), the volume is checked against thresholds defined in `telegram_bot/alerts.py`:
- **3 m³** ("volume bas") — opted-in users are alerted once when the volume drops below.
- **0.5 m³** ("volume critique") — alerted on crossing, then reminded every day at 18:00 while the level stays below.

A recovery message is sent when the volume rises back above a threshold (with a 0.2 m³ hysteresis margin to avoid flapping). Alert state is kept in `telegram_bot/.alert_state.json` (gitignored).

To test alert delivery on the Pi without touching the sensor:
```
python telegram_bot/alerts.py 2.8   # fake a 2.8 m³ measurement
```

## One-time tasks (migrations)

Some deploys need an action to run **exactly once** on the Pi (a data backfill, an
InfluxDB schema change, flashing Arduino firmware) rather than every time like a
service restart. Drop a numbered script in `deploy/migrations/`; `deploy.sh` runs each
one once and records it in the gitignored ledger `deploy/.migrations_applied`, so
`git reset --hard` never re-runs it. See `deploy/migrations/README.md` for the
convention.

## Arduino firmware

The XIAO SAMD21 sensor firmware is built in a separate PlatformIO repo; the prebuilt
`arduino/firmware.bin` is committed here and flashed onto the board **remotely** (no
SSH) via `bossac`. Push a new binary + a `deploy/migrations/000N_flash_arduino_*.sh`
migration to flash on deploy, or run `/flash` (admin only) on the Telegram bot to
reflash the committed binary. Full workflow and recovery steps: `arduino/README.md`.

**The Pi and the board share a versioned serial contract** — newline-delimited JSON over the
board's USB serial port, one object per line each way, `proto = 2`. `sensors/read_puit.py` sends
`{"id":n,"cmd":"read_puit"}` and matches the reply by its echoed `id`; the board answers
with the distance plus the four ping counts, or with an explicit error `code` instead of a
silent `0`. The full contract (commands, parameters, every error code) lives in the
firmware repo's README — [PiJardin-Arduino_Software](https://github.com/weloic/PiJardin-Arduino_Software).
`PROTO` in `read_puit.py` must match the flashed firmware: every line the board emits carries
its `proto`, so a mismatch fails the handshake with `proto_mismatch` rather than recording
numbers from a different contract. **Fix it by flashing (`/flash`), not by editing `PROTO`.**

The error codes decide what happens next, which is the point of having them: `echo_timeout`
and `insufficient_samples` are retried (ripples, an oblique surface); `sensor_fault` (the
sensor ignores the trigger — power or wiring) and `out_of_range` (it answers but is misaimed
or obstructed) are **not** retried and notify the admins on Telegram, throttled to once per
6 h per code since the scheduled run fires every 5 minutes; anything else is a bug in the
request the Pi sent. Admins can see one burst ping-by-ping with `/echantillons`.

**Which board is on which port is never assumed.** `/dev/ttyACM*` numbering is enumeration
order, not identity, so the device node is resolved at open time from the USB product string
the *firmware* advertises (`PiJardin Puit`), via the registry in `common/boards.py`. Identity
therefore lives in the firmware image, not the hardware: swap the board for any other one,
flash the same image, and it resolves identically — nothing on the Pi needs editing. The boot
banner's `role` field is then checked against what the code expects, which is what catches a
board carrying the wrong image (`wrong_board`). `/boards` (admin) shows how each board resolves; it
never opens a port, so it is safe during a measurement and still reports a board whose
firmware has hung. Until a board is reflashed with a descriptor-carrying image, resolution
falls back to `/dev/ttyACM0` and logs a warning.

`bossac` (`bossa-cli`) is installed automatically by `deploy/bootstrap.sh`. Flashing
stops `sensors.timer` and takes the serial-port lock for ~1 min, then restarts it; the
bootloader is never overwritten, so a bad flash is always recoverable by pushing a
corrected binary. If passwordless `sudo` is ever restricted on the Pi, the flash needs:
```
pi ALL=(root) NOPASSWD: /usr/bin/systemctl stop sensors.timer, /usr/bin/systemctl start sensors.timer
```