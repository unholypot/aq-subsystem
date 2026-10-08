#!/usr/bin/env python3
"""Enviro+ CSV logger and GCS MQTT telemetry publisher.

Run in the Pi's existing sensor Python environment:
    python3 -u sensorloop.py --broker 10.88.35.39
When the broker runs on this same Pi instead:
    python3 -u sensorloop.py --broker localhost

Requires the existing sensor libraries and paho-mqtt >= 2.
CSV retains the original flat fields. MQTT follows the GCS README schema.
Gas values are estimates using the supplied constants, not verified calibration.
Gas telemetry is null for the first 15 minutes; the receiver must handle null
as unavailable, not zero. CSV keeps raw measurements and provisional estimates.
Disconnected samples stay in CSV; they are not replayed to the live dashboard.
"""

import argparse
import csv
import json
import math
import os
import time
from datetime import datetime, timezone

MQTT_BROKER = "10.88.35.39"
MQTT_PORT = 1883
MQTT_TOPIC = "gcs/telemetry"
COMP_FACTOR = 2.25  # Existing provisional temperature correction, unchanged.
GAS_WARMUP_SECONDS = 15 * 60
READ_INTERVAL_SECONDS = 1
READ_RETRIES = 3
RETRY_DELAY_SECONDS = 0.2
R0_OXIDISING = 26526.05
R0_REDUCING = 332616.02
R0_NH3 = 143725.56
LOG_PATH = os.path.expanduser("~/aq-subsystem/data/sensorlog.csv")

BME_DEFAULTS = dict.fromkeys([
    "temperature_raw_c", "cpu_temp_c", "temperature_c",
    "pressure_hpa", "humidity_pct",
])
LIGHT_DEFAULTS = dict.fromkeys(["lux", "proximity"])
GAS_DEFAULTS = dict.fromkeys([
    "oxidising_ohm", "reducing_ohm", "nh3_ohm", "oxidising_ratio",
    "reducing_ratio", "nh3_ratio", "co_ppm", "no2_ppm", "nh3_ppm",
])


def get_cpu_temperature():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return float(f.read()) / 1000.0


def read_bme280(bme280):
    cpu_temp = get_cpu_temperature()
    raw_temp = bme280.get_temperature()
    pressure = bme280.get_pressure()
    humidity = bme280.get_humidity()
    comp_temp = raw_temp - ((cpu_temp - raw_temp) / COMP_FACTOR)
    return {
        "temperature_raw_c": round(raw_temp, 2),
        "cpu_temp_c": round(cpu_temp, 2),
        "temperature_c": round(comp_temp, 2),
        "pressure_hpa": round(pressure, 2),
        "humidity_pct": round(humidity, 2),
    }


def read_light(ltr559):
    return {"lux": round(ltr559.get_lux(), 2),
            "proximity": ltr559.get_proximity()}


def read_gas(gas):
    readings = gas.read_all()
    ox, red, nh3 = readings.oxidising, readings.reducing, readings.nh3
    if any(not math.isfinite(v) or v <= 0 for v in (ox, red, nh3)):
        raise ValueError("Gas resistances must be finite and positive")
    ox_ratio = ox / R0_OXIDISING
    red_ratio = red / R0_REDUCING
    nh3_ratio = nh3 / R0_NH3
    # Existing user-supplied curve fits; retained for integration, not validated.
    co_ppm = 10 ** (-1.25 * math.log10(red_ratio) + 0.64)
    no2_ppm = 10 ** (math.log10(ox_ratio) - 0.8129)
    nh3_ppm = 10 ** (-1.8 * math.log10(nh3_ratio) - 0.163)
    return {
        "oxidising_ohm": round(ox, 2), "reducing_ohm": round(red, 2),
        "nh3_ohm": round(nh3, 2), "oxidising_ratio": round(ox_ratio, 3),
        "reducing_ratio": round(red_ratio, 3), "nh3_ratio": round(nh3_ratio, 3),
        "co_ppm": round(co_ppm, 3), "no2_ppm": round(no2_ppm, 3),
        "nh3_ppm": round(nh3_ppm, 3),
    }


def safe_read(sensor_name, read_function, default_values):
    for attempt in range(1, READ_RETRIES + 1):
        try:
            return read_function()
        except Exception as exc:
            print(f"[WARNING] {sensor_name} attempt {attempt}/{READ_RETRIES}: {exc}")
            if attempt < READ_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    return default_values.copy()


def log_row(row, path=LOG_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    has_data = os.path.isfile(path) and os.path.getsize(path) > 0
    if has_data:
        with open(path, newline="") as f:
            if next(csv.reader(f), []) != list(row):
                raise ValueError("CSV header differs; archive the old sensorlog.csv first")
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not has_data:
            writer.writeheader()
        writer.writerow(row)


def finite_or_none(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def build_telemetry(reading, gas_ready):
    """Map the flat CSV reading to the exact keys in the GCS README."""
    return {
        "timestamp": reading["timestamp"],
        "temperature_c": finite_or_none(reading.get("temperature_c")),
        "pressure_hpa": finite_or_none(reading.get("pressure_hpa")),
        "humidity_pct": finite_or_none(reading.get("humidity_pct")),
        "light_lux": finite_or_none(reading.get("lux")),
        "gas": {
            key: finite_or_none(reading.get(key)) if gas_ready else None
            for key in ("co_ppm", "no2_ppm", "nh3_ppm")
        },
    }


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"[MQTT] Connected to {userdata['broker']}:{userdata['port']}; "
              f"publishing {MQTT_TOPIC}")
    else:
        print(f"[MQTT] Connection rejected: {reason_code}")


def on_connect_fail(client, userdata):
    print(f"[MQTT] Cannot reach {userdata['broker']}:{userdata['port']}; retrying")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code != 0:
        print(f"[MQTT] Disconnected: {reason_code}; reconnecting automatically")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default=os.getenv("MQTT_BROKER", MQTT_BROKER))
    parser.add_argument("--port", type=int, default=MQTT_PORT)
    args = parser.parse_args()

    # Hardware imports here allow payload checks on a computer without Enviro+.
    from smbus2 import SMBus
    from bme280 import BME280
    from ltr559 import LTR559
    from enviroplus import gas
    import paho.mqtt.client as mqtt

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              userdata=vars(args))
    mqtt_client.on_connect = on_connect
    mqtt_client.on_connect_fail = on_connect_fail
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    # Optional credentials if the team's broker requires authentication.
    if os.getenv("MQTT_USERNAME"):
        mqtt_client.username_pw_set(os.environ["MQTT_USERNAME"],
                                    os.getenv("MQTT_PASSWORD"))
    bus = SMBus(1)
    network_started = False
    try:
        bme280 = BME280(i2c_dev=bus)
        ltr559 = LTR559()
        mqtt_client.connect_async(args.broker, args.port, keepalive=60)
        mqtt_client.loop_start()
        network_started = True
        start_time = time.monotonic()
        last_offline_notice = -math.inf
        print(f"Starting sensor loop. CSV: {LOG_PATH}")
        print("Gas telemetry is null during the 15-minute warm-up. Ctrl+C stops.")
        while True:
            loop_start = time.monotonic()
            reading = {"timestamp": datetime.now(timezone.utc).isoformat()}
            reading.update(safe_read("BME280", lambda: read_bme280(bme280), BME_DEFAULTS))
            reading.update(safe_read("LTR559", lambda: read_light(ltr559), LIGHT_DEFAULTS))
            reading.update(safe_read("MiCS-6814", lambda: read_gas(gas), GAS_DEFAULTS))
            elapsed = time.monotonic() - start_time
            gas_ready = elapsed >= GAS_WARMUP_SECONDS
            telemetry = build_telemetry(reading, gas_ready)
            payload = json.dumps(telemetry, allow_nan=False)
            print(payload)
            if not gas_ready:
                print(f"Gas warm-up: {math.ceil(GAS_WARMUP_SECONDS - elapsed)}s remaining")
            try:
                log_row(reading)
            except (OSError, ValueError) as exc:
                print(f"[WARNING] CSV logging failed: {exc}")

            if mqtt_client.is_connected():
                # Live telemetry: no retained stale reading or outage replay.
                info = mqtt_client.publish(MQTT_TOPIC, payload, qos=0, retain=False)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    print(f"[WARNING] MQTT publish: {mqtt.error_string(info.rc)}")
            elif loop_start - last_offline_notice >= 30:
                print("[MQTT] Offline: logging locally while connection retries")
                last_offline_notice = loop_start
            time.sleep(max(0, READ_INTERVAL_SECONDS - (time.monotonic() - loop_start)))
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        if network_started:
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
        bus.close()


if __name__ == "__main__":
    main()
