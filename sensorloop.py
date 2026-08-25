import time
import json
import csv
import os
from datetime import datetime, timezone

from smbus2 import SMBus
from bme280 import BME280
from ltr559 import LTR559
from enviroplus import gas
import paho.mqtt.client as mqtt

# --- MQTT config: these are placeholders ---
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "egh455/aq"
# ------------------------------------------------------------------

COMP_FACTOR = 2.25  # gota tune

LOG_PATH = os.path.expanduser("~/aq-subsystem/data/sensor_log.csv")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def get_cpu_temperature():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return float(f.read()) / 1000.0


def read_bme280(bme280):
    cpu_temp = get_cpu_temperature()
    raw_temp = bme280.get_temperature()
    comp_temp = raw_temp - ((cpu_temp - raw_temp) / COMP_FACTOR)
    return {
        "temperature_c": round(comp_temp, 2),
        "pressure_hpa": round(bme280.get_pressure(), 2),
        "humidity_pct": round(bme280.get_humidity(), 2),
    }


def read_light(ltr559):
    return {
        "lux": round(ltr559.get_lux(), 2),
        "proximity": ltr559.get_proximity(),
    }


def read_gas():
    readings = gas.read_all()
    # Raw resistance in ohms. Converting to ppm needs a clean-air R0 baseline
    # per channel and datasheet curve fitting 
    return {
        "oxidising_ohm": round(readings.oxidising, 2),  # NO2-related
        "reducing_ohm": round(readings.reducing, 2),    # CO-related
        "nh3_ohm": round(readings.nh3, 2),               # Ammonia-related
    }


def log_row(row, path=LOG_PATH):
    file_exists = os.path.isfile(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("MQTT connected")
    else:
        print("MQTT connect failed:", reason_code)


def main():
    bus = SMBus(1)
    bme280 = BME280(i2c_dev=bus)
    ltr559 = LTR559()

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_connect
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    print("Starting AQ sensor loop. Ctrl+C to stop.")
    try:
        while True:
            reading = {"timestamp": datetime.now(timezone.utc).isoformat()}
            reading.update(read_bme280(bme280))
            reading.update(read_light(ltr559))
            reading.update(read_gas())

            print(reading)
            log_row(reading)
            mqtt_client.publish(MQTT_TOPIC, json.dumps(reading))

            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()