###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import time
import datetime
import serial
import os
import sys
import json
import logging
import itertools
import contextlib
from numpy import median

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

## Repo root (for the `common` package) and telegram_bot/ (for alerts) on sys.path; when run
## as a script, only sensors/ is on it.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, 'telegram_bot'))
import alerts
from common import boards
from common.logging_setup import (
    setup_logging, attach_influx_handler, pi_version, arduino_version, grafana_version,
)

log = logging.getLogger('read_puit')

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Influx database
INFLUXDB_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG    = os.getenv("INFLUX_ORG",    "PiJardin")
INFLUXDB_BUCKET = os.getenv("INFLUX_BUCKET", "puit")

# Client construction is lazy (no network); on failure keep going without a write API
# so importing this module never kills the caller (e.g. the Telegram bot).
try:
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    query_api = client.query_api()
except Exception as e:
    log.warning(f"Could not initialize InfluxDB client: {e}; measurements will not be recorded.")
    write_api = None
    query_api = None

## Serial communication — which board this module drives. The device node is not a constant:
## /dev/ttyACM* numbering is enumeration order, so it is resolved at open time from the USB
## product string the firmware advertises (see common/boards.py), and the baud rate comes from
## the same registry entry.
BOARD = 'puit'

## Firmware serial contract — newline-delimited JSON, one object per line each way. The
## authority is the README of the firmware repo (PiJardin-Arduino_Software); the constants
## below only size our own timeouts and guard our own requests. Everything else is
## discovered from the board: it echoes the effective value of every parameter it used.
PROTO = 2                       # must match the firmware; bumped only by a breaking change
LINE_MAX = 192                  # longest request line the board accepts, bytes
MAX_N = 25                      # most pings per burst; the board clamps anything above it
N_DEFAULT = 10                  # pings per burst, board-side default
TIMEOUT_DEFAULT_US = 45000      # per-ping echo timeout, board-side default
ACK_TIMEOUT_DEFAULT_US = 50000  # per-ping wait for echo to *rise*, board-side default
PING_GAP_S = 0.003              # the firmware's delay(3) between pings

## ack_max_us is the module's measured trigger->rise latency, and ack_timeout_us the deadline
## it is judged against — the one bound in the measurement path with no physical ground truth
## behind it. Warn once the former reaches this fraction of the latter: a module getting
## slower crosses the deadline eventually, and past that point the firmware reports perfectly
## healthy hardware as a dead sensor. (Measured on the fitted module: 12.3 ms against a
## 50 ms deadline, i.e. 25%.)
ACK_MARGIN_WARN = 0.5

## A physical sensor fault does not clear on its own and is not retried, but the scheduled
## run fires every 5 minutes — throttle the admin notification per error code.
FAULT_ALERT_INTERVAL_S = 6 * 3600

## Well geometry: the sensor measures the distance (cm) down to the water surface.
## Same conversion as the Grafana dashboards: volume_m3 = (220 - distance_cm) * 0.04
PUIT_EMPTY_DISTANCE_CM = 220.0
PUIT_M3_PER_CM = 0.04

## Store last measurements
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.puit_state.json')

## Lock file serializing access to the serial port across processes
## (scheduled sensors.service run vs. /mesure from the Telegram bot)
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.puit.lock')

# -------------------------------------------------------------------------------------------------
# FUNCTIONS
def height_to_volume(height_cm):
    """Convert the measured distance to the water surface (cm) into a volume (m³)."""
    return (PUIT_EMPTY_DISTANCE_CM - height_cm) * PUIT_M3_PER_CM

def query_volume_history(range_start):
    """Read the well's volume history from InfluxDB for the given window.

    range_start: a Flux duration literal like '-24h', '-3d', '-7d'. This is a
    fixed, caller-controlled constant (never user input), so it is safe to embed.

    Returns (times, volumes_m3): two parallel lists sorted by time, where times
    are timezone-aware UTC datetimes and volumes are m³. Returns ([], []) when
    the query API is unavailable or the window has no data.
    """
    if query_api is None:
        log.warning("No InfluxDB query API; cannot read history.")
        return [], []

    flux = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {range_start})\n'
        '  |> filter(fn: (r) => r._measurement == "height_measure")\n'
        '  |> filter(fn: (r) => r._field == "lenght_median")\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        '  |> sort(columns: ["_time"])'
    )

    times, volumes = [], []
    try:
        tables = query_api.query(flux, org=INFLUXDB_ORG)
    except Exception as e:
        log.warning(f"Could not query history from InfluxDB: {e}")
        return [], []

    for table in tables:
        for record in table.records:
            times.append(record.get_time())
            volumes.append(height_to_volume(record.get_value()))
    return times, volumes

class PuitError(Exception):
    """A request came back as status:"error", or got no correlated reply at all.

    `code` is the firmware's error code and `resp` the full response dict (empty when
    nothing came back). The measurement errors carry the four ping counts and the
    effective parameters, so a single logged line explains itself.
    """

    def __init__(self, code, resp=None, message=None):
        super().__init__(message or code)
        self.code = code
        self.resp = resp or {}


class PuitRetryable(PuitError):
    """Transient: ripples, an oblique surface, a lost line. Another burst may work."""


class PuitPermanent(PuitError):
    """Not fixable by retrying: a physical fault, or a bug in the request we sent."""


## What to do about each firmware error code (decision table in the firmware README).
## Anything not listed is a Pi-side bug — bad_param, bad_id, bad_request, line_too_long,
## unknown_cmd — where retrying cannot help and the code has to be fixed instead.
RETRYABLE_CODES = frozenset({'insufficient_samples', 'echo_timeout'})
PHYSICAL_CODES = frozenset({'sensor_fault', 'out_of_range'})   # someone has to go and look

## Request ids are a counter chosen by us and echoed verbatim, so a reply can be matched to
## its request instead of being assumed to be the next line on the port.
_request_id = itertools.count(1)


def _check_proto(obj):
    """Reject a board speaking a different protocol version, loudly and immediately.

    Every line the firmware emits carries `proto`, so this catches an un-upgraded board
    from any reply — not just the banner — before a value derived from a different
    contract reaches the database.
    """
    proto = obj.get('proto')
    if proto != PROTO:
        raise PuitPermanent(
            'proto_mismatch', obj,
            message=f"Arduino speaks proto {proto!r}, this code speaks {PROTO} — flash the "
                    f"matching firmware (/flash) or update {os.path.basename(__file__)}.")


def _check_board(banner, device):
    """Confirm the firmware on `device` says it is the board this module drives.

    The USB product string is what got us to this port, but that string is set at build time
    and vouches for nothing about the image actually in the application slot. This checks the
    running firmware's own answer, which is the only thing that can catch a board flashed
    with another board's image — realistically, an env block copied in the firmware repo with
    only one of its two role settings updated.

    The firmware calls this field `role`; this module calls the concept `board`, because
    `role` already means the admin/viewer permission level elsewhere in the project. The wire
    name is the firmware's to choose, so it is read as-is rather than renamed.

    Absent on firmware predating the field: warn and carry on rather than refuse, so this
    code can be deployed before the board is reflashed. `PROTO` stays at 2 for the same
    reason — adding a field is additive, and bumping it would force a flag-day reflash.
    """
    reported = banner.get('role')
    if reported is None:
        log.warning(
            f"Firmware on {device} does not say which board it is, so its identity is "
            f"unverified; flash a firmware built with -DPIJARDIN_ROLE to close this.")
    elif reported != BOARD:
        raise PuitPermanent(
            'wrong_board', banner,
            message=f"The board on {device} reports its role as {reported!r}, but this code "
                    f"drives {BOARD!r} — either that board carries the wrong firmware, or the "
                    f"wrong device was resolved. Refusing to record its readings as {BOARD!r}.")


def _format_uptime(ms):
    """Human-readable board uptime, or '' when the firmware reported none."""
    if not isinstance(ms, (int, float)):
        return ''
    seconds = int(ms // 1000)
    if seconds < 120:
        return f" up={seconds}s"
    if seconds < 3600:
        return f" up={seconds // 60}min"
    return f" up={seconds // 3600}h{(seconds % 3600) // 60:02d}"


def _read_json_object(arduino, deadline):
    """Return the next JSON object from the port, or None once `deadline` passes.

    Anything that is not a JSON object is noise on a line-delimited JSON link (a truncated
    line, boot chatter from a foreign firmware) — log it and keep reading, because the
    reply we want may be right behind it.
    """
    while time.time() < deadline:
        raw = arduino.readline()
        if not raw:
            continue                       # readline() timeout, not end of stream
        line = raw.decode('utf-8', errors='ignore').strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            log.warning(f"Ignoring non-JSON line from Arduino: {line!r}")
            continue
        if not isinstance(obj, dict):
            log.warning(f"Ignoring JSON that is not an object: {line!r}")
            continue
        return obj
    return None


def _burst_timeout(cmd, params):
    """How long to wait for a reply: the worst case of every ping in the burst failing.

    Sized against the slowest burst, never against a good reading. A healthy burst is back in
    well under a second, but one where the sensor answers and then finds nothing spends the
    full echo timeout on every ping, and one where nothing answers at all spends the full
    rise deadline instead. Too short a deadline here turns a carefully diagnosed sensor_fault
    into a generic no_reply — throwing the diagnosis away at exactly the moment it matters,
    and blaming the link for what the firmware had correctly identified as the sensor.

    Both per-ping waits are counted even though a single ping can only hit one of them: which
    one it hits varies within a burst, so only their sum is a safe bound. At the defaults that
    is ~3.0 s for a 10-ping burst and ~4.5 s for 25.
    """
    if cmd == 'status':
        return 2.0
    # Bound by what the board will actually do, not by what we asked for: out-of-range
    # parameters are clamped rather than rejected, so n=999 is 25 pings — and without the
    # clamp here, a typo in a diagnostic would strand the caller for minutes.
    n = min(params.get('n') or N_DEFAULT, MAX_N)
    timeout_us = min(params.get('timeout_us') or TIMEOUT_DEFAULT_US, 60000)
    ack_timeout_us = min(params.get('ack_timeout_us') or ACK_TIMEOUT_DEFAULT_US, 60000)
    return 2.0 + n * ((timeout_us + ack_timeout_us) / 1e6 + PING_GAP_S)


def request(arduino, cmd, timeout=None, **params):
    """Send one request and return the response dict, or raise PuitError.

    Parameters left out (or passed as None) are simply not sent: the board is stateless and
    falls back to its own documented defaults, and the response echoes what it actually
    used — including any value it clamped into range.
    """
    params = {name: value for name, value in params.items() if value is not None}
    req_id = next(_request_id)
    line = json.dumps({'id': req_id, 'cmd': cmd, **params}, separators=(',', ':'))
    payload = line.encode('utf-8') + b'\n'
    if len(payload) > LINE_MAX:
        raise PuitPermanent('line_too_long', message=f"request is {len(payload)} B, over the "
                                                     f"board's {LINE_MAX} B line limit: {line}")

    if timeout is None:
        timeout = _burst_timeout(cmd, params)
    log.debug(f"-> {line}")
    arduino.write(payload)

    deadline = time.time() + timeout
    while True:
        resp = _read_json_object(arduino, deadline)
        if resp is None:
            raise PuitRetryable('no_reply', message=f"no reply to {cmd} (id={req_id}) "
                                                    f"within {timeout:.1f} s")
        log.debug(f"<- {resp}")
        _check_proto(resp)

        if resp.get('type') != 'resp':
            continue                       # a boot banner, or a line meant for nobody
        if resp.get('id') is None:
            # bad_request / line_too_long / bad_id: the board could not tell which request
            # it was answering, so it has no id to echo. Ours is the only one in flight.
            raise PuitPermanent(resp.get('code', 'bad_request'), resp,
                                message=f"{cmd}: {resp.get('code', 'bad_request')} — the board "
                                        f"could not parse the request we sent")
        if resp.get('id') != req_id:
            # A late reply to a request that already timed out. Correlating on the id is
            # exactly what stops it from being read as the answer to this one.
            log.info(f"Discarding stale reply for id={resp['id']} (waiting for {req_id}).")
            continue

        if resp.get('status') == 'ok':
            return resp

        code = resp.get('code', 'unknown')
        error = PuitRetryable if code in RETRYABLE_CODES else PuitPermanent
        raise error(code, resp, message=f"{cmd}: {code}"
                                        + (f" [{describe_counts(resp)}]" if 'n' in resp else "")
                                        + (f" field={resp['field']}" if 'field' in resp else ""))


def describe_counts(resp):
    """Summarise which physical outcome the pings of a burst had.

    Present on every measurement reply, success or failure, and the level worth recording:
    `rejected=10` means the sensor is alive and aimed at the wrong thing, `no_response=10`
    means it is not talking to us at all. Those were indistinguishable under proto 1.
    """
    parts = [f"n={resp.get('n', '?')}"]
    parts += [f"{field[2:]}={resp.get(field, '?')}"
              for field in ('n_valid', 'n_timeout', 'n_rejected')]

    # n_stuck is a subset of n_no_response, never a fifth bucket, so it is rendered inside
    # it — the four buckets have to keep visibly summing to n. Shown only when non-zero:
    # zero is the healthy case, and it is absent altogether on firmware before 2.1.0.
    no_response = f"no_response={resp.get('n_no_response', '?')}"
    if resp.get('n_stuck'):
        no_response += f"(stuck={resp['n_stuck']})"
    parts.append(no_response)

    # Reported against the window it is judged against, so the one assumption in the
    # measurement path with no physical ground truth behind it is never invisible again.
    if resp.get('ack_max_us') is not None:
        parts.append(f"ack_max={resp['ack_max_us']}/{resp.get('ack_timeout_us', '?')}µs")

    if resp.get('ping_status'):
        parts.append(resp['ping_status'])
    return ' '.join(parts)


def log_burst_health(resp):
    """Log what a successful burst cost, at the level each failure mode deserves.

    Lost echoes over water are ordinary — ripples, an oblique surface — and the median over
    the survivors is still sound, so they are an info line. An ignored trigger has no benign
    cause: it means the sensor did not react at all, which is the earliest sign of a failing
    connection, and no min_valid threshold would surface it because the burst still
    succeeded. (The firmware's 3 ms inter-ping gap is below the module's recommended
    measurement cycle, so an isolated one can be a timing artefact; a burst that is mostly
    no-response is unambiguous.)
    """
    if resp.get('n_no_response'):
        # n_stuck splits this into the two wiring faults it can be, at opposite ends of the
        # harness — see _sensor_fault_text() for why the distinction is worth carrying.
        side = ("echo held high, so no trigger was even fired — check the echo line (D7)"
                if resp.get('n_stuck') else "check power, the module, and the trigger line")
        log.warning(f"Sensor ignored {resp['n_no_response']} trigger(s) — {side} "
                    f"[{describe_counts(resp)}]")
    elif resp.get('n_valid', 0) < resp.get('n', 0):
        log.info(f"Lost {resp['n'] - resp['n_valid']} ping(s) to the environment "
                 f"[{describe_counts(resp)}]")

    _log_ack_margin(resp)


def _log_ack_margin(resp):
    """Warn when the module's reaction time is closing on the deadline it is judged against.

    ack_max_us is not a fault signal in itself — it is how long the module took to react, and
    it is worth watching because the deadline above it is the only bound here derived from a
    datasheet rather than from physics. A module going soft drifts upward for weeks, and the
    moment it crosses ack_timeout_us the firmware starts reporting healthy hardware as a dead
    sensor, with a code that says "unpowered, dead, or a wire off" and sends someone to the
    well. Catching the drift is far cheaper than diagnosing that alert. Trend it in Grafana
    too — the field is recorded on every point.
    """
    ack_max = resp.get('ack_max_us')
    window = resp.get('ack_timeout_us')
    # 0 means nothing answered (there is no latency to report); absent means fw < 2.1.0.
    if not ack_max or not window:
        return
    if ack_max > ACK_MARGIN_WARN * window:
        log.warning(f"Sensor took {ack_max} µs to answer a trigger — over "
                    f"{ACK_MARGIN_WARN:.0%} of the {window} µs deadline. A module that keeps "
                    f"slowing down will start being reported as dead hardware; re-measure "
                    f"with /echantillons and raise ack_timeout_us in the firmware.")


def measure(arduino, cmd='read_puit', retries=3, delay=0.5, **params):
    """Run one measurement burst, retrying only what a retry can fix.

    Returns the successful response dict. Transient failures (too few valid pings, no echo,
    a lost reply) are retried; a physical fault or a malformed request raises straight out,
    because repeating it would only delay the report by `retries * delay`.
    """
    last = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return request(arduino, cmd, **params)
        except PuitRetryable as e:
            last = e
            log.warning(f"{cmd} attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(delay)
    raise last


def open_arduino(device=None):
    """Open the board's serial port and confirm what is on the other end.

    Returns an open port whose board has answered with a matching `proto` and the expected
    `role`; raises rather than hand back a port to something unidentified.

    device: an explicit node, bypassing resolution. Only the flasher passes this — it may be
    talking to a board whose descriptor has not come back yet, or one targeted with --port
    for recovery. Normal callers pass nothing and let the registry find the board.

    Resolution happens here rather than at import time on purpose: the Telegram bot and the
    flasher both import this module, and a module-level lookup would raise at import with the
    board unplugged and take the bot down with it. Same reasoning as the lazy Influx client.
    """
    if device is None:
        device = boards.resolve(BOARD)

    # Assert DTR once the port is open, the way a serial terminal would: the SAMD21's CDC
    # endpoint derives "a host is attached" from DTR, and the core can discard writes made
    # while it is low. Deasserting first guarantees the transition rather than depending on
    # whatever the previous session left behind.
    #
    # It does NOT reset the board — a natural assumption, and the reason this code originally
    # waited for a boot banner. DTR-as-reset is an AVR arrangement: a capacitor from the USB
    # bridge chip's DTR pin to the MCU's RESET pin. The XIAO has neither, because the SAMD21
    # speaks USB natively — DTR is just a flag in a CDC control request that nothing acts on
    # at this baud rate. On SAMD the reset convention is opening the port at 1200 baud
    # (see enter_bootloader in arduino/flash_firmware.py), and that enters the bootloader
    # rather than restarting the sketch. So the board runs uninterrupted across opens and
    # never re-announces itself; its identity is established by the handshake below.
    arduino = serial.Serial()
    arduino.port = device
    arduino.baudrate = boards.config(BOARD)['baud']
    arduino.timeout = .1
    arduino.dtr = False
    arduino.open()
    time.sleep(0.25)
    arduino.dtr = True

    try:
        # Ask the board who it is instead of waiting for the banner it only prints on a real
        # reboot. Waiting for that banner cost the full 10 s timeout on every single open —
        # ~48 min/day across the scheduled runs — and then fell through to this same request
        # anyway. `status` is also the better handshake: it answers synchronously and carries
        # proto, role, fw and uptime_ms, where the banner carries the first three. Bounded at
        # 2 s by _burst_timeout. A banner still unread in the buffer (we did just catch a
        # board mid-boot) is skipped harmlessly — request() ignores anything not a `resp`.
        try:
            banner = request(arduino, 'status')
        except PuitRetryable as e:
            raise PuitPermanent(
                'no_banner',
                message=f"The board on {device} did not answer a status request ({e}) — it is "
                        f"wedged, or running pre-proto-{PROTO} firmware that does not speak "
                        f"JSON. /flash it.") from e

        _check_proto(banner)
        _check_board(banner, device)
        # uptime is the only thing that reveals a board which rebooted on its own between
        # measurements — a brownout or a watchdog — which was previously invisible: a board
        # that never announces itself looks identical whether or not it restarted.
        log.info(f"Arduino ready on {device}: role={banner.get('role', '?')} "
                 f"fw={banner.get('fw', '?')} proto={banner.get('proto')}"
                 f"{_format_uptime(banner.get('uptime_ms'))}")
        return arduino
    except Exception:
        arduino.close()     # never leak the port when the handshake fails
        raise


def get_sensor_data(arduino, retries=3, delay=0.5, **params):
    """Median distance in cm over the valid pings, or None if there is no usable reading.

    The tolerant wrapper around measure(), for the callers where a reading is a health check
    rather than data (post-flash verification): every failure collapses to None. The
    measurement path calls measure() directly — it needs the error code to decide whether to
    retry, alert, or record.
    """
    try:
        resp = measure(arduino, retries=retries, delay=delay, **params)
    except PuitError as e:
        # WARNING+ auto-records to InfluxDB via the handler in the scheduled run.
        log.error(f"No valid sensor reading: {e}")
        return None
    log_burst_health(resp)
    log.info(f"Read {resp['value']} cm (pulse {resp.get('pulse_us')} µs) "
             f"[{describe_counts(resp)}]")
    return resp['value']

def load_state():
    """Read the sensor state file (last measure, last fault alert); {} if unreadable."""
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"Could not load {STATE_FILE} ({e}); treating the state as empty.")
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"Could not save {STATE_FILE} ({e}).")

def load_previous_measure():
    value = load_state().get('previous_measure')
    return float(value) if isinstance(value, (int, float)) else None

def save_previous_measure(height):
    # Merge, don't overwrite: the fault-alert timestamps live in the same file.
    state = load_state()
    state['previous_measure'] = height
    save_state(state)

def fault_alert_key(error):
    """Throttle slot for a fault alert: the code, plus which wiring fault it turned out to be.

    The two sensor_fault flavours get separate slots on purpose. They share a code but send
    someone to opposite ends of the harness, so if an echo-line fault is repaired and a
    trigger-side one appears an hour later, the second must not be swallowed by the first
    one's 6 h throttle.
    """
    if error.code != 'sensor_fault':
        return error.code
    stuck = error.resp.get('n_stuck')
    if stuck is None:
        return error.code                       # fw < 2.1.0: undifferentiated, one slot
    return f"{error.code}:{'stuck' if stuck else 'no_ack'}"


def _sensor_fault_text(resp):
    """The sensor_fault message, split by which end of the wiring to go and look at.

    n_stuck > 0 means echo was already HIGH when the ping began, so the trigger was never
    fired at all: the fault is on the echo line — held high, miswired, a damaged input.
    n_stuck == 0 means triggers did go out and nothing ever came back: power, the module, or
    the trigger line. Both are critical and neither is retried, but they are opposite ends of
    the harness, so an alert that does not say which wastes the trip.

    A board on firmware before 2.1.0 does not report n_stuck at all. Say so rather than
    assuming zero: guessing here would send someone confidently to the wrong wire, which is
    the same unearned certainty the firmware added these fields to remove.
    """
    tail = "Aucune mesure n'est enregistrée jusqu'à réparation."
    stuck = resp.get('n_stuck')
    if stuck is None:
        return ("🚨 Puit : le capteur ne répond plus au déclenchement — alimentation, câblage "
                f"ou module HS. {tail}")
    if stuck:
        return ("🚨 Puit : la ligne echo reste haute — aucun déclenchement n'a même été émis. "
                "À vérifier côté echo (D7) : câble, adaptateur de niveau, entrée endommagée. "
                f"{tail}")
    return ("🚨 Puit : les déclenchements sont émis mais le capteur ne répond jamais — "
            f"alimentation, module HS, ou ligne trigger (D8). {tail}")


def notify_sensor_fault(error):
    """Tell the admins the sensor needs physical attention — at most once per fault per 6 h.

    Only for the two codes that are never retried and will not clear by themselves. The
    throttle is the point: the scheduled run fires every 5 minutes, so the same dead sensor
    would otherwise send ~290 identical messages a day.
    """
    state = load_state()
    sent = state.get('fault_alerts') or {}
    now = time.time()
    key = fault_alert_key(error)
    last = sent.get(key)
    if isinstance(last, (int, float)) and now - last < FAULT_ALERT_INTERVAL_S:
        log.info(f"Fault alert for {key} already sent {(now - last) / 3600:.1f} h ago; "
                 "not repeating.")
        return

    detail = describe_counts(error.resp) if error.resp else str(error)
    if error.code == 'sensor_fault':
        text = _sensor_fault_text(error.resp)
        # Spelled out even when zero, unlike in the log line: these are the two numbers that
        # turn "go look at the well" into "go look at the echo wire", and an ack_max_us of 0
        # confirms nothing answered at all rather than answering just outside the window.
        detail += (f"\nn_stuck={error.resp.get('n_stuck', 'n/a')} · "
                   f"ack_max_us={error.resp.get('ack_max_us', 'n/a')}")
    else:
        text = ("🚨 Puit : le capteur répond mais toutes les mesures sont hors plage — "
                "probablement mal orienté ou obstrué (support, paroi, surface).")
    try:
        alerts.send_telegram(alerts.admin_recipients(), f"{text}\n({detail})")
    except Exception as e:
        log.error(f"Could not notify admins of {error.code}: {e}")
        return          # not recorded as sent, so the next run tries again

    sent[key] = now
    state['fault_alerts'] = sent
    save_state(state)

def write_influx_version(event='deploy'):
    """Record a Point('version') marking the currently-deployed versions.

    Written from deploy/deploy.sh (event='deploy') and on a successful firmware flash
    (event='flash'), so Grafana can annotate version changes across every panel. Fields
    are pi_version (repo git hash), arduino_version (arduino/VERSION) and grafana_version
    (grafana/ subtree hash — changes only when the dashboards/config do). Best-effort.
    """
    if write_api is None:
        log.warning("No InfluxDB write API; version marker not recorded.")
        return False

    point = (
        Point('version')
        .tag('event', event)
        .field('pi_version', pi_version(_REPO))
        .field('arduino_version', arduino_version(_REPO))
        .field('grafana_version', grafana_version(_REPO))
    )
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        log.warning(f"Could not write version marker to InfluxDB: {e}")
        return False

def write_service_failure(unit):
    """Record a CRITICAL Point('log') for a systemd unit that entered its failed state.

    Called by the pijardin-onfailure@ unit (see systemd/) so a crashed service — e.g. the
    Telegram bot, which never writes to InfluxDB itself — still leaves a trace in the DB.
    """
    if write_api is None:
        log.warning("No InfluxDB write API; service-failure not recorded.")
        return False

    point = (
        Point('log')
        .tag('level', 'CRITICAL')
        .tag('source', unit)
        .field('message', 'systemd: service entered failed state')
    )
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        log.warning(f"Could not write service-failure to InfluxDB: {e}")
        return False

## Extra fields stored next to the distance, and the type they must keep for ever (InfluxDB
## rejects a field that changes type). pulse_us and temp_c are the raw echo width and the air
## temperature it was converted with: the pulse is the only thing the board actually measures,
## so storing both means a future correction to the µs->cm divisor can be replayed over
## everything already recorded. The counts make a sensor degrading over weeks visible as a
## graph (n_valid/n) instead of as a surprise the morning it dies.
##
## n_stuck and ack_max_us arrive with fw 2.1.0 and are simply absent on an older board — the
## loop below skips whatever is missing, so this stays compatible either way. ack_max_us is
## the one to graph next to n_valid/n: it is the module's reaction time, and its upward trend
## is both an early warning of a dying module and a prediction of when the firmware will
## begin reporting a live sensor as dead.
MEASUREMENT_FIELDS = (
    ('pulse_us', float), ('temp_c', float),
    ('n', int), ('n_valid', int), ('n_timeout', int), ('n_rejected', int), ('n_no_response', int),
    ('n_stuck', int), ('ack_max_us', int),
)

def write_influx_measurement(heigth_median, resampled=False, resp=None):
    if write_api is None:
        log.warning("No InfluxDB write API; measurement not recorded.")
        return False

    # Timestamp rounded to the nearest 5 minutes; must be UTC-aware because the
    # client interprets naive datetimes as UTC
    rounded = roundTime(datetime.datetime.now(datetime.timezone.utc), roundTo=300)

    log.info(f'Write influxdb time {rounded.isoformat()}')
    # Create a data point for InfluxDB. lenght_median (cm) is the field the Grafana
    # dashboards read; the rest is diagnosis and future repair.
    point = (
        Point('height_measure')  # Measurement name
        .tag('resampled', resampled)
        .field('lenght_median', heigth_median)
    )
    for name, cast in MEASUREMENT_FIELDS:
        value = (resp or {}).get(name)
        if isinstance(value, (int, float)):
            point.field(name, cast(value))
    point.time(rounded, WritePrecision.S)

    # Write data to InfluxDB
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        log.error(f"Could not write measurement to InfluxDB: {e}")
        return False


def collect_puit_data(arduino):
    """One measurement cycle: burst, resample if the level jumped, record, check thresholds.

    Returns (height_cm, resampled, db_ok). Raises PuitError when the sensor produced no
    usable reading — the code says whose problem it is, and it is already logged at the
    level it deserves and alerted on if someone has to go to the well.
    """
    max_diff_tolerance = 5 # cm
    resampled = False

    previous_measure = load_previous_measure()
    try:
        resp = measure(arduino)
    except PuitPermanent as e:
        if e.code in PHYSICAL_CODES:
            # A silent sensor is critical, a misaimed one an error; neither is retried,
            # both need someone at the well.
            log.log(logging.CRITICAL if e.code == 'sensor_fault' else logging.ERROR,
                    f"Sensor needs physical attention: {e}")
            notify_sensor_fault(e)
        else:
            log.error(f"Arduino rejected the request: {e} — bug in "
                      f"{os.path.basename(__file__)}, retrying cannot help.")
        raise
    except PuitRetryable as e:
        log.warning(f"No usable reading this run: {e}")
        raise

    log_burst_health(resp)
    height = resp['value']
    responses = [resp]

    if previous_measure is not None:
        if abs(height-previous_measure) > max_diff_tolerance:
            for _ in range(4):
                try:
                    extra = measure(arduino)
                except PuitError as e:
                    # Keep the bursts we do have; the first one already succeeded.
                    log.warning(f"Resample failed: {e}")
                    continue
                log_burst_health(extra)
                responses.append(extra)
            height = float(median([r['value'] for r in responses]))
            resampled = True

    # Record the raw pulse and counts of the burst that produced the retained value, so
    # pulse_us and lenght_median stay the same measurement — that is what makes a later
    # divisor correction replayable over the history.
    kept = min(responses, key=lambda r: abs(r['value'] - height))
    db_ok = write_influx_measurement(height, resampled, kept)
    save_previous_measure(height)

    try:
        alerts.check_thresholds(height_to_volume(height))
    except Exception as e:
        log.error(f"Alert check failed: {e}")

    return height, resampled, db_ok


@contextlib.contextmanager
def puit_lock(timeout):
    """Hold an exclusive flock on LOCK_FILE for the duration of the `with` block.

    This is the single cross-process gate on the serial port: the scheduled
    sensors.service run, the Telegram bot's /mesure, and firmware flashing all take
    it so only one of them drives /dev/ttyACM0 at a time. Raises TimeoutError if the
    lock is not acquired within `timeout` seconds.
    """
    import fcntl  # Linux-only; imported here so the module loads on dev machines

    lock_fd = open(LOCK_FILE, 'w')
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError("Another measurement is already in progress.")
                time.sleep(0.5)
        yield
    finally:
        lock_fd.close()  # releases the flock


def _with_arduino(lock_timeout, action):
    """Serialize serial-port access (flock) and run `action(arduino)` on a fresh port.

    Raises TimeoutError if another measurement holds the lock past lock_timeout
    seconds.
    """
    with puit_lock(lock_timeout):
        arduino = open_arduino()
        try:
            return action(arduino)
        finally:
            arduino.close()


def measure_once(lock_timeout=0):
    """Run one full measurement cycle: lock, open serial, measure (+DB write), clean up.

    Returns (height, resampled, db_ok). Raises PuitError if the sensor gave no valid
    reading, or TimeoutError if another measurement holds the lock past lock_timeout
    seconds.
    """
    return _with_arduino(lock_timeout, collect_puit_data)


def raw_samples_once(lock_timeout=0, n=None, ack_timeout_us=None):
    """Ask the Arduino for one `sampling` burst — per-ping detail, diagnostics only.

    Returns the response dict: the same statistics as a measurement plus index-aligned
    `samples`/`pulse_us`/`ack_us` arrays and `ping_status`, one character per ping. That last
    field is the only per-ping way to tell a sensor that answered but saw nothing (T) from one
    that ignored the trigger (N) or never got one (S) — all three are null in `samples`.
    `ack_us` follows a different null rule and separates T from N/S on its own: it holds a
    latency for every ping the module engaged with, including the timeouts, so a null pulse
    beside a real ack reads as "answered, found nothing" — a sensor returning no data that is
    nonetheless alive.

    `ack_timeout_us` (500–60000, default 50000) widens the per-ping wait for echo to rise.
    That is the diagnostic for a suspicious sensor_fault: if a burst reporting
    n_no_response == n comes back valid at a wider window, the window was the fault and the
    sensor was never broken — which is exactly what the firmware's original 2 ms deadline did
    to a healthy module that takes 12.3 ms.

    Not retried: a diagnostic wants the failure it actually got. Raises PuitError (with the
    failing response on `.resp`) if the burst failed, TimeoutError like measure_once.
    """
    def action(arduino):
        resp = measure(arduino, cmd='sampling', retries=1, n=n,
                       ack_timeout_us=ack_timeout_us)
        log_burst_health(resp)
        return resp

    return _with_arduino(lock_timeout, action)


def roundTime(dt=None, roundTo=60):
   """Round a datetime object to any time lapse in seconds
   dt : datetime.datetime object, default now.
   roundTo : Closest number of seconds to round to, default 1 minute.
   Author: Thierry Husson 2012 - Use it as you want but don't blame me.
   """
   if dt == None : dt = datetime.datetime.now()
   seconds = (dt.replace(tzinfo=None) - dt.min).seconds
   rounding = (seconds+roundTo/2) // roundTo * roundTo
   return dt + datetime.timedelta(0,rounding-seconds,-dt.microsecond)

# -------------------------------------------------------------------------------------------------
# SCHEDULING

if __name__ == '__main__':
    setup_logging()

    # CLI sub-commands used by deploy/deploy.sh and the systemd OnFailure hook. These only
    # record a point and exit — they never touch the serial port.
    if len(sys.argv) >= 2 and sys.argv[1] == 'record-version':
        event = sys.argv[2] if len(sys.argv) > 2 else 'deploy'
        sys.exit(0 if write_influx_version(event) else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == 'record-failure':
        unit = sys.argv[2] if len(sys.argv) > 2 else 'unknown'
        sys.exit(0 if write_service_failure(unit) else 1)

    # Normal scheduled run: record data-path WARNING+ to InfluxDB, then measure. Attached
    # here (not on import) so the Telegram bot, which imports this module, never writes logs
    # to InfluxDB — only the sensors.service process does.
    attach_influx_handler(write_api, INFLUXDB_BUCKET, INFLUXDB_ORG)

    try:
        height, resampled, db_ok = measure_once(lock_timeout=60)
    except TimeoutError as e:
        log.error(str(e))
        sys.exit(1)
    except PuitError:
        # collect_puit_data has already logged this at the right level and alerted if the
        # sensor needs attention; the exit code is what systemd's OnFailure hook keys on.
        sys.exit(1)
    except Exception as e:
        log.error(f"Could not open the {BOARD} board's serial port: {e}")
        sys.exit(1)

    if not db_ok:
        sys.exit(1)
