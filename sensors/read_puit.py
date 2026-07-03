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

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Influx database
INFLUXDB_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG    = os.getenv("INFLUX_ORG",    "PiJardin")
INFLUXDB_BUCKET = os.getenv("INFLUX_BUCKET", "puit")

try:
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
except Exception as e:
    print(f"❌ Could not initialize InfluxDB client: {e}")
    sys.exit(1)

## Serial communication
SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 9600
try:
    arduino = serial.Serial(port=SERIAL_PORT, baudrate=BAUD_RATE, timeout=.1)
except Exception as e:
    print(f"❌ Could not open serial port {SERIAL_PORT}: {e}")
    sys.exit(1)

## Store last measurements
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.puit_state.json')

# -------------------------------------------------------------------------------------------------
# FUNCTIONS
def get_sensor_data(retries=3, delay=0.5):
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
    (field_name, field_value) = log
    point = (
        Point('log')
        .field(field_name, field_value)
    )
    if tag is not None:
        (tag_name, tag_value) = tag
        point.tag(tag_name, tag_value)

    # Write data to InfluxDB
    write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)

def write_influx_measurement(heigth_median, resampled=False):
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
    write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)


def collect_puit_data():
    max_diff_tolerance = 5 # cm
    resampled = False

    previous_measure = load_previous_measure()
    height = get_sensor_data()

    if height is None:
        print("No valid height reading this run; skipping write.")
        return False

    if previous_measure is not None:
        if abs(height-previous_measure) > max_diff_tolerance:
            height_medians = [height]
            for i in range(4):
                sample = get_sensor_data()
                if sample is not None:
                    height_medians.append(sample)
            height = float(median(height_medians))
            resampled = True

    write_influx_measurement(height, resampled)
    save_previous_measure(height)
    return True


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
# INITIALISATION
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

# -------------------------------------------------------------------------------------------------
# SCHEDULING

success = collect_puit_data()
if not success:
    sys.exit(1)