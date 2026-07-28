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

## Serial communication
SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 9600

## Firmware serial contract — newline-delimited JSON, one object per line each way. The
## authority is the README of the firmware repo (PiJardin-Arduino_Software); the constants
## below only size our own timeouts and guard our own requests. Everything else is
## discovered from the board: it echoes the effective value of every parameter it used.
PROTO = 2                       # must match the firmware; bumped only by a breaking change
LINE_MAX = 192                  # longest request line the board accepts, bytes
N_DEFAULT = 10                  # pings per burst, board-side default
TIMEOUT_DEFAULT_US = 45000      # per-ping echo timeout, board-side default

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
    """How long to wait for a reply: the worst case of every ping in the burst timing out."""
    if cmd == 'status':
        return 2.0
    n = params.get('n') or N_DEFAULT
    timeout_us = params.get('timeout_us') or TIMEOUT_DEFAULT_US
    return 2.0 + n * (timeout_us / 1e6 + 0.02)


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
              for field in ('n_valid', 'n_timeout', 'n_rejected', 'n_no_response')]
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
        log.warning(f"Sensor ignored {resp['n_no_response']} trigger(s) — check power and "
                    f"wiring [{describe_counts(resp)}]")
    elif resp.get('n_valid', 0) < resp.get('n', 0):
        log.info(f"Lost {resp['n'] - resp['n_valid']} ping(s) to the environment "
                 f"[{describe_counts(resp)}]")


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


def open_arduino():
    # Open with DTR deasserted, then assert it: the edge auto-resets the Arduino
    # deterministically. Relying on open() alone is not enough — whether it
    # produces a reset edge depends on the DTR state left by the previous
    # session, and an un-reset board serves a stale reading (frozen /mesure bug:
    # no boot banner in the journal, constant 61.00 response).
    arduino = serial.Serial()
    arduino.port = SERIAL_PORT
    arduino.baudrate = BAUD_RATE
    arduino.timeout = .1
    arduino.dtr = False
    arduino.open()
    time.sleep(0.25)
    arduino.dtr = True

    # The reset pulse rebooted the Arduino; wait for the one line it prints on boot.
    try:
        log.info("Waiting for the Arduino boot banner...")
        deadline = time.time() + 10
        banner = None
        while banner is None:
            obj = _read_json_object(arduino, deadline)
            if obj is None:
                break
            if obj.get('type') == 'ready':
                banner = obj
            else:
                log.info(f"Ignoring pre-banner line: {obj}")

        if banner is None:
            # Either the reset edge did not take or we came in mid-line. The board is
            # stateless and will answer anyway, but confirm what is on the other end before
            # trusting a reading — a board that never rebooted is the frozen-/mesure case.
            log.warning("No boot banner within 10 s; probing the board with a status request.")
            try:
                banner = request(arduino, 'status')
            except PuitRetryable as e:
                raise PuitPermanent(
                    'no_banner',
                    message=f"Arduino neither announced itself nor answered a status request "
                            f"({e}) — it is wedged, or still running pre-proto-{PROTO} firmware "
                            f"that does not speak JSON. /flash it.") from e

        _check_proto(banner)
        log.info(f"Arduino ready: fw={banner.get('fw', '?')} proto={banner.get('proto')}")
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

def notify_sensor_fault(error):
    """Tell the admins the sensor needs physical attention — at most once per code per 6 h.

    Only for the two codes that are never retried and will not clear by themselves. The
    throttle is the point: the scheduled run fires every 5 minutes, so the same dead sensor
    would otherwise send ~290 identical messages a day.
    """
    state = load_state()
    sent = state.get('fault_alerts') or {}
    now = time.time()
    last = sent.get(error.code)
    if isinstance(last, (int, float)) and now - last < FAULT_ALERT_INTERVAL_S:
        log.info(f"Fault alert for {error.code} already sent {(now - last) / 3600:.1f} h ago; "
                 "not repeating.")
        return

    detail = describe_counts(error.resp) if error.resp else str(error)
    if error.code == 'sensor_fault':
        text = ("🚨 Puit : le capteur ne répond plus au déclenchement — alimentation, câblage "
                "ou module HS. Aucune mesure n'est enregistrée jusqu'à réparation.")
    else:
        text = ("🚨 Puit : le capteur répond mais toutes les mesures sont hors plage — "
                "probablement mal orienté ou obstrué (support, paroi, surface).")
    try:
        alerts.send_telegram(alerts.admin_recipients(), f"{text}\n({detail})")
    except Exception as e:
        log.error(f"Could not notify admins of {error.code}: {e}")
        return          # not recorded as sent, so the next run tries again

    sent[error.code] = now
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
MEASUREMENT_FIELDS = (
    ('pulse_us', float), ('temp_c', float),
    ('n', int), ('n_valid', int), ('n_timeout', int), ('n_rejected', int), ('n_no_response', int),
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


def raw_samples_once(lock_timeout=0, n=None):
    """Ask the Arduino for one `sampling` burst — per-ping detail, diagnostics only.

    Returns the response dict: the same statistics as a measurement plus index-aligned
    `samples`/`pulse_us` arrays and `ping_status`, one character per ping. That last field
    is the only per-ping way to tell a sensor that answered but saw nothing (T) from one
    that ignored the trigger (N) — both are null in the arrays.

    Not retried: a diagnostic wants the failure it actually got. Raises PuitError (with the
    failing response on `.resp`) if the burst failed, TimeoutError like measure_once.
    """
    def action(arduino):
        resp = measure(arduino, cmd='sampling', retries=1, n=n)
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
        log.error(f"Could not open serial port {SERIAL_PORT}: {e}")
        sys.exit(1)

    if not db_ok:
        sys.exit(1)
