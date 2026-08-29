# PiJardin
Micro domotic project for water management, using a raspberry pi and arduino for sensors.


## Pi Initialisation
Run once on a fresh Pi to register all systemd units and start the automation:

```bash
bash ~/PiJardin/deploy/bootstrap.sh
```

After that, `deploy.timer` pulls and redeploys the repo automatically every 15 minutes, and `sensors.timer` reads sensor data every 5 minutes on the clock. `pump.service` runs continuously instead of on a timer — see "Pump on/off" for why.

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

**Transient Telegram API failures are collapsed on purpose.** A Telegram-side 502 arrives as a
burst of retries, and with no error handler registered python-telegram-bot logged each one with
a full traceback — one upstream blip produced ~200 lines and pushed everything else out of the
15-line `/logs` window, which is the only view into the Pi. `on_error` in `telegram_bot/bot.py`
now logs the first as a single WARNING and mutes the same error for 5 min, reporting
`(+N more suppressed)` when it next speaks. Errors that are *not* transient — `BadRequest`
(which subclasses `NetworkError` but means the API rejected our request), `Conflict`,
`InvalidToken`, and any bug in our own handlers — keep their full traceback.

Read logs on the Pi with `journalctl -u <unit>`, or remotely (admin only) via the Telegram
`/logs` command:

```
/logs [bot|sensors|deploy|pump] [N | 2h|30m|3d] [since <t>] [until <t>]
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
- **`height_measure` measurement** — one point per reading, timestamped at the moment it was
  taken (so an on-demand `/mesure` lands where it happened rather than on the 5-min grid), tag
  `resampled`. Field `lenght_median` is the distance in cm the Grafana dashboards read; the
  rest is health and future repair, all from the same burst: `pulse_us` (the raw echo width,
  the only thing the board actually measures) and `temp_c` (the air temperature it was
  converted with) make a later correction to the µs→cm divisor replayable over history, and
  `n`/`n_valid`/`n_timeout`/`n_rejected`/`n_no_response` say how the burst went. Graphing
  `n_valid / n` is the best early warning there is: a sensor on its way out trends down for
  weeks before it fails outright.

  **Every reading is the median of 3 bursts taken half a second apart**, and 4 more are taken
  when either the bursts disagree by more than 1 cm or the result has moved more than 5 cm
  since the last stored reading — `resampled` marks that second round, whichever triggered it.
  The spacing is the part that matters and is easy to get wrong: a burst's 10 pings are ~3 ms
  apart, so the whole burst spans about a tenth of a second and all ten pings see the *same*
  phase of whatever the surface is doing. More pings per burst therefore fight electrical noise
  and do almost nothing about ripple; separating the bursts in time is what averages over a
  moving surface. That matters because the cistern can run as a closed loop, whose returning
  flow disturbs the surface without moving the level — so the 5 cm jump test never fires, and a
  rippled surface would otherwise be read once and believed.
- **`log` measurement** — every `WARNING`+ from the scheduled `sensors.service` data path
  (`read_puit`/`alerts`), written automatically. Tags: `level`, `source`; field: `message`.
- **`version` measurement** — a marker written by `deploy.sh` on each deploy (`event=deploy`)
  and by `flash_firmware.py` on a successful flash (`event=flash`). Fields: `pi_version`
  (repo git hash), `arduino_version` (`arduino/VERSION`), `grafana_version` (grafana/ subtree
  hash). Add it as a Grafana annotation query to overlay version changes on any panel.
- **`pump_state` / `pump_run` measurements** — the pump's on/off history, written by
  `pump.service`. See "Pump on/off" below.
- **service down** — if `telegram-bot.service` or `pump.service` fails, systemd's `OnFailure`
  hook (`pijardin-onfailure@.service`) writes a `CRITICAL` `log` point (`source=<unit>`), so a
  dead service still leaves a trace even though it can't report on itself.

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

## Pump on/off

A second board (XIAO **RP2040**, USB product string `PiJardin Pump`) watches for mains AC on
the pump's switched feed with a ZMPT101B module, and reports when the pump starts and stops.
`sensors/read_pump.py` records that history, run by `pump.service`.

**It is a daemon, not a timer, and that is the design.** The well level is a value you fetch;
the pump is an event. A 5-minute poll like `sensors.timer` would quantise every transition to
5 minutes, miss any cycle shorter than that, and — the part that actually breaks it — have no
way to know what it missed. So the board watches continuously and pushes; the Pi listens.

**Who says what.** The Pi speaks exactly twice per connection and then goes quiet for weeks:

1. It resolves the board (`common/boards.py`, by USB product string) and sends one `status`.
   That answers *am I talking to the right board* (`proto` + `role`, same checks as the puit
   board) and *what is its state now*.
2. It sends `history after_seq=<last event in the DB>` and replays whatever it missed.
3. From then on it sends **nothing**. The board emits a line on every debounced state change
   and a heartbeat every 60 s; each one becomes a point.
4. It speaks again only if the board goes silent for 3 heartbeats — the sole symptom a passive
   listener has — and then reconnects with backoff.

Opening the port does not disturb the board: DTR is asserted the way a terminal would, but on
both PiJardin boards a reset needs the 1200-baud touch, not a DTR edge. That is what makes a
restart of this service cheap — the board keeps counting, and `since_ms` + the history buffer
let the Pi recover the gap exactly.

**Three measurements**, all tagged `pump`:

- **`pump_state`** — one point per transition *and* per heartbeat. Field `state` is an integer:
  `1` on, `0` off, `-1` sensor fault, `-2` unknown. Filter `state >= 0` for the pump trace,
  `state < 0` for the anomalies. Tag `source` says how the point was learned:
  `event` (live), `heartbeat`, `replay` (drained from the board's ring buffer after a
  reconnect), `resync` (back-dated from `since_ms`) or `reboot` (a gap marker). Also carries
  `rms_counts`, `freq_hz`, `asym` and the board's own counters, plus `seq_missed` when lines
  were lost.
  **The heartbeat points are not filler**: without them "pump off for six hours" and "board
  dead for six hours" are the same picture, and telling those apart is the point.
- **`pump_run`** — one point per state that *ended*, timestamped at its **start**, with
  `duration_s`. Runtime per day is a `sum`, cycles per day a `count` — no integrating a step
  series. Tag `state` is the state that ended; tag `truncated` is `true` when the daemon or
  the board was away for part of it, so the value is a **lower bound**. Never treat a
  truncated run as an exact duration.

  `duration_s` is derived, not read: on a live event the board sends two **timestamps**
  (`ms`, when the transition was declared, and `prev_ms`, when the state being left began) and
  the Pi subtracts them — modular arithmetic, because the board's `millis()` wraps at ~49.7
  days. Replayed events carry no `prev_ms` at all (the ring buffer holds only
  `{seq, state, ms}`), so there the duration comes from chaining consecutive transition times.
  Every transition also cross-checks the board's duration against the interval this service
  watched pass, and logs a WARNING if they disagree — reading `prev_ms` as a duration once
  produced runs 4.5× too long that looked entirely plausible next to a correct state trace.
- **`pump_volume`** — one point per finished `on` run, timestamped at the same **start** as the
  `pump_run` it describes, with `volume_l`: how much water actually left the cistern. See
  "How much water a run used" below.

**The cursor is durable.** `sensors/.pump_state.json` records the last event InfluxDB actually
*accepted*, not the last line the serial port delivered, so a crash in between replays that
line instead of losing it. Combined with the board's 32-entry history buffer, a Pi reboot or a
deploy restart costs nothing — points land a minute late, timestamped correctly. The two
things that cannot be recovered are always marked rather than guessed: a **board** reboot
(its buffer went with it → an `unknown` gap marker + `truncated` runs) and a buffer that
wrapped while the Pi was down longer than 32 transitions.

Check it without SSH: `/pompe` on the Telegram bot (reads the state file, never the port —
the daemon holds that open for its whole life), `/boards` for USB-level presence, and
`/logs pump` for the journal.

## How much water a run used

`sensors/pump_volume.py` turns each finished pump run into a volume in litres and records it as
`pump_volume`. The Grafana table **« Eau utilisée / perdue — 3 derniers cycles de pompage »** and
the Telegram command **`/pertes`** are the two views of it.

**There is no timer.** `read_pump.py` calls the sweep as soon as it has written the level reading
forced at a pump stop, so a run is costed within seconds of ending. What makes that safe is that
the sweep asks the database *"which runs have no volume yet?"* rather than *"cost the run that
just ended"*. A calculation that never happened — `deploy.timer` restarted `pump.service` in the
middle, the Pi lost power, InfluxDB was down — leaves the run simply unanswered, and the next
pump cycle picks it up. The runs replayed out of the board's history buffer after any downtime
arrive in a batch and are handled the same way, with no special case. The one gap is that nothing
runs while the pump is idle; `/pertes` sweeps before it reads, which closes it on demand.

**It is measured, never multiplied by a flow rate — that is the whole point.** The cistern feeds
either irrigation, where the water leaves and does not come back, or a **closed loop**, where it
circulates and returns minus whatever the loop leaks or sprays away. Those two produce the same
pump run: same duration, same current, nothing the pump board can tell apart. `duration_s × a
constant` would report the same volume for both and be confidently wrong half the time. The well
level is what separates them — and in the closed-loop case the number it gives *is* the loop's
losses, which is what this exists to catch.

**How.**

```
volume = (level at the start − level at the end) × 40 L/cm   +   whatever rain put back
```

Each of those two levels is **the reading `read_pump.py` forces at the transition** — the single
measurement taken at exactly the instant that matters. There is no curve fitting: the reading is
used as it stands.

Unless the routine readings either side of it say it cannot be right. The check is a **bracket**:
between the routine reading before the transition and the one after it, the level has genuinely
moved — by a lot on a fast run — so any value inside the range those two span, plus 2 cm, is
plausible. Only something outside it is evidence of a bad reading, and that is what catches a
stray echo or a surface the returning loop has stirred into ripples. A rejected reading is
replaced by the neighbours, interpolated to the transition time, and the run is marked `coarse`.
That fallback is noticeably worse — it draws a straight line across the very instant the level
changed direction, and can be 100 L out on a 600 L run — which is why `volume_sigma_l` widens to
the full neighbour spread when it happens.

For a 15-minute run starting at 14:07:20, the start level is checked against the 14:05 and 14:10
readings, and the end level against 14:20 and 14:25.

The forced readings are what make short runs measurable at all: `sensors.timer` samples every 5
minutes, so without them a boundary can be five minutes stale — half of a ten-minute run. They
land in `height_measure` like any other reading, and get the same multi-burst treatment (see
above), which is what keeps a loop-disturbed surface from deciding a volume. They are taken on a
**separate thread** so waiting for the puit flock can never delay the pump listener, and with the
volume alerts suppressed — a reading taken the instant the pump stops is the lowest of the whole
cycle, and feeding those to the thresholds would fire "volume bas" every time.

**The rain term is almost always zero.** The cistern gains water when it rains, and the pump is
rarely running then, so this reads 0 and disappears. It is there for when the two coincide,
because that is exactly where it flips the sign of the answer: a closed-loop cycle that loses 10 L
while 45 L of rain falls in ends with *more* water than it started with, and would otherwise be
reported as −35 L instead of +10 L. The rate is measured per run from the quiet stretch before it
(first reading against last, nothing fitted), and only if that stretch is at least 10 minutes long.

**Every point says how far to trust it.** `volume_sigma_l` is the uncertainty — the level moves
40 litres per centimetre, so a small enough loss is indistinguishable from sensor noise, and this
says so out loud instead of printing a confident number. Tag `quality` is `ok` (both forced
readings used and vouched for by their neighbours), `coarse` (one was rejected, absent, or had no
neighbours to check it against), or `degraded` (a `truncated` run, whose duration is itself only
a lower bound).

**It is a cache, not a record.** Every input stays in InfluxDB, so the estimator can be changed
and the whole history rebuilt — which it will need to be, since several constants in there are
first guesses:

```
python sensors/pump_volume.py                            # sweep the last 24 h
python sensors/pump_volume.py --since 90d --recompute    # rebuild everything
```

(`--since` takes `24h`, `7d`, `90d`. Not `-90d` with a space — argparse reads a leading dash as
an option; `--since=-90d` works if you are copying a Flux range literal.)

**What it cannot do.** At 40 litres per centimetre, a small enough loss is lost in the sensor's
own wobble — `volume_sigma_l` is what admits that. And if rain starts or stops *during* a run,
the rate measured from the quiet stretch before it is the wrong one and nothing here can tell.
An inline flow meter is the only thing that removes either.

⚠️ **Flashing this board is not `/flash`.** It is an RP2040: its bootloader exposes no SAM-BA
interface, only the `RPI-RP2` mass-storage drive, so `bossac` cannot touch it. `flash_firmware.py`
refuses it explicitly (exit 7) rather than writing a SAMD21 image onto it. Flash it from the
firmware repo with `tools/flash_uf2.py`. Anything that needs the port must
`systemctl stop pump.service` first — a flock cannot wait out a holder that never lets go.

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

**The handshake is a `status` request, not the boot banner.** Opening the port does not reset the
board — DTR auto-reset needs a capacitor to the RESET pin, which only bridge-chip boards have,
while the XIAO speaks USB natively (on SAMD, 1200 baud is the reset convention, which is how the
flasher enters the bootloader). So the board runs uninterrupted across opens and only banners
after a real reboot; waiting for one previously cost a 10 s timeout on every open. `status`
answers on demand with `proto`, `role`, `fw` and `uptime_ms` — that last one being the only way
to notice a board that restarted on its own between measurements.

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
firmware has hung. If nothing advertises the expected string, resolution **fails** rather than
guessing at `/dev/ttyACM0` — a guess that would open the wrong board once a second one exists.
The registry supports a per-board `fallback` node for rolling back to a firmware without a
product string; none is set, and it should stay that way outside such a rollback.

`bossac` (`bossa-cli`) is installed automatically by `deploy/bootstrap.sh`. Flashing
stops `sensors.timer` and takes the serial-port lock for ~1 min, then restarts it; the
bootloader is never overwritten, so a bad flash is always recoverable by pushing a
corrected binary. If passwordless `sudo` is ever restricted on the Pi, the flash needs:
```
pi ALL=(root) NOPASSWD: /usr/bin/systemctl stop sensors.timer, /usr/bin/systemctl start sensors.timer
```