###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import time
import datetime
import serial
import os
import sys
import json
from numpy import median

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

## alerts.py lives in telegram_bot/; when run as a script, only sensors/ is on sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'telegram_bot'))
import alerts

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
    print(f"⚠️ Could not initialize InfluxDB client: {e}; measurements will not be recorded.")
    write_api = None
    query_api = None

## Serial communication
SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 9600

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
        print("⚠️ No InfluxDB query API; cannot read history.")
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
        print(f"⚠️ Could not query history from InfluxDB: {e}")
        return [], []

    for table in tables:
        for record in table.records:
            times.append(record.get_time())
            volumes.append(height_to_volume(record.get_value()))
    return times, volumes

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

    # The reset pulse rebooted the Arduino; wait for it to come back up
    print("Waiting for Arduino to start serial communication...")
    deadline = time.time() + 10
    while time.time() < deadline:
        line = arduino.readline().decode('utf-8', errors='ignore').strip()
        if line:
            print(f"Arduino says: {line}")
        if line == "Started serial com":
            break
        time.sleep(0.1)
    print("Proceeding (ready or timeout).")
    arduino.reset_input_buffer()
    return arduino

def get_sensor_data(arduino, retries=3, delay=0.5):
    for attempt in range(retries):
        print(f'Get sensor data (attempt {attempt + 1})')
        arduino.write(b'READ_PUIT\n')
        time.sleep(delay)
        raw = arduino.readline()

        if raw:
            try:
                height_str = raw.decode('utf-8').strip()
                print('received data:', height_str)
                return float(height_str)
            except ValueError:
                print("⚠️ Invalid float format.")
                write_influx_log(('arduino', raw), tag=('error', 'Invalid float format'))
        else:
            print("No response from Arduino.")
    print("❌ Failed to get valid sensor data after retries.")
    write_influx_log(('arduino', 'no response'))
    return None

def load_previous_measure():
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        value = data.get('previous_measure')
        if isinstance(value, (int, float)):
            return float(value)
        return None
    except Exception as e:
        print(f"Warning: could not load previous measure ({e}); treating as unknown.")
        return None

def save_previous_measure(height):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({'previous_measure': height}, f)
    except Exception as e:
        print(f"Warning: could not save previous measure ({e}).")

def write_influx_log(log, tag=None):
    if write_api is None:
        return False

    (field_name, field_value) = log
    point = (
        Point('log')
        .field(field_name, field_value)
    )
    if tag is not None:
        (tag_name, tag_value) = tag
        point.tag(tag_name, tag_value)

    # Write data to InfluxDB
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        print(f"⚠️ Could not write log to InfluxDB: {e}")
        return False

def write_influx_measurement(heigth_median, resampled=False):
    if write_api is None:
        print("⚠️ No InfluxDB write API; measurement not recorded.")
        return False

    # Timestamp rounded to the nearest 5 minutes; must be UTC-aware because the
    # client interprets naive datetimes as UTC
    rounded = roundTime(datetime.datetime.now(datetime.timezone.utc), roundTo=300)

    print('Write influxdb time', rounded.isoformat())
    # Create a data point for InfluxDB
    point = (
        Point('height_measure')  # Measurement name
        .tag('resampled', resampled)
        .field('lenght_median', heigth_median)
        .time(rounded, WritePrecision.S)
    )

    # Write data to InfluxDB
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        return True
    except Exception as e:
        print(f"⚠️ Could not write measurement to InfluxDB: {e}")
        return False


def collect_puit_data(arduino):
    max_diff_tolerance = 5 # cm
    resampled = False

    previous_measure = load_previous_measure()
    height = get_sensor_data(arduino)

    if height is None:
        print("No valid height reading this run; skipping write.")
        return None, False, False

    if previous_measure is not None:
        if abs(height-previous_measure) > max_diff_tolerance:
            height_medians = [height]
            for i in range(4):
                sample = get_sensor_data(arduino)
                if sample is not None:
                    height_medians.append(sample)
            height = float(median(height_medians))
            resampled = True

    db_ok = write_influx_measurement(height, resampled)
    save_previous_measure(height)

    try:
        alerts.check_thresholds(height_to_volume(height))
    except Exception as e:
        print(f"⚠️ Alert check failed: {e}")
        write_influx_log(('alerts', str(e)), tag=('error', 'alert check failed'))

    return height, resampled, db_ok


def _with_arduino(lock_timeout, action):
    """Serialize serial-port access (flock) and run `action(arduino)` on a fresh port.

    Raises TimeoutError if another measurement holds the lock past lock_timeout
    seconds.
    """
    import fcntl  # Linux-only; imported here so the module loads on dev machines

    lock_fd = open(LOCK_FILE, 'w')
    try:
        deadline = time.time() + lock_timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError("Another measurement is already in progress.")
                time.sleep(0.5)

        arduino = open_arduino()
        try:
            return action(arduino)
        finally:
            arduino.close()
    finally:
        lock_fd.close()  # releases the flock


def measure_once(lock_timeout=0):
    """Run one full measurement cycle: lock, open serial, measure (+DB write), clean up.

    Returns (height, resampled, db_ok); height is None if the sensor gave no valid
    reading. Raises TimeoutError if another measurement holds the lock past
    lock_timeout seconds.
    """
    return _with_arduino(lock_timeout, collect_puit_data)


def raw_samples_once(lock_timeout=0):
    """Ask the Arduino for its raw ping array (SAMPLING command) — diagnostics only.

    Returns the raw JSON-ish line (e.g. "[61.00, 61.00, 158.00, ...]") or None if
    the Arduino did not answer. Raises TimeoutError like measure_once.
    """
    def action(arduino):
        arduino.reset_input_buffer()
        arduino.write(b'SAMPLING\n')
        # 10 pings, worst case ~1 s each when an echo times out.
        deadline = time.time() + 12
        while time.time() < deadline:
            line = arduino.readline().decode('utf-8', errors='ignore').strip()
            if line:
                return line
        return None

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
    try:
        height, resampled, db_ok = measure_once(lock_timeout=60)
    except TimeoutError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Could not open serial port {SERIAL_PORT}: {e}")
        sys.exit(1)

    if height is None or not db_ok:
        sys.exit(1)
