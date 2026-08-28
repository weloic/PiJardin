###################################################################################################
# -------------------------------------------------------------------------------------------------
# Pump on/off recorder — a long-running listener, not a scheduled measurement.
#
# WHY THIS IS A DAEMON AND read_puit.py IS A TIMER
# ------------------------------------------------
# The well level is a value you go and fetch: it changes slowly, and a sample every 5 minutes
# describes it completely. The pump is an event. A poll every 5 minutes would quantise every
# transition to 5 minutes and miss whole cycles, and — the part that actually breaks it — it
# would have no way to know what it missed. So the board watches continuously and PUSHES; this
# process listens and records. It speaks first only to say hello and to ask what it missed.
#
# WHO SAYS WHAT
# -------------
#   1. We resolve the board, open the port and send exactly one `status`. That answers two
#      questions: am I talking to the right board (proto + role), and what is its state now.
#   2. We ask for `history` since the last event we durably recorded, and replay it.
#   3. From then on we send nothing. The board emits a line on every state change and a
#      heartbeat every 60 s; we write each one to InfluxDB.
#   4. We speak again only if the board goes silent past SILENCE_S — the sole symptom a
#      passive listener has.
#
# WHY THE HEARTBEATS ARE RECORDED TOO
# -----------------------------------
# Without them "no data for six hours" and "pump off for six hours" are the same picture, and
# the whole point of this measurement is telling those apart. A heartbeat point is proof the
# board was alive and what it saw; a gap in them is proof it was not.
#
# THE SERIAL PORT IS HELD OPEN FOR THE PROCESS'S WHOLE LIFE
# ---------------------------------------------------------
# So there is deliberately no flock here, unlike read_puit's. A lock coordinates processes that
# each take the port briefly; nothing can wait out a holder that never lets go. Anything else
# needing this port (a reflash) must `systemctl stop pump.service` first. Diagnostics that do
# not need the port — /pompe, /boards — read the state file or the USB layer instead.
#
# Opening does not disturb the board: DTR is asserted the way a terminal would (the CDC stack
# discards writes made while it is low), but on both PiJardin boards a reset needs the
# 1200-baud touch, not a DTR edge — see the long comment in read_puit.open_arduino. That is
# what makes `since_ms` meaningful across a restart of this service: the board keeps counting.
#
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import json
import time
import signal
import logging
import datetime
import itertools

import serial

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

## Repo root on sys.path for the `common` package; when run as a script only sensors/ is on it.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from common import boards
from common.logging_setup import setup_logging, attach_influx_handler

log = logging.getLogger('read_pump')

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Influx database — same bucket and lazy-client pattern as read_puit.py.
INFLUXDB_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG    = os.getenv("INFLUX_ORG",    "PiJardin")
INFLUXDB_BUCKET = os.getenv("INFLUX_BUCKET", "puit")

try:
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
except Exception as e:
    log.warning(f"Could not initialize InfluxDB client: {e}; nothing will be recorded.")
    write_api = None

BOARD = 'pump'

## Set by the SIGTERM handler so the read and backoff loops can leave promptly. Defined up
## here because listen() and run() both consult it and it must exist before either runs.
_stopping = False

## Must match the flashed firmware. Declared here rather than read from the registry for the
## same reason read_puit does: this constant is this module's statement of which contract it
## speaks, and a mismatch is fixed by flashing the board, not by editing the number.
PROTO = 2

## Tag value distinguishing this pump from any other added later. The pump that serves the
## well, so: 'puit'.
PUMP = 'puit'

## The firmware's own cadence, which we do not set and cannot change — these only size our
## expectations. The board reports its effective values in `status`; HB_INTERVAL_S is the
## fallback for a firmware that does not.
HB_INTERVAL_S = 60

## Missed heartbeats before we stop believing the link. Three, because a single dropped line
## is ordinary (the firmware drops rather than blocks when our end is not reading) and
## reconnecting costs a `status` round trip plus a history replay.
SILENCE_FACTOR = 3

## Reconnect backoff. Starts short because the overwhelmingly common cause is this service
## restarting a moment before the board finished re-enumerating; grows so a genuinely absent
## board does not fill the journal.
BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 60

## How long to wait for a reply to `status` / `history`. The board parses input between
## detector windows and may be mid-window, so its worst-case answer is one window (~200 ms)
## plus the reply itself. 3 s is generous enough that a timeout means something is wrong.
REQUEST_TIMEOUT_S = 3.0

## Longest request line the board accepts, bytes. Ours are far shorter; this only turns a
## malformed request into a local error instead of a truncated line the board rejects.
LINE_MAX = 192

## State the board can report. `fault` is the bias-plausibility failure — the module is not
## reporting, so the pump's state is unknown; it is deliberately NOT folded into `off`, since
## a disconnected signal wire and a stopped pump both read as a low RMS.
STATES = ('on', 'off', 'fault', 'unknown')

## How each state is stored. One integer field, always present, so the series never changes
## type and never has holes: >= 0 is the pump itself, < 0 means the board could not say.
## Filter `state >= 0` in Grafana for the pump trace, `state < 0` for the anomalies.
STATE_NUM = {'on': 1, 'off': 0, 'fault': -1, 'unknown': -2}

## Files kept next to this module, both gitignored. The state file is the durable cursor: it
## records what InfluxDB has ACCEPTED, not what the serial port delivered, so a crash between
## reading a line and writing it replays that line instead of losing it.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pump_state.json')

## Numeric fields copied from an event onto its point, and the type each must keep for ever
## (InfluxDB rejects a field that changes type). asym is the headroom check: the module's
## op-amp flattens the top of the wave well below the ADC rails, so n_clipped structurally
## cannot see it and this ratio is the only warning that the gain is too high.
EVENT_FIELDS = (
    ('rms_counts', float), ('freq_hz', float), ('asym', float),
    ('dropped', int), ('n_freq_reject', int), ('n_headroom', int),
)

# -------------------------------------------------------------------------------------------------
# ERRORS

class PumpError(Exception):
    """Any failure that should drop the link and reconnect."""

class PumpPermanent(PumpError):
    """Wrong board, wrong protocol: reconnecting to the same port cannot help."""

class BoardRebooted(PumpError):
    """The board announced itself mid-session. Not an error — it invalidates our cursor."""

class LinkSilent(PumpError):
    """No line at all for longer than the silence deadline."""


# -------------------------------------------------------------------------------------------------
# TRANSPORT
#
# Deliberately a second copy of read_puit's NDJSON reader rather than an import. The two are
# the same shape but not the same contract: read_puit.request() carries the burst-timeout
# arithmetic and the PuitError hierarchy of a request/response sensor, and read_puit.PROTO
# happens to be 2 as well — importing it would make a pump/puit protocol mix-up type-check
# perfectly and fail silently. The duplicated part is ~40 lines and is the part least likely
# to change; when a third board arrives, lift it into common/ with all three callers visible.

_request_id = itertools.count(1)


## The board's millis() counter, for the wrap below.
MS_WRAP = 2 ** 32


def elapsed_ms(start_ms, end_ms):
    """Milliseconds from `start_ms` to `end_ms` on the board's clock, or None.

    Both are board `millis()` values, NOT durations — see the field table in the firmware's
    docs/pump.md: `ms` is when the event happened and `prev_ms` is when the state being left
    began. The board sends timestamps and lets the Pi subtract, which is lossless (a duration
    can always be recovered, a timestamp cannot) but does put the 32-bit wrap on this side.
    Modular arithmetic is what handles it: at ~49.7 days the counter rolls over, and a plain
    subtraction would turn a two-minute run into a 49-day one.
    """
    if not isinstance(start_ms, (int, float)) or isinstance(start_ms, bool):
        return None
    if not isinstance(end_ms, (int, float)) or isinstance(end_ms, bool):
        return None
    return (int(end_ms) - int(start_ms)) % MS_WRAP


def _read_json_object(port, deadline):
    """Return the next JSON object from the port, or None on `deadline` or on shutdown.

    Anything that is not a JSON object is noise on a line-delimited JSON link (a truncated
    line, boot chatter) — log it and keep reading, because the line we want may be behind it.

    `_stopping` is checked every pass, so a SIGTERM is noticed within one readline timeout
    (1 s) instead of at the deadline. In the listen loop that deadline is the 180 s silence
    window, so without this the process sat here until the next heartbeat happened to arrive:
    a measured 47 s stop on a healthy link, and past systemd's 90 s TimeoutStopSec — i.e. a
    SIGKILL instead of a clean exit — on a link that had already gone quiet.
    """
    while time.time() < deadline and not _stopping:
        raw = port.readline()
        if not raw:
            continue                       # readline() timeout, not end of stream
        line = raw.decode('utf-8', errors='ignore').strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            log.warning(f"Ignoring non-JSON line from the pump board: {line!r}")
            continue
        if not isinstance(obj, dict):
            log.warning(f"Ignoring JSON that is not an object: {line!r}")
            continue
        return obj
    return None


def _check_line(obj, device):
    """Reject a board speaking a different protocol, or carrying another board's firmware.

    Both checks run on every line, not just the handshake: `proto` and `role` ride on
    everything the firmware emits precisely so a mismatch is caught from whatever arrives
    first, before a value from a different contract reaches the database.
    """
    proto = obj.get('proto')
    if proto != PROTO:
        raise PumpPermanent(
            f"The board on {device} speaks proto {proto!r}, this code speaks {PROTO} — flash "
            f"the matching firmware or update {os.path.basename(__file__)}.")

    role = obj.get('role')
    if role is None:
        return          # not every line has to carry it; the handshake checks it explicitly
    if role != BOARD:
        raise PumpPermanent(
            f"The board on {device} reports its role as {role!r}, but this code drives "
            f"{BOARD!r} — either that board carries the wrong firmware, or the wrong device "
            f"was resolved. Refusing to record its readings as the pump.")


def request(port, cmd, timeout=REQUEST_TIMEOUT_S, **params):
    """Send one request and return its response dict.

    Correlates on the echoed `id`, so an event or heartbeat arriving mid-exchange is handled
    rather than mistaken for the answer — which on this board is not an edge case: the
    detector keeps pushing while we are waiting.
    """
    req_id = next(_request_id)
    line = json.dumps({'id': req_id, 'cmd': cmd, **params}, separators=(',', ':'))
    payload = line.encode('utf-8') + b'\n'
    if len(payload) > LINE_MAX:
        raise PumpPermanent(f"request is {len(payload)} B, over the board's {LINE_MAX} B "
                            f"line limit: {line}")

    log.debug(f"-> {line}")
    port.write(payload)

    deadline = time.time() + timeout
    while True:
        resp = _read_json_object(port, deadline)
        if resp is None:
            raise PumpError(f"no reply to {cmd} (id={req_id}) within {timeout:.1f} s")
        log.debug(f"<- {resp}")
        _check_line(resp, port.port)

        if resp.get('type') == 'event':
            # An event we cannot record yet: the handshake has not established the time
            # anchor, or the replay is still running and this would land out of order. The
            # board keeps it in its ring buffer, so the history replay picks it up.
            log.debug("Deferring an event received mid-request; history will replay it.")
            continue
        if resp.get('type') != 'resp':
            continue                       # a boot banner, or a line meant for nobody
        if resp.get('id') != req_id:
            log.info(f"Discarding reply for id={resp.get('id')} (waiting for {req_id}).")
            continue

        if resp.get('status') == 'ok':
            return resp
        raise PumpError(f"{cmd}: {resp.get('code', 'unknown')}"
                        + (f" field={resp['field']}" if 'field' in resp else ""))


# -------------------------------------------------------------------------------------------------
# STATE FILE (the durable cursor)

def load_state():
    """Read the cursor file; {} if it is missing or unreadable."""
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"Could not load {STATE_FILE} ({e}); treating the cursor as empty.")
        return {}


def save_state(state):
    """Persist the cursor. Called only AFTER a successful InfluxDB write.

    That ordering is the whole point: the cursor names the last event the database actually
    holds, so a crash in between replays the line instead of skipping it. Written via a
    temporary file and replaced atomically — a torn cursor would resync from a seq that never
    existed.
    """
    tmp = STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"Could not save {STATE_FILE} ({e}).")


def _utc(timestamp):
    """A UTC-aware datetime from a POSIX timestamp (the client reads naive ones as UTC)."""
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


# -------------------------------------------------------------------------------------------------
# INFLUXDB

def write_state_point(state, when, source, fields=None, seq_missed=0):
    """Record one `pump_state` point: what the board saw, and how we came to hear it.

    Written for transitions AND heartbeats. The heartbeats are not filler — they are what
    makes a gap in this series mean "the board was not reporting" instead of being
    indistinguishable from a long quiet stretch with the pump off.
    """
    if write_api is None:
        log.warning("No InfluxDB write API; pump state not recorded.")
        return False

    point = (
        Point('pump_state')
        .tag('pump', PUMP)
        .tag('source', source)
        .field('state', STATE_NUM.get(state, STATE_NUM['unknown']))
    )
    for name, cast in EVENT_FIELDS:
        value = (fields or {}).get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            point.field(name, cast(value))
    if seq_missed:
        # Only when non-zero: it is the count of event lines that never reached us, and a
        # constant 0 on every point would just be noise in the schema.
        point.field('seq_missed', int(seq_missed))
    point.time(_utc(when), WritePrecision.S)

    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        log.error(f"Could not write pump state to InfluxDB: {e}")
        return False


def write_run_point(state, start, duration_s, truncated=False):
    """Record one completed `pump_run`: a state that ended, timestamped at its START.

    This exists so runtime and cycle counts are a `sum` and a `count` rather than an
    integration over a step series that Grafana would have to reconstruct. Every state gets
    one (not just `on`), because the runs then tile the timeline and a missing stretch is
    visible as a hole rather than as an assumption.

    truncated marks a duration we could not observe end to end — the daemon or the board was
    away for part of it, so the value is a lower bound. Recording it as if it were exact is
    the one thing that would make the daily total quietly wrong.
    """
    if write_api is None:
        log.warning("No InfluxDB write API; pump run not recorded.")
        return False

    point = (
        Point('pump_run')
        .tag('pump', PUMP)
        .tag('state', state)
        .tag('truncated', truncated)
        .field('duration_s', float(duration_s))
        .time(_utc(start), WritePrecision.S)
    )
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        log.error(f"Could not write pump run to InfluxDB: {e}")
        return False


# -------------------------------------------------------------------------------------------------
# RECORDING

def record_transition(state, when, source, fields=None, prev_state=None, duration_s=None,
                      truncated=False, seq=None, uptime_ms=None, seq_missed=0):
    """Write one state change, close out the state it ended, and advance the cursor.

    `duration_s` is how long `prev_state` lasted, already computed by the caller — from the
    board's own two timestamps on the live path (drift-free from its crystal, immune to
    however busy this Pi happened to be) and by chaining transition times on the replay path,
    where the board sends no timestamp for the state being left.
    """
    ok = True
    if prev_state in STATES and isinstance(duration_s, (int, float)) and duration_s > 0:
        ok = write_run_point(prev_state, when - duration_s, duration_s, truncated) and ok

    ok = write_state_point(state, when, source, fields, seq_missed) and ok
    if not ok:
        # Cursor NOT advanced: the board still holds this event, so the next connect replays
        # it. Better a duplicate point than a silent hole.
        log.warning(f"Pump transition to {state!r} not fully recorded; cursor left behind.")
        return False

    state_file = load_state()
    state_file.update({'state': state, 'state_start': when, 'last_line': when})
    if seq is not None:
        state_file['last_seq'] = seq
    if uptime_ms is not None:
        state_file['last_uptime_ms'] = uptime_ms
    save_state(state_file)
    return True


def record_heartbeat(state, when, fields, seq=None, uptime_ms=None, seq_missed=0):
    """Write a liveness point and advance the cursor. No run is closed — nothing ended."""
    if not write_state_point(state, when, 'heartbeat', fields, seq_missed):
        return False

    state_file = load_state()
    state_file['last_line'] = when
    # A heartbeat also carries the state, and it is the authority if our cursor disagrees —
    # that happens when a transition point failed to write and was rolled back above.
    if state_file.get('state') != state:
        log.warning(f"Heartbeat says {state!r} but the cursor says "
                    f"{state_file.get('state')!r}; trusting the board.")
        state_file['state'] = state
        state_file.setdefault('state_start', when)
    if seq is not None:
        state_file['last_seq'] = seq
    if uptime_ms is not None:
        state_file['last_uptime_ms'] = uptime_ms
    save_state(state_file)
    return True


# -------------------------------------------------------------------------------------------------
# CONNECT AND RESYNC

def connect():
    """Resolve the board, open its port, and confirm what is on the other end.

    Returns (port, status). Raises rather than hand back a port to something unidentified.
    """
    device = boards.resolve(BOARD)

    port = serial.Serial()
    port.port = device
    port.baudrate = boards.config(BOARD)['baud']
    # One second, not read_puit's 0.1: this loop blocks on readline() for its whole life, and
    # a short timeout would just spin. It also bounds how long a shutdown waits.
    port.timeout = 1.0
    port.dtr = False
    port.open()
    time.sleep(0.25)
    port.dtr = True

    try:
        status = request(port, 'status')
        _check_line(status, device)
        if status.get('role') != BOARD:
            raise PumpPermanent(
                f"The board on {device} did not identify itself as {BOARD!r} in its status "
                f"reply (role={status.get('role')!r}); refusing to record its output.")
        log.info(f"Pump board ready on {device}: role={status.get('role')} "
                 f"fw={status.get('fw', '?')} proto={status.get('proto')} "
                 f"state={status.get('state')} since={_format_since(status.get('since_ms'))} "
                 f"seq={status.get('seq')}")
        # A new connection knows nothing about what the board's counters did before it, so the
        # first reading has to re-establish the baseline rather than be read as an increase.
        _reset_health()
        return port, status
    except Exception:
        port.close()        # never leak the port when the handshake fails
        raise


def _format_since(ms):
    """Human-readable duration for a log line, or '?' when the board reported none."""
    if not isinstance(ms, (int, float)):
        return '?'
    seconds = int(ms // 1000)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}min"
    return f"{seconds // 3600}h"


def fetch_history(port, after_seq):
    """Drain the board's ring buffer of transitions newer than `after_seq`.

    Returns (events, truncated). `truncated` means the buffer had already wrapped past
    after_seq, so some transitions are gone for good — reported rather than papered over,
    because a runtime total computed from an incomplete replay is wrong in a way nothing
    downstream could detect.
    """
    events, truncated = [], False
    cursor = after_seq
    while True:
        resp = request(port, 'history', after_seq=cursor)
        batch = resp.get('events') or []
        truncated = truncated or bool(resp.get('truncated'))
        events.extend(batch)
        if not resp.get('more') or not batch:
            break
        cursor = batch[-1].get('seq', cursor)
    return events, truncated


def resync(port, status):
    """Reconcile the database with the board after a connect, then return the live seq.

    This is what a polled design cannot do. The board kept watching while this process was
    away — restarted by a deploy, or the whole Pi rebooted — and it carries both the ring
    buffer of what happened and `since_ms` for the state still in effect. So the gap is
    filled from the board's own record rather than guessed at from either side.
    """
    now = time.time()
    uptime_ms = status.get('uptime_ms')
    board_state = status.get('state', 'unknown')
    since_ms = status.get('since_ms')
    seq = status.get('seq')

    # Wall time the board's millis() clock read zero. Only used for replayed events, which
    # happened in the past and have no arrival time of their own; live lines are stamped on
    # arrival, where the sub-100 ms latency is far inside the ~600 ms the board needs to
    # declare a change at all.
    anchor = now - (uptime_ms / 1000.0) if isinstance(uptime_ms, (int, float)) else None

    saved = load_state()
    last_seq = saved.get('last_seq')
    last_uptime = saved.get('last_uptime_ms')

    rebooted = (isinstance(uptime_ms, (int, float)) and isinstance(last_uptime, (int, float))
                and uptime_ms < last_uptime)

    if rebooted:
        # Its RAM buffer went with it, so anything from before the reboot is unrecoverable.
        # Close the state we were tracking at the last line we actually saw — a lower bound,
        # marked as one — rather than letting it run silently up to now.
        log.warning(f"The pump board rebooted (uptime {_format_since(uptime_ms)} < the "
                    f"{_format_since(last_uptime)} last seen). Events from before the reboot "
                    f"are lost; closing the previous state as truncated.")
        _close_truncated(saved)

        # Then mark the hole itself. From the last line we heard until the board's current
        # state began, what the pump did is genuinely unknown — it may have cycled any number
        # of times. Recording `unknown` over that span is what stops a query from spreading
        # either neighbouring state across it and reporting a runtime nobody observed.
        gap_start = saved.get('last_line') or now
        write_state_point('unknown', gap_start, 'reboot')
        # The cursor is rewritten, not just dropped: the reconciliation below reads it back
        # from disk, and leaving the pre-reboot state there would close that same state a
        # second time.
        save_state({'state': 'unknown', 'state_start': gap_start, 'last_line': gap_start,
                    'last_uptime_ms': uptime_ms})
        saved, last_seq = load_state(), None

    # Whether the gap between our cursor and now is fully accounted for. Only a complete
    # replay makes the durations below exact; anything else means the pump may have cycled
    # more than once in there and every span we write is a lower bound.
    complete = False
    replayed = 0
    if last_seq is not None and anchor is not None:
        try:
            events, truncated = fetch_history(port, last_seq)
            complete = not truncated
        except PumpError as e:
            # Not fatal: the live stream is what matters, and the state below is still
            # resynced exactly. Only the completed cycles inside the gap are at risk.
            log.warning(f"Could not replay history after seq {last_seq} ({e}); "
                        f"any cycle completed while this service was down is lost.")
            events, truncated = [], True
        if truncated:
            log.warning(f"The board's history buffer had already wrapped past seq {last_seq} "
                        f"— some pump cycles were never recorded and cannot be recovered.")
        # A history entry is {seq, state, ms} and nothing else — the ring buffer does not
        # store what each transition left behind, so unlike a live event there is no
        # prev_state/prev_ms to read. Durations come from chaining instead: each replayed
        # transition ends the state the one before it started, and the first ends whatever
        # the cursor was holding when this service stopped.
        chain_state = saved.get('state')
        chain_when = saved.get('state_start')
        for event in events:
            when = anchor + event.get('ms', 0) / 1000.0
            duration_s = (when - chain_when
                          if isinstance(chain_when, (int, float)) and when > chain_when
                          else None)
            record_transition(
                event.get('state', 'unknown'), when, 'replay', event,
                prev_state=chain_state, duration_s=duration_s,
                seq=event.get('seq'), uptime_ms=uptime_ms)
            chain_state, chain_when = event.get('state'), when
            replayed += 1
        if replayed:
            log.info(f"Replayed {replayed} pump transition(s) missed while disconnected.")

    # Whatever the replay did or did not cover, the board's current state is authoritative.
    # Record it if the cursor still disagrees — the pump changed while we were away and that
    # change was not (or not fully) in the buffer.
    saved = load_state()
    if saved.get('state') != board_state:
        when = now - (since_ms / 1000.0) if isinstance(since_ms, (int, float)) else now
        log.info(f"Resync: board is {board_state!r} since {_format_since(since_ms)}, cursor "
                 f"said {saved.get('state')!r}. Back-dating the change.")
        # Close the state the cursor was holding. It ran from its recorded start until the
        # instant the board says the current state began — exact when the replay covered the
        # whole gap, a lower bound otherwise, and marked as such either way.
        start = saved.get('state_start')
        if saved.get('state') in STATES and isinstance(start, (int, float)) and when > start:
            write_run_point(saved['state'], start, when - start, truncated=not complete)
        record_transition(board_state, when, 'resync', status,
                          seq=seq, uptime_ms=uptime_ms)

    return seq


def _close_truncated(saved):
    """Close an open state whose end we will never observe, as a lower bound.

    Only for a board reboot: its ring buffer went with it, so the state we were tracking ends
    somewhere we cannot see. It is closed at the last line we actually received, because that
    is the last instant there is evidence it still held — running it up to now would invent
    time the pump may not have spent in that state.
    """
    state = saved.get('state')
    start = saved.get('state_start')
    end = saved.get('last_line')
    if state in STATES and isinstance(start, (int, float)) and isinstance(end, (int, float)):
        if end > start:
            write_run_point(state, start, end - start, truncated=True)


# -------------------------------------------------------------------------------------------------
# LISTEN

def listen(port, seq, hb_interval_s):
    """Read and record until the link goes quiet, the board restarts, or we are stopping.

    This is the whole steady state of the service: no requests, no polling, one blocking read.
    Returns only on shutdown; every other way out is an exception the reconnect loop handles.
    """
    silence_s = SILENCE_FACTOR * hb_interval_s
    expected_seq = seq

    while not _stopping:
        deadline = time.time() + silence_s
        obj = _read_json_object(port, deadline)
        if obj is None:
            # Two ways to get here, and they must not be confused: a shutdown is a clean
            # return, while a genuinely quiet link is the one symptom this listener has.
            if _stopping:
                return
            raise LinkSilent(f"no line from the pump board for {silence_s:.0f} s "
                             f"({SILENCE_FACTOR} missed heartbeats)")

        _check_line(obj, port.port)
        now = time.time()
        kind = obj.get('type')

        if kind == 'ready':
            # The board restarted underneath us — a watchdog bite or a power blip. Our seq
            # cursor and its ring buffer both restarted with it, so bail out and let the
            # reconnect path run the full handshake and resync.
            raise BoardRebooted(f"the pump board announced itself mid-session "
                                f"(fw={obj.get('fw', '?')}); resyncing")

        if kind != 'event':
            log.debug(f"Ignoring a non-event line: {obj}")
            continue

        # A gap means event lines never reached us — the board drops rather than blocks when
        # our end is not reading, which is the correct trade (see the firmware's `dropped`
        # counter) but leaves a hole worth recording on the point it lands on.
        seq_missed = 0
        this_seq = obj.get('seq')
        if isinstance(this_seq, int) and isinstance(expected_seq, int):
            if this_seq > expected_seq + 1:
                seq_missed = this_seq - expected_seq - 1
                log.warning(f"Missed {seq_missed} line(s) from the pump board "
                            f"(seq {expected_seq} -> {this_seq}).")
        if isinstance(this_seq, int):
            expected_seq = this_seq

        state = obj.get('state', 'unknown')
        if obj.get('ev') == 'pump':
            # `ms` is when this transition was declared and `prev_ms` when the state being
            # left began — two board timestamps, not a duration. Subtracting them here is
            # the contract (docs/pump.md, and what pump_tune.py's `listen` prints).
            held_ms = elapsed_ms(obj.get('prev_ms'), obj.get('ms'))
            duration_s = held_ms / 1000.0 if held_ms is not None else None
            log.info(f"Pump {state} (rms={obj.get('rms_counts')} freq={obj.get('freq_hz')} "
                     f"after {_format_since(held_ms)} {obj.get('prev_state')})")
            _check_duration(obj.get('prev_state'), duration_s, now)
            record_transition(state, now, 'event', obj,
                              prev_state=obj.get('prev_state'), duration_s=duration_s,
                              seq=this_seq, uptime_ms=obj.get('ms'), seq_missed=seq_missed)
            _warn_on_health(obj)
        elif obj.get('ev') == 'hb':
            log.debug(f"Heartbeat: {state} since {_format_since(obj.get('since_ms'))}")
            record_heartbeat(state, now, obj, seq=this_seq, uptime_ms=obj.get('ms'),
                             seq_missed=seq_missed)
            _warn_on_health(obj)
        else:
            log.warning(f"Unknown event kind {obj.get('ev')!r}: {obj}")


## How far the board's duration may differ from what this process observed before it is worth
## a line. The two are measured from different clocks and different instants — the board's
## from its own declaration times, ours from when the lines arrived — so a second or two of
## disagreement is normal. Anything larger is not a clock difference.
DURATION_TOLERANCE_S = 5.0
DURATION_TOLERANCE_FRAC = 0.05


def _check_duration(prev_state, duration_s, when):
    """Cross-check the board's duration against the one this daemon watched pass.

    Two independent measurements of the same interval: the board subtracts its own
    timestamps, and we hold the arrival time of the previous transition in the cursor. They
    should agree to well inside a second.

    This exists because reading `prev_ms` as a duration rather than a timestamp produced
    runs 4.5x too long that looked entirely plausible in Grafana — the numbers were the right
    order of magnitude and the state trace beside them was correct. Nothing downstream could
    have caught it. A disagreement here is the cheap signal that one side's idea of the
    contract has drifted from the other's, whichever side that turns out to be.
    """
    if duration_s is None:
        return
    saved = load_state()
    if saved.get('state') != prev_state:
        return          # the cursor is not tracking that state, so there is nothing to compare
    start = saved.get('state_start')
    if not isinstance(start, (int, float)):
        return

    observed = when - start
    tolerance = max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_FRAC * observed)
    if abs(observed - duration_s) > tolerance:
        log.warning(
            f"The board says {prev_state!r} lasted {duration_s:.1f} s, but this service "
            f"watched {observed:.1f} s pass since it started. One side is reading the "
            f"ms/prev_ms contract differently — the recorded pump_run duration is the "
            f"board's, and is wrong if the board is.")


## Counters that only mean something as a trend, so they are logged when they grow rather than
## on every heartbeat. Each maps to the one sentence saying what to do about it.
_HEALTH_COUNTERS = {
    'dropped': "lines the board could not send because this end was not reading — the "
               "transitions are still in its history buffer, but they arrived late",
    'n_freq_reject': "windows with a strong signal at the wrong frequency — induced hum or a "
                     "wiring fault, not the pump; check the routing of the sensor leads",
    'n_headroom': "windows where the waveform was flattened by the module's op-amp — turn the "
                  "gain pot down. n_clipped cannot see this: on a 3V3 supply the flattening "
                  "happens well below the ADC rails",
}
## Last value seen for each counter, or None before the first reading of this connection.
## Reset by _reset_health() on every connect — see why there rather than treating an unseen
## counter as zero.
_health_seen = {}


def _reset_health():
    """Forget the counter baselines, so the next reading re-establishes them."""
    _health_seen.clear()


def _warn_on_health(obj):
    """Log the board's counters: at INFO when first seen, at WARNING when they grow.

    The counters are cumulative since the BOARD's boot, and this process restarts far more
    often than the board does — every deploy touching sensors/ or common/ bounces the unit.
    So the first reading of a connection is a baseline to adopt, not a change to report: its
    value may have accumulated entirely before this process was watching, and warning about it
    would re-raise the same alarm on every deploy until the board next reboots.

    That matters beyond tidiness. WARNING lines are written to InfluxDB as `log` points, so a
    repeated false alarm both fills the database and teaches the reader to skim past exactly
    the counter — n_headroom — that means the signal is degrading and the gain pot needs
    turning down.
    """
    for name, meaning in _HEALTH_COUNTERS.items():
        value = obj.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            continue

        previous = _health_seen.get(name)
        if previous is None:
            # First sighting on this connection. Non-zero is worth stating — it is real, and
            # if it keeps climbing the next reading says so — but it is not news.
            if value:
                log.info(f"Pump board reports {name}={value} accumulated since its own boot "
                         f"(baseline; not necessarily during this session).")
        elif value > previous:
            log.warning(f"Pump board {name}={value} (+{value - previous}): {meaning}.")
        # Assigned unconditionally: a counter that went DOWN means the board rebooted, and the
        # new value is the baseline to compare against from here.
        _health_seen[name] = value


# -------------------------------------------------------------------------------------------------
# PUBLIC READ-ONLY VIEW (for the Telegram bot)

def current_state():
    """What the cursor says the pump is doing, without touching the serial port.

    The daemon holds the port open for its whole life, so nothing else can open it. This
    reads the file the daemon writes after every recorded line instead — which also means it
    answers instantly and works even while the board is unplugged.

    Returns a dict with `state`, `since_s`, `age_s` (how long since the daemon last heard
    anything — the number that says whether to trust the rest) and `stale`, or None if the
    daemon has never recorded anything.
    """
    saved = load_state()
    state = saved.get('state')
    if state is None:
        return None
    now = time.time()
    last_line = saved.get('last_line')
    age = now - last_line if isinstance(last_line, (int, float)) else None
    start = saved.get('state_start')
    return {
        'state':   state,
        'since_s': now - start if isinstance(start, (int, float)) else None,
        'age_s':   age,
        # Two heartbeats of slack: one missed line is ordinary, two means the daemon is not
        # hearing the board and the state above is a memory, not an observation.
        'stale':   age is None or age > 2 * HB_INTERVAL_S,
    }


# -------------------------------------------------------------------------------------------------
# SERVICE

def _handle_sigterm(signum, frame):
    """systemd stops and restarts this unit routinely (every deploy that touches sensors/).

    Exiting cleanly matters only for the log line: the cursor is already saved after every
    recorded write, so a hard kill loses at most the line currently in flight — and the board
    still holds that one in its history buffer.
    """
    global _stopping
    _stopping = True
    log.info("SIGTERM received; closing the pump link.")


def run():
    """Connect, resync, listen, reconnect. Runs until stopped."""
    backoff = BACKOFF_MIN_S

    while not _stopping:
        port = None
        try:
            port, status = connect()
            # `or`, not a .get default: a firmware that reports hb_ms as null would otherwise
            # divide None and take the whole service down on a field that is only advisory.
            hb_interval_s = (status.get('hb_ms') or HB_INTERVAL_S * 1000) / 1000.0
            seq = resync(port, status)
            backoff = BACKOFF_MIN_S      # a successful handshake resets the escalation
            listen(port, seq, hb_interval_s)

        except BoardRebooted as e:
            log.warning(f"{e}.")
            continue                      # immediate reconnect: nothing is wrong with the link

        except PumpPermanent as e:
            # Wrong firmware or the wrong board on the port. Reconnecting changes nothing, so
            # back off to the maximum rather than repeating the same complaint every 5 s.
            log.error(f"{e}")
            backoff = BACKOFF_MAX_S

        except boards.BoardError as e:
            log.warning(f"Pump board not available: {e}")
            backoff = min(backoff * 2, BACKOFF_MAX_S)

        except (LinkSilent, PumpError) as e:
            # A request abandoned mid-flight because we are shutting down is not a fault, and
            # saying so would put a misleading WARNING (and an InfluxDB `log` point) in the
            # journal on every ordinary deploy.
            if not _stopping:
                log.warning(f"Pump link lost: {e}")
            backoff = min(backoff * 2, BACKOFF_MAX_S)

        except serial.SerialException as e:
            log.warning(f"Serial error on the pump board: {e}")
            backoff = min(backoff * 2, BACKOFF_MAX_S)

        except Exception as e:
            log.error(f"Unexpected failure in the pump listener: {e}", exc_info=True)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

        finally:
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass

        if _stopping:
            break
        log.info(f"Reconnecting to the pump board in {backoff} s.")
        # Sleep in slices so SIGTERM does not have to wait out the whole backoff.
        waited = 0
        while waited < backoff and not _stopping:
            time.sleep(min(1, backoff - waited))
            waited += 1


if __name__ == '__main__':
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Record WARNING+ from this data path to InfluxDB, exactly as the scheduled sensors run
    # does. Attached here and not on import so the Telegram bot, which imports this module
    # for current_state(), never writes logs to the database.
    attach_influx_handler(write_api, INFLUXDB_BUCKET, INFLUXDB_ORG)

    log.info("Pump listener starting.")
    run()
    log.info("Pump listener stopped.")
