###################################################################################################
"""Dump the InfluxDB bucket to line protocol, for replaying the Grafana dashboard off the Pi.

There is no route into this Pi but Telegram, so the dashboard cannot be opened where the data
lives. This module answers that by moving the *data* instead of a picture of it: the export
reloads into any InfluxDB, and `grafana/` in this repo provisions the real dashboard against it —
same panel JSON, same Flux, same datasource uid. What you then look at is Grafana, not a
reimplementation of it, so it cannot disagree with the Pi about what the data says.

**Line protocol, not the annotated CSV** `query_raw()` would hand over for free. That CSV carries
Flux's own `_start`/`_stop`/`result`/`table` columns, which `influx write` reinterprets as fields —
an import to repair by hand every time. Line protocol is the native ingest format and needs
nothing but an HTTP POST on the other side.

**Memory is the constraint that shapes the rest.** This runs on a Pi 3 Model B with ~380 MB
available and ~240 MB of swap already in use at rest, and `influxd` allocates to serve a query
just as we allocate to consume it. So nothing here scales with the window asked for: the
measurements are queried a day at a time, records are streamed one by one, each is compressed
and dropped immediately, and the compressed bytes go straight into a file the caller owns rather
than into a buffer — a 45 MB `BytesIO` at the end would have undone all three. Ask for 30 days
and the slice count grows while the footprint does not.
"""
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import gzip
import math
import logging
import datetime

## Same path dance as bot.py: read_puit lives in sensors/. Repeated here rather than relied upon
## so this module can be imported and exercised on its own.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_REPO, os.path.join(_REPO, 'sensors')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

## The InfluxDB connection is read through read_puit rather than built again here. sensors/
## already owns the URL, token, org and bucket, and already guards a failed construction by
## setting the API to None — a third copy of that setup (read_puit and pump_volume have one
## each) would be a third place to fix the day the credentials move.
import read_puit

log = logging.getLogger(__name__)

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Every measurement the dashboard reads, plus the two that explain a hole in it. `log` and
## `version` cost a handful of points a day and are what turn "the curve stops here" into "the
## curve stops here because that deploy landed" or "...because this warning fired".
MEASUREMENTS = (
    'height_measure',   # the well level: every panel except the pump table
    'pump_state',       # the on/off trace overlaid on the three history panels
    'pump_run',         # one point per state that ended, carrying duration_s
    'pump_cycle',       # the costed runs behind « Eau utilisée / perdue »
    'log',              # WARNING+ from the scheduled sensors data path
    'version',          # deploy markers, usable as a Grafana annotation
)

## How much time one query covers. A day of the densest measurement (pump_state, one heartbeat
## a minute) is ~1400 points — bounded regardless of what the caller asked for.
SLICE = datetime.timedelta(days=1)

## Telegram refuses a document over 50 MB. Stop short of it and say so, rather than spending
## minutes on a Pi 3 building a file the API will reject on arrival.
MAX_GZIP_BYTES = 45 * 1024 * 1024

## Columns Flux adds that are not part of the point. Everything else in a record IS a tag.
## Derived rather than enumerated on purpose: the tag set differs per measurement (`resampled`
## on one, `source` on another, `level`+`source` on log), and a tag missing from an export
## changes the identity of its point — which changes the dashboard. This way a tag added later
## is exported without touching this file.
RESERVED_COLUMNS = frozenset({
    '_time', '_value', '_field', '_measurement', '_start', '_stop', 'result', 'table',
})

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class ExportTooLarge(Exception):
    """The gzipped export passed MAX_GZIP_BYTES; the caller should narrow the window."""


# -------------------------------------------------------------------------------------------------
# LINE PROTOCOL

## Escaping is per-position in line protocol, not global, so these are three functions rather
## than one: escaping a character that does not need it in that position leaves the backslash
## in the parsed value. Mostly defensive here — the measurement names are literals above and
## the tag values are short identifiers — except for `log`'s `message` field, which is arbitrary
## text and is exactly where a single-function version would corrupt an import.

def _escape_measurement(text):
    """A measurement name: comma and space."""
    return str(text).replace(',', r'\,').replace(' ', r'\ ')


def _escape_tag(text):
    """A tag key, tag value or field key: comma, equals and space."""
    return str(text).replace(',', r'\,').replace('=', r'\=').replace(' ', r'\ ')


def _escape_string_field(text):
    """A string field value, which sits inside double quotes: backslash and quote."""
    return str(text).replace('\\', '\\\\').replace('"', '\\"')


def _field_value(value):
    """Encode a field value with its InfluxDB type preserved, or None if it has none.

    The type matters more than it looks. Write a float that happens to be whole as `3` and
    InfluxDB records the field as an integer, then **rejects** the next `3.5` — the series
    develops holes, and nothing in Grafana says why. `repr` on a float always produces a
    decimal point or an exponent, which is what keeps `lenght_median` a float on a reading
    that lands exactly on a centimetre.

    bool is tested before int because in Python a bool *is* an int: without this, `resampled`
    as a field would be written `1i`.
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return f'{value}i'
    if isinstance(value, float):
        # NaN and infinity have no line protocol representation; a line carrying one would be
        # rejected, and a rejected line takes its whole batch with it.
        return repr(value) if math.isfinite(value) else None
    return '"' + _escape_string_field(value) + '"'


def _nanoseconds(when):
    """Epoch nanoseconds, computed in integers.

    `when.timestamp() * 1e9` looks equivalent and is not: a float64 carries ~16 significant
    digits where current epoch nanoseconds need 19, so that route quietly rounds. This is
    exact to datetime's own microsecond resolution — six orders of magnitude finer than the
    5-minute cadence of the data, and unable to collide two points that were distinct.
    """
    delta = when - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10 ** 9 + delta.microseconds * 1000


def record_to_line(record, redact=None):
    """One Flux record as one line protocol line, or None if it cannot be represented.

    **One line per field, not one per point.** Line protocol allows either, and InfluxDB merges
    them because a point's identity is measurement + tag set + timestamp — so the two import
    identically. Grouping the fields of a point would mean holding records back in a dict keyed
    by (tags, timestamp) until the timestamp is complete, which is precisely the buffer that
    grows with the request that this module is built to avoid. gzip collapses the repeated tag
    text anyway, so the file is barely larger for it.
    """
    field = record.values.get('_field')
    value = record.get_value()
    if field is None or value is None:
        return None

    ## Strings are the only values that can carry a secret, and `log.message` holds arbitrary
    ## WARNING+ text — a traceback from the InfluxDB client has put a token in that field's
    ## reach before. Redaction is injected rather than imported so this module does not have to
    ## import bot.py, which imports this one.
    if redact is not None and isinstance(value, str):
        value = redact(value)

    encoded = _field_value(value)
    if encoded is None:
        return None

    key = _escape_measurement(record.values['_measurement'])
    ## Sorted so the output is deterministic and diffable; line protocol does not care.
    for tag_key, tag_value in sorted(record.values.items()):
        if tag_key in RESERVED_COLUMNS or tag_value is None:
            continue
        text = str(tag_value)
        ## InfluxDB drops a tag whose value is empty, so writing one would change the point's
        ## identity instead of preserving it. Skipping keeps the round trip honest.
        if not text:
            continue
        key += f',{_escape_tag(tag_key)}={_escape_tag(text)}'

    return f'{key} {_escape_tag(field)}={encoded} {_nanoseconds(record.get_time())}'


# -------------------------------------------------------------------------------------------------
# QUERY

def _rfc3339(when):
    """A datetime as a Flux range literal, microseconds kept.

    Truncating to the second would drop whatever sits in the truncated fraction at a slice
    boundary — `range` excludes its stop, so a point there would fall between two slices and
    silently not be exported.
    """
    return when.astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def _flux(measurement, start, stop, include_heartbeats):
    """The narrowest query that still returns every tag column.

    No `keep()`, `pivot()` or `group()`: each of those drops or folds columns, and the tags are
    read back off the record generically. A `pivot` would additionally collide `pump_cycle`'s
    string field `quality` with the floats beside it — a type collision Flux rejects at query
    time (see sensors/pump_volume.py:query_recent).

    Interpolated rather than parameterised, like the rest of the Flux in this repo: the
    measurement names are literals in MEASUREMENTS above and the bounds are formatted
    datetimes. Nothing here reaches the query from a Telegram message.
    """
    flux = (
        f'from(bucket: "{read_puit.INFLUXDB_BUCKET}")\n'
        f'  |> range(start: {_rfc3339(start)}, stop: {_rfc3339(stop)})\n'
        f'  |> filter(fn: (r) => r._measurement == "{measurement}")'
    )
    if measurement == 'pump_state' and not include_heartbeats:
        ## `not exists` first: a bare `r.source != "heartbeat"` evaluates to null on a point
        ## that somehow lacks the tag, and a null predicate filters the point out — dropping
        ## data while looking like it kept it.
        flux += '\n  |> filter(fn: (r) => not exists r.source or r.source != "heartbeat")'
    return flux


def build_export(sink, window, include_heartbeats=True, redact=None, now=None):
    """Query every measurement over `window`, writing gzipped line protocol into `sink`.

    sink: a writable binary file object. Taken from the caller rather than returned as bytes
      on purpose — a 45 MB buffer built in memory would undo the streaming above, which is the
      whole reason this module queries a day at a time. The caller passes a temporary file and
      nothing here grows with the window.
    window: a timedelta looking back from `now` (default: this instant, UTC).

    Returns {measurement: lines emitted}, for telling the caller what is in the file.

    Raises ExportTooLarge if the result passes MAX_GZIP_BYTES, and RuntimeError if InfluxDB
    is unreachable.
    """
    query_api = read_puit.query_api
    if query_api is None:
        raise RuntimeError("No InfluxDB query API — check INFLUXDB_TOKEN, then /logs bot.")

    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = now - window

    counts = {}
    ## mtime=0: the header timestamp is the only thing that would make two exports of the same
    ## window differ byte for byte, which is a nuisance when checking one against another.
    with gzip.GzipFile(fileobj=sink, mode='wb', mtime=0) as gz:
        for measurement in MEASUREMENTS:
            counts[measurement] = 0
            slice_start = start
            while slice_start < now:
                slice_stop = min(slice_start + SLICE, now)
                flux = _flux(measurement, slice_start, slice_stop, include_heartbeats)

                for record in query_api.query_stream(flux, org=read_puit.INFLUXDB_ORG):
                    line = record_to_line(record, redact)
                    if line is None:
                        continue
                    gz.write(line.encode('utf-8'))
                    gz.write(b'\n')
                    counts[measurement] += 1

                ## Checked per slice rather than per line: sink.tell() lags the true size while
                ## zlib holds a block back, so this is a lower bound, and one slice of overshoot
                ## is far below the 5 MB of headroom left under Telegram's limit.
                if sink.tell() > MAX_GZIP_BYTES:
                    raise ExportTooLarge(
                        f"export passed {MAX_GZIP_BYTES // (1024 * 1024)} MB at "
                        f"{slice_stop:%Y-%m-%d}")
                slice_start = slice_stop

            log.info(f"export: {measurement} -> {counts[measurement]} lines")

    return counts


def format_counts(counts):
    """The per-measurement line counts, densest first, as one short line."""
    listed = sorted(counts.items(), key=lambda kv: -kv[1])
    return " · ".join(f"{name} {count}" for name, count in listed if count)
