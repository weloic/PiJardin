###################################################################################################
# -------------------------------------------------------------------------------------------------
# How much water each pump run actually moved.
#
# WHY THIS IS MEASURED AND NOT MULTIPLIED
# ---------------------------------------
# The cistern feeds either irrigation — water leaves and does not come back — or a closed loop,
# where it circulates and returns minus whatever the loop leaks or sprays away. Those two produce
# the SAME pump run: same duration, same current, same everything this service can see at the
# board. `duration_s * a flow constant` would report the same volume for both and be confidently
# wrong half the time. The well level is what tells them apart, so the volume is read from the
# level on either side of the run, every run, with no rate constant anywhere in this file.
#
# WHAT IS ACTUALLY COMPUTED
# -------------------------
#   volume_l = (the level fell by this much, in litres) + whatever rain put back meanwhile
#
# The level at each end comes from the reading read_pump.py FORCES at the transition — the one
# measurement taken at exactly the instant that matters. It is used as it stands, unless the
# routine 5-minute readings either side of it disagree, in which case those are used instead.
# See THE LEVEL AT A TRANSITION below; there is no curve fitting anywhere in this file.
#
# The refill term is zero nearly always: the cistern gains water when it rains, and the pump is
# rarely running then. It is here for the case where the two coincide, because that is exactly
# where it flips the answer's sign — a closed-loop cycle losing a little while the rain puts back
# more ends with MORE water than it began with, and would otherwise be reported as a negative
# volume instead of as the small loss it was.
#
# WHY THIS IS A SWEEP AND NOT A CALCULATION PER RUN
# -------------------------------------------------
# read_pump.py calls sweep() as soon as it has written the level reading forced at a pump stop —
# so in normal running this computes one run, immediately, and there is no timer anywhere.
#
# But what it asks is "which runs have no volume yet?", not "compute the run that just ended",
# and that difference is the whole robustness story. A run whose calculation never happened —
# the deploy timer restarted pump.service in the middle, the Pi lost power, InfluxDB was down —
# is simply still unanswered, and the next pump cycle picks it up. The same is true of the runs
# replayed out of the board's history buffer after any downtime, which arrive in a batch and
# have no forced edge readings at all: they are runs with no volume, like any other.
#
# It also means the estimator can be changed and the history rebuilt (--recompute), which will
# happen: several constants in here are first guesses.
#
# The cost of having no timer is that nothing runs while the pump is idle, so a run missed just
# before a long idle stays missing until the next cycle. `/pertes` shows what is outstanding.
#
# WHAT IT CANNOT DO
# -----------------
# At PUIT_M3_PER_CM the level moves 40 litres per centimetre, so a loss small enough gets lost in
# the sensor's own wobble. That is what volume_sigma_l is on the point to say out loud, rather
# than printing a confident-looking figure the readings cannot support.
#
# And if rain starts or stops DURING a run, the refill rate measured from the quiet stretch before
# it is the wrong one, and nothing here can tell. Rare, since the pump rarely runs in the rain.
#
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import math
import logging
import argparse
import datetime

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

## Repo root on sys.path for the `common` package; when run as a script only sensors/ is on it.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

## The puit sensor, for the well geometry. Imported rather than copied so a corrected
## PUIT_EMPTY_DISTANCE_CM or PUIT_M3_PER_CM reaches this file too — the same reason the Grafana
## dashboards spell the conversion out and this one does not.
import read_puit

## Telegram notifications. Guarded: a sweep that cannot tell anyone the answer must still record
## it, so a missing or broken bot module costs the message and nothing else.
sys.path.insert(0, os.path.join(_REPO, 'telegram_bot'))
try:
    import alerts
except Exception as _e:                # noqa: BLE001 - anything here means "no notifications"
    alerts = None
    logging.getLogger('pump_volume').warning(
        f"Could not import alerts ({_e}); pump cycles will not be notified.")

from common.logging_setup import setup_logging, attach_influx_handler

log = logging.getLogger('pump_volume')

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Influx database — same bucket and lazy-client pattern as the two sensors.
INFLUXDB_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG    = os.getenv("INFLUX_ORG",    "PiJardin")
INFLUXDB_BUCKET = os.getenv("INFLUX_BUCKET", "puit")

try:
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    query_api = client.query_api()
except Exception as e:
    log.warning(f"Could not initialize InfluxDB client: {e}; nothing will be computed.")
    write_api = None
    query_api = None

## Which pump these runs belong to. Matches read_pump.PUMP.
PUMP = 'puit'

## Where the costed runs are written. One point per pump cycle, so it sits beside `pump_state`
## and `pump_run` and reads the same way.
##
## It used to be `pump_volume`, and that name is now abandoned rather than reused. `quality`
## started life as a TAG there, and tags are part of a point's identity in InfluxDB: the day it
## became a field, every run already recorded kept its old tagged series and new writes landed
## BESIDE the old ones rather than on top — the same cycle listed twice, for ever. Deleting the
## strays needs a token with delete permission, which this deployment's has not got. A new name
## costs nothing, is clean from the first write, and leaves the handful of orphans to expire with
## the bucket's retention. Because this measurement is a cache and every input for it is still
## in InfluxDB, starting a fresh one loses precisely nothing.
MEASUREMENT = 'pump_cycle'

## Litres per centimetre of level. Derived, never spelled out: read_puit owns the well geometry.
L_PER_CM = read_puit.PUIT_M3_PER_CM * 1000.0

## How late the reading forced at a transition may be and still count as that transition's own.
## The worker in read_pump.py normally answers within seconds; it is only slow when the scheduled
## measurement is holding the puit flock. Two minutes covers that without letting an unrelated
## routine reading five minutes later masquerade as the edge one.
EDGE_S = 120

## How far outside its neighbours the forced reading may sit before it is disbelieved. The level
## genuinely moves between two routine readings, so this is applied on top of the range they
## span, not to the reading in isolation — see boundary_level(). Two centimetres is 80 litres:
## loose enough that an honest reading during a fast run still passes, tight enough to catch a
## stray echo or a surface the returning loop has stirred up.
COHERENCE_CM = 2.0

## The quiet stretch before a run, used to measure how fast the cistern is filling on its own.
## Clamped to the actual off period, so a reading from an earlier pumping run can never leak in.
QUIET_WINDOW_S = 2 * 3600

## Shortest quiet stretch worth deriving a refill rate from. Two readings ten minutes apart with
## the sensor's own wobble on each already give a rate good to a few litres over a run; anything
## shorter says more about the noise than about the rain.
REFILL_MIN_SPAN_S = 10 * 60

## How long after a run ends before it is worth computing, for a caller that cannot know whether
## the closing edge reading has landed yet. read_pump.py, which triggers the normal sweep, passes
## 0 — it calls this only once its own edge measurement has been written, so there is nothing
## left to wait for. This default is for a manual or backfilling run.
SETTLE_S = 120

## Default sweep window, in seconds. Long enough that a Pi that was down, or a batch of runs
## replayed out of the board's history buffer, is caught up rather than left as a hole.
DEFAULT_SINCE_S = 24 * 3600

## Stand-in for the sensor's noise when it cannot be measured from the data itself. Deliberately
## pessimistic — 1 cm is 40 litres — so a volume reports a large uncertainty rather than a
## confident one when we do not actually know how steady the readings were.
FALLBACK_SIGMA_CM = 1.0

## Fields written, and the type each must keep for ever (InfluxDB rejects a field that changes
## type). volume_l is the headline; the rest is what makes a surprising row checkable without
## re-running anything.
##
## `quality` is a FIELD and not a tag, which looks wrong until you consider the recompute below.
## A run is first costed seconds after it ends, before the routine reading that would vouch for
## its closing level exists, so it starts out `coarse` and is redone once that reading arrives.
## Tags are part of a point's identity: rewriting it with quality='ok' would create a SECOND
## series rather than replacing the first, and the run would appear twice. As a field it is
## simply overwritten.
VOLUME_FIELDS = (
    ('volume_l', float), ('rate_l_per_h', float), ('volume_sigma_l', float),
    ('volume_drop_l', float), ('refill_l_per_s', float), ('duration_s', float),
    ('level_start_cm', float), ('level_end_cm', float), ('n_quiet', int),
    ('quality', str),
)

## How long after a run ends its inputs are still arriving. A run costed immediately cannot have
## its closing level checked — the routine reading after the transition has not happened yet — so
## anything not yet `ok` is redone while it is younger than this. Past it, every reading that will
## ever exist does, and whatever quality it has is final.
RECHECK_HORIZON_S = 30 * 60


# -------------------------------------------------------------------------------------------------
# TIME

def _utc(timestamp):
    """A UTC-aware datetime from a POSIX timestamp (the client reads naive ones as UTC)."""
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def _rfc3339(timestamp):
    """A POSIX timestamp as a Flux range literal."""
    return _utc(timestamp).isoformat().replace('+00:00', 'Z')


# -------------------------------------------------------------------------------------------------
# THE SENSOR'S OWN NOISE

def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


## Turning a spread of successive differences into a standard deviation. If consecutive readings
## carry independent noise of sigma, their difference has sigma*sqrt(2), and for normal noise
## median|d| = 0.6745*sigma*sqrt(2) and mean|d| = 0.7979*sigma*sqrt(2). Dividing by these
## recovers sigma. Differencing first is what makes this work on a level that is drifting.
_MEDIAN_TO_SIGMA = 1.0 / (0.6745 * math.sqrt(2.0))
_MEAN_TO_SIGMA = 1.0 / (0.7979 * math.sqrt(2.0))


def pooled_sigma_cm(samples):
    """Estimate the sensor's noise from successive differences of a quiet series, in cm.

    The median is the robust estimator and is tried first. But this sensor is QUANTISED and the
    cistern is usually still, so most consecutive readings come back byte-identical and the
    median of the differences is exactly zero — which is not "no information", it is "the noise
    is smaller than the step size". Measured on the real series: 453 quiet samples, median 0.

    So a zero median falls through to the mean, which the handful of readings that did move still
    move off zero. Only if literally every reading is identical is there nothing to say, and then
    the caller uses FALLBACK_SIGMA_CM.
    """
    diffs = [abs(samples[i + 1][1] - samples[i][1]) for i in range(len(samples) - 1)]
    if len(diffs) < 4:
        return None

    median = _median(diffs)
    if median is not None and median > 0:
        return median * _MEDIAN_TO_SIGMA

    mean = sum(diffs) / len(diffs)
    if mean > 0:
        return mean * _MEAN_TO_SIGMA
    return None


# -------------------------------------------------------------------------------------------------
# THE LEVEL AT A TRANSITION
#
# read_pump.py forces a reading the moment the pump switches. That is the one measurement taken at
# exactly the instant we care about, so it is what gets used — unless the routine readings either
# side of it say it cannot be right.
#
# The test is a BRACKET, not a comparison against one value. Between the routine reading before
# the transition and the routine reading after it, the level has genuinely moved, and by a lot on
# a fast run — so any value inside the range those two span (plus COHERENCE_CM) is plausible, and
# only something outside it is evidence of a bad reading. That is what catches a stray echo, or a
# surface the returning loop has stirred into ripples.
#
# A rejected reading is not fatal: the level is taken from those same routine readings instead,
# interpolated to the transition time. Less precise, said so in `quality`, but never wrong in the
# way one bad echo would be.

def _neighbours(levels, t, skip):
    """The nearest readings either side of `t`, ignoring index `skip` (the forced one)."""
    before = after = None
    for index, sample in enumerate(levels):
        if index == skip:
            continue
        if sample[0] < t:
            before = sample
        elif after is None:
            after = sample
    return before, after


def _interpolate(before, after, t):
    """What the neighbours imply the level was at `t`, and how far apart they are."""
    if before is not None and after is not None:
        span = after[0] - before[0]
        if span <= 0:
            return before[1], abs(after[1] - before[1])
        fraction = (t - before[0]) / span
        return before[1] + fraction * (after[1] - before[1]), abs(after[1] - before[1])
    if before is not None:
        return before[1], None
    if after is not None:
        return after[1], None
    return None, None


def boundary_level(levels, t, sigma_floor):
    """The level at instant `t`. Returns (level_cm, sigma_cm, source), source being:

    'forced'     the reading taken at the transition; its neighbours agree it is plausible
    'unchecked'  that reading, with no routine reading on BOTH sides to test it against
    'routine'    it was rejected, or never arrived; the neighbours were used instead
    'none'       nothing to go on at all
    """
    forced_index, closest = None, EDGE_S
    for index, (timestamp, _) in enumerate(levels):
        if abs(timestamp - t) <= closest:
            closest = abs(timestamp - t)
            forced_index = index

    before, after = _neighbours(levels, t, forced_index)
    expected, spread = _interpolate(before, after, t)
    forced = levels[forced_index][1] if forced_index is not None else None

    ## Falling back to the neighbours means interpolating a straight line across the very instant
    ## the level changed direction, so the answer can be off by as much as the neighbours are
    ## apart — not half that. The whole spread is the honest bar.
    if forced is None:
        if expected is None:
            return None, None, 'none'
        return expected, max(sigma_floor, spread or 0.0), 'routine'

    if before is None or after is None:
        # One neighbour cannot say whether a reading is wrong, only that the level changed —
        # which it was always going to. Believe it, and record that nothing vouched for it.
        return forced, sigma_floor, 'unchecked'

    low, high = min(before[1], after[1]), max(before[1], after[1])
    if low - COHERENCE_CM <= forced <= high + COHERENCE_CM:
        return forced, sigma_floor, 'forced'

    log.warning(f"The reading forced at {_rfc3339(t)} says {forced:.1f} cm, outside the "
                f"[{low:.1f}, {high:.1f}] cm spanned by the readings either side (+/- "
                f"{COHERENCE_CM} cm). Using {expected:.1f} cm from those instead.")
    return expected, max(sigma_floor, spread), 'routine'


# -------------------------------------------------------------------------------------------------
# HOW FAST THE CISTERN FILLS ON ITS OWN

def refill_l_per_s(levels, quiet_start, t0, sigma_floor):
    """How fast the cistern is gaining water while nothing pumps. Returns (l_per_s, sigma, n).

    It collects rain, so this is zero most of the time and can be substantial during a shower.
    Measured per run from the quiet stretch before it — first reading against last, nothing
    fitted — because it is weather, not a property of the cistern.

    It matters most exactly where the volume is smallest. A closed-loop cycle that loses a little
    while the rain puts back more ends with MORE water than it started with, and without this the
    run would be reported as a negative volume instead of as the small loss it was.
    """
    quiet = [s for s in levels if quiet_start <= s[0] < t0]
    if len(quiet) < 2:
        return 0.0, 0.0, len(quiet)

    first, last = quiet[0], quiet[-1]
    span = last[0] - first[0]
    if span < REFILL_MIN_SPAN_S:
        return 0.0, 0.0, len(quiet)

    # The distance to the water shrinks as the water rises, hence the sign. The + 0.0 turns a
    # negative zero back into a zero, so a flat cistern logs "0.0000" and not "-0.0000".
    rate = -(last[1] - first[1]) / span * L_PER_CM + 0.0
    sigma = sigma_floor * math.sqrt(2.0) / span * L_PER_CM
    return rate, sigma, len(quiet)


# -------------------------------------------------------------------------------------------------
# THE ESTIMATE
#
# Pure arithmetic over a list of readings, deliberately free of any InfluxDB call, so the whole
# estimator can be exercised offline against synthetic runs.

def estimate_run(t0, t1, truncated, levels, quiet_start, sigma_floor):
    """Compute one run's volume. Returns (fields, quality), or None if nothing can be said.

    `levels` is every reading in range, sorted by time; this picks out what it needs.
    `quiet_start` is when the off period before the run began, so that no reading from an earlier
    pumping run can reach the refill estimate.
    """
    duration_s = t1 - t0
    if duration_s <= 0:
        return None

    start_cm, sigma_start, start_source = boundary_level(levels, t0, sigma_floor)
    end_cm, sigma_end, end_source = boundary_level(levels, t1, sigma_floor)
    if start_cm is None or end_cm is None:
        log.debug(f"Run at {_rfc3339(t0)} has no usable level either side; leaving it alone.")
        return None

    rate, sigma_rate, n_quiet = refill_l_per_s(levels, quiet_start, t0, sigma_floor)

    ## The distance to the water grows as the water falls, so this is positive for a run that
    ## took water out.
    drop_l = (end_cm - start_cm) * L_PER_CM
    volume_l = drop_l + rate * duration_s

    sigma_l = math.sqrt((sigma_start * L_PER_CM) ** 2
                        + (sigma_end * L_PER_CM) ** 2
                        + (sigma_rate * duration_s) ** 2)

    ## What the number rests on, so a surprising row can be judged without re-deriving it — the
    ## same job `truncated` and `seq_missed` already do on the pump series. A truncated run is
    ## degraded whatever the levels did: its duration is only a lower bound.
    if truncated:
        quality = 'degraded'
    elif start_source == 'forced' and end_source == 'forced':
        quality = 'ok'
    else:
        quality = 'coarse'

    return {
        'volume_l':       volume_l,
        'rate_l_per_h':   volume_l / duration_s * 3600.0,
        'volume_sigma_l': sigma_l,
        'volume_drop_l':  drop_l,
        'refill_l_per_s': rate,
        'duration_s':     duration_s,
        'level_start_cm': start_cm,
        'level_end_cm':   end_cm,
        'n_quiet':        n_quiet,
        'quality':        quality,
    }, quality



# -------------------------------------------------------------------------------------------------
# INFLUXDB

def query_runs(since, until):
    """Every pump_run in the window, sorted by start. Returns [(start, state, duration, trunc)]."""
    flux = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {_rfc3339(since)}, stop: {_rfc3339(until)})\n'
        '  |> filter(fn: (r) => r._measurement == "pump_run")\n'
        '  |> filter(fn: (r) => r._field == "duration_s")\n'
        '  |> keep(columns: ["_time", "_value", "state", "truncated"])\n'
        '  |> sort(columns: ["_time"])'
    )
    runs = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            runs.append((
                record.get_time().timestamp(),
                record.values.get('state'),
                float(record.get_value()),
                str(record.values.get('truncated')).lower() == 'true',
            ))
    runs.sort(key=lambda r: r[0])
    return runs


def query_levels(since, until):
    """Every well-level reading in the window, sorted. Returns [(timestamp, cm)]."""
    flux = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {_rfc3339(since)}, stop: {_rfc3339(until)})\n'
        '  |> filter(fn: (r) => r._measurement == "height_measure")\n'
        '  |> filter(fn: (r) => r._field == "lenght_median")\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        '  |> sort(columns: ["_time"])'
    )
    levels = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            levels.append((record.get_time().timestamp(), float(record.get_value())))
    levels.sort(key=lambda s: s[0])
    return levels


def query_computed(since, until):
    """Runs that already carry a costed point: {start timestamp: quality}.

    The quality is what decides whether a run is left alone or redone — see sweep().
    """
    flux = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {_rfc3339(since)}, stop: {_rfc3339(until)})\n'
        f'  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")\n'
        '  |> filter(fn: (r) => r._field == "quality")\n'
        '  |> keep(columns: ["_time", "_value"])'
    )
    done = {}
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            done[round(record.get_time().timestamp())] = record.get_value()
    return done


## Numbers on a costed run, and the one text field beside them. Read apart, deliberately — see
## query_recent.
RECENT_NUMERIC = ('volume_l', 'rate_l_per_h', 'volume_sigma_l', 'duration_s')


def query_recent(limit=5, since='-30d'):
    """The last `limit` costed runs, newest last. Returns [{'time', 'quality', <fields>}].

    TWO queries, because `quality` is a string and everything else is a float. Flux gives each
    _field its own table, and a column can hold only one type — so folding them into a single
    `_value` column (a keep() plus a group(), which is the obvious way to write this) is a schema
    collision that fails at query time, not at write time. Reading the text separately and
    matching on the timestamp cannot collide, and this is a Telegram command, not a hot path.
    """
    if query_api is None:
        return []

    predicate = ' or '.join(f'r._field == "{name}"' for name in RECENT_NUMERIC)
    numeric = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {since})\n'
        f'  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")\n'
        f'  |> filter(fn: (r) => {predicate})\n'
        '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> sort(columns: ["_time"])\n'
        f'  |> tail(n: {int(limit)})'
    )
    text = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {since})\n'
        f'  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")\n'
        '  |> filter(fn: (r) => r._field == "quality")\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        '  |> sort(columns: ["_time"])'
    )

    qualities = {}
    for table in query_api.query(text, org=INFLUXDB_ORG):
        for record in table.records:
            qualities[record.get_time()] = record.get_value()

    ## Keyed by timestamp, so one run can only ever produce one row. Belt and braces: a run IS
    ## one point, but the day `quality` moved from a tag to a field the old series stayed behind
    ## and every affected run listed twice. delete_computed clears that up; this makes sure no
    ## future split can put a duplicate in front of anyone again.
    rows = {}
    for table in query_api.query(numeric, org=INFLUXDB_ORG):
        for record in table.records:
            row = {'time': record.get_time(), 'quality': qualities.get(record.get_time())}
            for name in RECENT_NUMERIC:
                row[name] = record.values.get(name)
            rows[record.get_time()] = row
    return sorted(rows.values(), key=lambda r: r['time'])[-int(limit):]


def write_volume_point(start, fields, quality):
    """Record one costed cycle, timestamped at the run's START like pump_run itself.

    Same timestamp and same convention as the run it describes, so the two line up without a
    join and a re-run overwrites its own previous answer instead of accumulating versions.
    """
    if write_api is None:
        log.warning("No InfluxDB write API; pump volume not recorded.")
        return False

    point = Point(MEASUREMENT).tag('pump', PUMP).field('quality', quality)
    for name, cast in VOLUME_FIELDS:
        if name == 'quality':
            continue                       # written above; it is the only non-numeric field
        value = fields.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and not math.isnan(value) and not math.isinf(value):
            point.field(name, cast(value))
    point.time(_utc(start), WritePrecision.S)

    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        log.error(f"Could not write pump volume to InfluxDB: {e}")
        return False


# -------------------------------------------------------------------------------------------------
# THE SWEEP

def _between(samples, start, end):
    """Samples with start <= t <= end. The lists here are small; a scan is the whole algorithm."""
    return [s for s in samples if start <= s[0] <= end]


## How far past the end of a run to keep readings. The stop boundary needs the routine reading
## AFTER the transition to check the forced one against, and routine readings are 5 minutes apart.
TAIL_S = 10 * 60


def sweep(since_s, recompute=False, now=None, settle_s=SETTLE_S, notify=False):
    """Compute every ON run in the window that has no volume yet. Returns (written, skipped).

    Asking "which runs have no volume?" of the database rather than remembering it is what makes
    this safe to call from anywhere and as often as you like: it backfills, it recovers whatever
    a restart interrupted, and calling it twice does nothing the second time.

    `notify` sends a Telegram message for each run costed for the FIRST time. Only read_pump.py
    passes it, because only there does a costed run mean "the pump has just stopped". A manual
    sweep or a backfill would otherwise announce a week of old cycles at once.
    """
    if query_api is None:
        log.error("No InfluxDB query API; cannot compute pump volumes.")
        return 0, 0

    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    since = now - since_s

    runs = query_runs(since, now)
    if not runs:
        log.info("No pump runs in the window; nothing to compute.")
        return 0, 0

    ## Level history has to reach back past the earliest run by the whole quiet window. Fetched
    ## once for the whole sweep rather than per run.
    levels = query_levels(runs[0][0] - QUIET_WINDOW_S, now)
    done = {} if recompute else query_computed(since, now)

    ## The sensor's own noise, measured from the series rather than assumed. Successive
    ## differences over the quiet stretches: the runs are where the level is deliberately moving,
    ## so including them would inflate this into meaninglessness.
    on_spans = [(t, t + d) for t, state, d, _ in runs if state == 'on']
    quiet = [s for s in levels if not any(a <= s[0] <= b for a, b in on_spans)]
    sigma_floor = pooled_sigma_cm(quiet) or FALLBACK_SIGMA_CM
    log.info(f"Sensor noise floor for this sweep: {sigma_floor:.2f} cm "
             f"({sigma_floor * L_PER_CM:.0f} L) from {len(quiet)} quiet samples.")

    written = skipped = 0
    for index, (t0, state, duration_s, truncated) in enumerate(runs):
        if state != 'on':
            continue
        t1 = t0 + duration_s
        if t1 > now - settle_s:
            log.debug(f"Run at {_rfc3339(t0)} ended too recently; leaving it for the next sweep.")
            skipped += 1
            continue
        ## Already costed? Leave it alone once it is `ok`, or once every reading that could
        ## improve it has had time to arrive. In between — the usual case for a run costed
        ## seconds after it ended, whose closing level nothing had vouched for yet — redo it.
        existing = done.get(round(t0))
        if existing is not None:
            if existing == 'ok' or (now - t1) > RECHECK_HORIZON_S:
                skipped += 1
                continue
            log.debug(f"Run at {_rfc3339(t0)} is {existing!r}; rechecking now that more "
                      f"readings have arrived.")

        ## No reading from an earlier pumping run may reach the quiet window the refill rate is
        ## measured over. Runs tile the timeline, so the previous run's start is exactly where
        ## this off period began.
        floor_t = runs[index - 1][0] if index > 0 else t0 - QUIET_WINDOW_S
        quiet_start = max(t0 - QUIET_WINDOW_S, floor_t)

        result = estimate_run(
            t0, t1, truncated,
            levels=_between(levels, quiet_start, t1 + TAIL_S),
            quiet_start=quiet_start,
            sigma_floor=sigma_floor,
        )
        if result is None:
            skipped += 1
            continue

        fields, quality = result
        if write_volume_point(t0, fields, quality):
            written += 1
            log.info(f"Run at {_rfc3339(t0)} lasting {duration_s / 60:.1f} min moved "
                     f"{fields['volume_l']:.0f} +/- {fields['volume_sigma_l']:.0f} L "
                     f"({fields['rate_l_per_h']:.0f} L/h, {quality}).")

            ## Only when this run had no point before: the recheck below writes it a second
            ## time, and two messages about one cycle would be worse than a quiet improvement.
            ## `notify` is off for a backfill, where every run is historic and a burst of
            ## messages about last week would be nothing but noise.
            if notify and existing is None and alerts is not None:
                try:
                    alerts.notify_pump_run(fields['volume_l'], fields['volume_sigma_l'],
                                           duration_s, fields['rate_l_per_h'], quality)
                except Exception as e:
                    log.warning(f"Could not notify the pump cycle at {_rfc3339(t0)}: {e}")
        else:
            skipped += 1

    return written, skipped


# -------------------------------------------------------------------------------------------------
# SCHEDULING

def _parse_since(text):
    """A duration literal ('24h', '30d', '90m') as seconds. A leading '-' is tolerated.

    Note that argparse cannot take the leading-dash form with a SPACE — `--since -30d` reads
    as an option, not a value — so the documented spelling drops the dash. `--since=-30d` also
    works, for anyone copying a Flux range literal.
    """
    units = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    body = text.lstrip('-')
    if not body or body[-1] not in units or not body[:-1].isdigit():
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a duration like 24h, 7d or 90m.")
    return int(body[:-1]) * units[body[-1]]


if __name__ == '__main__':
    setup_logging()

    parser = argparse.ArgumentParser(description=__doc__)
    # The default is already in seconds, and argparse only applies `type` to a string default.
    parser.add_argument('--since', default=DEFAULT_SINCE_S, type=_parse_since,
                        help="how far back to sweep, e.g. 24h, 7d, 90d (default: 24h)")
    parser.add_argument('--recompute', action='store_true',
                        help="rewrite runs that already have a volume, instead of skipping them")
    args = parser.parse_args()

    # WARNING+ from this data path goes to InfluxDB, exactly as the two sensors do. Attached
    # here and not on import so nothing that merely imports this module writes log points.
    attach_influx_handler(write_api, INFLUXDB_BUCKET, INFLUXDB_ORG)

    try:
        written, skipped = sweep(args.since, recompute=args.recompute)
    except Exception as e:
        log.error(f"Pump volume sweep failed: {e}", exc_info=True)
        sys.exit(1)

    log.info(f"Pump volume sweep done: {written} written, {skipped} skipped.")
