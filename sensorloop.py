import time
import json
import csv
import os
from datetime import datetime, timezone
import math


from smbus2 import SMBus
from bme280 import BME280
from ltr559 import LTR559
from enviroplus import gas
import paho.mqtt.client as mqtt


# ============================================================
# CONFIG
# ============================================================

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "egh455/aq"

# Temporary value until proper temperature calibration is done
COMP_FACTOR = 2.25

# Based on our current bench testing.
# We will refine this later if needed.
GAS_WARMUP_SECONDS = 15 * 60

READ_INTERVAL_SECONDS = 1
READ_RETRIES = 3
RETRY_DELAY_SECONDS = 0.2


R0_OXIDISING = 26526.05
R0_REDUCING = 332616.02
R0_NH3 = 143725.56


LOG_PATH = os.path.expanduser("~/aq-subsystem/data/sensorlog.csv")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


# ============================================================
# TEMPERATURE
# ============================================================

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


# ============================================================
# LIGHT / PROXIMITY
# ============================================================

def read_light(ltr559):
    return {
        "lux": round(ltr559.get_lux(), 2),
        "proximity": ltr559.get_proximity(),
    }


# ============================================================
# GAS SENSOR
# ============================================================

def read_gas():
    readings = gas.read_all()

    ox = readings.oxidising
    red = readings.reducing
    nh3 = readings.nh3

    # Normalised sensor response
    ox_ratio = ox / R0_OXIDISING
    red_ratio = red / R0_REDUCING
    nh3_ratio = nh3 / R0_NH3

    # Approximate ppm conversion from MiCS-6814 datasheet curve fits
    # RED channel -> CO equivalent
    co_ppm = 10 ** (-1.25 * math.log10(red_ratio) + 0.64)

    # OX channel -> NO2 equivalent
    no2_ppm = 10 ** (math.log10(ox_ratio) - 0.8129)

    # NH3 channel -> NH3 equivalent
    nh3_ppm = 10 ** (-1.8 * math.log10(nh3_ratio) - 0.163)

    return {
        "oxidising_ohm": round(ox, 2),
        "reducing_ohm": round(red, 2),
        "nh3_ohm": round(nh3, 2),

        "oxidising_ratio": round(ox_ratio, 3),
        "reducing_ratio": round(red_ratio, 3),
        "nh3_ratio": round(nh3_ratio, 3),

        "co_ppm": round(co_ppm, 3),
        "no2_ppm": round(no2_ppm, 3),
        "nh3_ppm": round(nh3_ppm, 3),
    }


# ============================================================
# SAFE SENSOR READING
# ============================================================

def safe_read(sensor_name, read_function, default_values):
    """
    Try to read a sensor several times.

    If an I2C or other sensor error occurs, retry instead of
    terminating the whole sensor loop.
    """

    for attempt in range(1, READ_RETRIES + 1):
        try:
            return read_function()

        except OSError as e:
            print(
                f"[WARNING] {sensor_name} read failed "
                f"(attempt {attempt}/{READ_RETRIES}): {e}"
            )

        except Exception as e:
            print(
                f"[WARNING] Unexpected {sensor_name} error "
                f"(attempt {attempt}/{READ_RETRIES}): {e}"
            )

        time.sleep(RETRY_DELAY_SECONDS)

    print(
        f"[ERROR] {sensor_name} unavailable after "
        f"{READ_RETRIES} attempts."
    )

    return default_values.copy()


# ============================================================
# CSV LOGGING
# ============================================================

def log_row(row, path=LOG_PATH):
    file_exists = os.path.isfile(path)

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=row.keys()
        )

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)


# ============================================================
# MQTT
# ============================================================

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("[MQTT] Connected")
    else:
        print("[MQTT] Connection failed:", reason_code)


# ============================================================
# MAIN
# ============================================================

def main():

    print("Initialising sensors...")

    bus = SMBus(1)

    bme280 = BME280(i2c_dev=bus)
    ltr559 = LTR559()

    # --------------------------------------------------------
    # MQTT
    # --------------------------------------------------------

    mqtt_client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2
    )

    mqtt_client.on_connect = on_connect

    try:
        mqtt_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            keepalive=60
        )

        mqtt_client.loop_start()
        mqtt_available = True

    except Exception as e:
        print("[WARNING] MQTT unavailable:", e)
        mqtt_available = False


    # --------------------------------------------------------
    # Gas warm-up timer
    # --------------------------------------------------------

    start_time = time.monotonic()

    print("Starting AQ sensor loop.")
    print(
        f"Gas sensor warm-up period: "
        f"{GAS_WARMUP_SECONDS // 60} minutes"
    )
    print("Ctrl+C to stop.")


    try:

        while True:

            loop_start = time.monotonic()

            reading = {
                "timestamp": datetime.now(
                    timezone.utc
                ).isoformat()
            }


            # ------------------------------------------------
            # BME280
            # ------------------------------------------------

            bme_data = safe_read(
                "BME280",
                lambda: read_bme280(bme280),
                {
                    "temperature_raw_c": None,
                    "cpu_temp_c": None,
                    "temperature_c": None,
                    "pressure_hpa": None,
                    "humidity_pct": None,
                }
            )

            reading.update(bme_data)


            # ------------------------------------------------
            # LTR559
            # ------------------------------------------------

            light_data = safe_read(
                "LTR559",
                lambda: read_light(ltr559),
                {
                    "lux": None,
                    "proximity": None,
                }
            )

            reading.update(light_data)


            # ------------------------------------------------
            # GAS
            # ------------------------------------------------

            gas_data = safe_read(
    "MiCS-6814",
    read_gas,
    {
        "oxidising_ohm": None,
        "reducing_ohm": None,
        "nh3_ohm": None,

        "oxidising_ratio": None,
        "reducing_ratio": None,
        "nh3_ratio": None,

        "co_ppm": None,
        "no2_ppm": None,
        "nh3_ppm": None,
    }
)

            reading.update(gas_data)


            # ------------------------------------------------
            # Gas warm-up status
            # ------------------------------------------------

            elapsed = time.monotonic() - start_time

            gas_ready = elapsed >= GAS_WARMUP_SECONDS

            if gas_ready:
                gas_status = "READY"
            else:
                remaining = int(
                    GAS_WARMUP_SECONDS - elapsed
                )

                gas_status = (
                    f"WARMING_UP "
                    f"({remaining}s remaining)"
                )


            # ------------------------------------------------
            # OUTPUT
            # ------------------------------------------------

            print(reading)
            print(f"Gas status: {gas_status}")


            # ------------------------------------------------
            # CSV
            # ------------------------------------------------

            try:
                log_row(reading)

            except Exception as e:
                print("[WARNING] CSV logging failed:", e)


            # ------------------------------------------------
            # MQTT
            # ------------------------------------------------

            if mqtt_available:

                try:
                    mqtt_client.publish(
                        MQTT_TOPIC,
                        json.dumps(reading)
                    )

                except Exception as e:
                    print(
                        "[WARNING] MQTT publish failed:",
                        e
                    )


            # ------------------------------------------------
            # Maintain approximately 1 Hz sampling
            # ------------------------------------------------

            loop_duration = (
                time.monotonic() - loop_start
            )

            sleep_time = (
                READ_INTERVAL_SECONDS
                - loop_duration
            )

            if sleep_time > 0:
                time.sleep(sleep_time)


    except KeyboardInterrupt:
        print("\nStopped by user.")


    finally:

        if mqtt_available:

            mqtt_client.loop_stop()
            mqtt_client.disconnect()

        bus.close()

        print("AQ sensor loop shut down cleanly.")


if __name__ == "__main__":
    main()