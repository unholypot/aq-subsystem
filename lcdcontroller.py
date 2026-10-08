#!/usr/bin/env python3
"""Manual Enviro+ LCD controller. Reads the sensor loop's RAM snapshot.
No sensor/I2C reads, HTTP server or boot configuration. Frame page is a placeholder.
"""

import argparse
import json
import math
import os
import queue
import signal
import subprocess
import time
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

PAGES = ("ip", "environment", "gas", "frame")
COMMAND_TOPIC = "gcs/aq/display/set"
STATE_TOPIC = "gcs/aq/display/state"
SNAPSHOT_PATH = "/dev/shm/egh455-aq.json"


def parse_command(payload, retained=False):
    if retained:
        raise ValueError("Old retained commands are ignored; send with retain=false")
    if len(payload) > 1024:
        raise ValueError("Command exceeds 1024 bytes")
    command = json.loads(payload)
    if not isinstance(command, dict) or command.get("screen") not in PAGES:
        raise ValueError("screen must be ip, environment, gas or frame")
    request_id = command.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or len(request_id) > 80
    ):
        raise ValueError("request_id must be a string of at most 80 characters")
    return command["screen"], request_id


def load_snapshot(path, now):
    try:
        with open(path) as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        stamp = data.get("sampled_monotonic")
        if not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= 5:
            return None
        if (
            not isinstance(data.get("reading"), dict)
            or not isinstance(data.get("session_id"), str)
            or type(data.get("gesture_count")) is not int
            or data["gesture_count"] < 0
        ):
            return None
        remaining = data.get("gas_warmup_remaining_s")
        if (
            not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
            or remaining < 0
        ):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


class GestureCursor:
    """Ignore past gestures on controller start, sensor restart or stale recovery."""

    def __init__(self):
        self.session = None
        self.count = 0

    def consume(self, data):
        if data is None:
            self.session = None
            return 0
        session, count = data["session_id"], data["gesture_count"]
        if session != self.session or count < self.count:
            self.session, self.count = session, count
            return 0
        advances = count - self.count
        self.count = count
        return advances % len(PAGES)


def network_addresses():
    """Local interface addresses, without depending on internet connectivity."""
    lan, tail = [], []
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
        for interface in json.loads(result.stdout):
            name = interface.get("ifname", "")
            if name == "lo" or name.startswith(("docker", "br-", "veth")):
                continue
            for addr in interface.get("addr_info", []):
                if addr.get("scope") != "global":
                    continue
                (tail if name == "tailscale0" else lan).append((name, addr["local"]))
        lan.sort(key=lambda item: (not item[0].startswith(("wlan", "wl")), item[0]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return lan, tail


def font(size, bold=False):
    # Use a real bold face where available, with the existing Roboto as fallback.
    candidates = (
        ["DejaVuSansCondensed-Bold.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["DejaVuSansCondensed.ttf", "DejaVuSans.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    try:
        from fonts.ttf import RobotoMedium

        return ImageFont.truetype(RobotoMedium, size)
    except (ImportError, OSError):
        return ImageFont.load_default()


class Renderer:
    """160x80 high-contrast display, preserving existing content positions."""

    BACKGROUND = "#163D75"
    PANEL = BACKGROUND
    TEXT = "#FFFFFF"
    MUTED = "#FFFFFF"
    LINE = BACKGROUND
    ACCENTS = {
        "ip": "#60CFFF",
        "environment": "#57E3B1",
        "gas": "#FFC66B",
        "frame": "#C2A2FF",
    }
    # All important text sits to the right of the vertical stripe in the photos.
    LEFT, RIGHT = 31, 155

    def __init__(self, width=160, height=80):
        self.width, self.height = width, height
        self.last_ip_check = -math.inf
        self.addresses = ([], [])
        self.fonts = {}

    def text(
        self,
        draw,
        text,
        x,
        y,
        size=10,
        colour=None,
        bold=False,
        align="left",
        max_width=None,
    ):
        text = str(text)
        max_width = max_width if max_width is not None else self.RIGHT - self.LEFT
        # Shrink only when necessary; do not silently chop off numeric values.
        for chosen_size in range(size, 7, -1):
            key = chosen_size, bold
            if key not in self.fonts:
                self.fonts[key] = font(*key)
            selected = self.fonts[key]
            box = draw.textbbox((0, 0), text, font=selected)
            width = box[2] - box[0]
            if width <= max_width:
                break
        if width > max_width:
            # Very large numeric readings remain identifiable via scientific notation
            # in the callers; unusual interface names can be shortened here.
            while text and width > max_width:
                text = text[:-1]
                box = draw.textbbox((0, 0), text + "~", font=selected)
                width = box[2] - box[0]
            text += "~"
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        draw.text(
            (round(x - box[0]), round(y - box[1])),
            text,
            font=selected,
            fill=colour or self.TEXT,
        )

    def center(self, draw, text, y, **kwargs):
        self.text(draw, text, (self.LEFT + self.RIGHT) / 2, y, align="center", **kwargs)

    @staticmethod
    def value(value, digits=1):
        if isinstance(value, (int, float)) and math.isfinite(value):
            if abs(value) >= 10000:
                return f"{value:.1e}"
            return f"{value:.{digits}f}"
        return "--"

    def render(self, page, reading, remaining, connected, sample_age):
        # Compose at the native resolution, then adapt if the driver reports another size.
        image = Image.new("RGB", (160, 80), self.BACKGROUND)
        draw = ImageDraw.Draw(image)
        accent = self.ACCENTS[page]
        draw.rounded_rectangle((27, 2, 159, 77), radius=5, fill=self.PANEL)
        draw.rectangle((31, 15, 155, 15), fill=self.LINE)
        titles = {
            "ip": "NETWORK",
            "environment": "ENVIRONMENT",
            "gas": "GAS EST. / ppm",
            "frame": "ANNOTATED FRAME",
        }
        self.center(draw, titles[page], 4, size=10, bold=True, colour=accent)
        if page == "ip":
            if time.monotonic() - self.last_ip_check >= 10:
                self.addresses = network_addresses()
                self.last_ip_check = time.monotonic()
            lan, tail = self.addresses
            entry = lan[int(time.monotonic() // 5) % len(lan)] if lan else ("LAN", None)
            name = entry[0]
            label = "WI-FI" if name.startswith(("wlan", "wl")) else "LAN"
            self.center(draw, label, 19, size=8, colour=self.MUTED)
            self.center(
                draw,
                entry[1] or "Not connected",
                29,
                size=12,
                bold=True,
                colour=accent if entry[1] else self.MUTED,
            )
            self.center(draw, "TAILSCALE", 44, size=8, colour=self.MUTED)
            self.center(
                draw, tail[0][1] if tail else "Not connected", 54, size=12, bold=True
            )
        elif page == "frame":
            # Placeholder only: the annotated image handoff is a separate task.
            draw.rounded_rectangle((83, 21, 103, 35), radius=2, outline=accent)
            draw.ellipse((96, 24, 99, 27), fill=accent)
            draw.line([(86, 32), (91, 27), (95, 31)], fill=accent, width=1)
            self.center(draw, "Waiting for", 42, size=11, bold=True)
            self.center(draw, "annotated frame", 55, size=10, colour=self.MUTED)
        elif sample_age > 5:
            self.center(
                draw, "NO SENSOR DATA", 26, size=11, bold=True, colour=self.TEXT
            )
            self.center(draw, "Waiting for readings", 46, size=9, colour=self.MUTED)
        elif page == "environment":
            self.text(draw, "AIR EST.", 32, 19, size=8, colour=self.MUTED)
            self.text(
                draw,
                "HUMIDITY",
                154,
                19,
                size=8,
                colour=self.MUTED,
                align="right",
                max_width=55,
            )
            self.text(
                draw,
                self.value(reading.get("temperature_c")) + " C",
                32,
                29,
                size=16,
                bold=True,
                colour=accent,
                max_width=72,
            )
            self.text(
                draw,
                self.value(reading.get("humidity_pct"), 0) + "%",
                154,
                29,
                size=16,
                bold=True,
                align="right",
                max_width=49,
            )
            cpu = self.value(reading.get("cpu_temp_c"))
            raw = self.value(reading.get("temperature_raw_c"))
            self.text(draw, f"CPU {cpu}C", 32, 48, size=9, max_width=62)
            self.text(draw, f"RAW {raw}C", 154, 48, size=9, align="right", max_width=60)
            self.text(
                draw,
                self.value(reading.get("pressure_hpa"), 0) + " hPa",
                32,
                60,
                size=9,
                colour=self.MUTED,
                max_width=62,
            )
            self.text(
                draw,
                self.value(reading.get("lux")) + " lux",
                154,
                60,
                size=9,
                align="right",
                colour=self.MUTED,
                max_width=60,
            )
        elif remaining > 0:
            seconds = max(0, math.ceil(remaining))
            self.center(draw, "WARMING UP", 20, size=9, bold=True, colour=self.MUTED)
            self.center(
                draw,
                f"{seconds // 60:02d}:{seconds % 60:02d}",
                32,
                size=22,
                bold=True,
                colour=accent,
            )
            self.center(draw, "Estimates pending", 56, size=9, colour=self.MUTED)
        else:
            for label, field, y in [
                ("CO", "co_ppm", 21),
                ("NO2", "no2_ppm", 37),
                ("NH3", "nh3_ppm", 53),
            ]:
                self.text(
                    draw,
                    label,
                    34,
                    y + 2,
                    size=10,
                    bold=True,
                    colour=self.MUTED,
                    max_width=30,
                )
                self.text(
                    draw,
                    self.value(reading.get(field), 3),
                    153,
                    y,
                    size=14,
                    bold=True,
                    colour=accent,
                    align="right",
                    max_width=86,
                )
        # The connectivity indicator describes MQTT, not air quality.
        status = "#FFFFFF" if connected else "#BDD1EE"
        draw.ellipse((32, 72, 35, 75), fill=status)
        self.text(
            draw,
            "MQTT ON" if connected else "MQTT OFF",
            39,
            71,
            size=8,
            colour=self.MUTED,
            max_width=70,
        )
        for index in range(4):
            x = 120 + index * 9
            draw.rounded_rectangle(
                (x, 72, x + 5, 75),
                radius=1,
                fill=accent if PAGES[index] == page else "#7FA4CF",
            )
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height), Image.Resampling.NEAREST)
        return image


def create_display():
    import st7735

    display = st7735.ST7735(
        port=0, cs=1, dc="GPIO9", backlight="GPIO12", rotation=270, spi_speed_hz=1000000
    )
    display.begin()
    return display


def publish_state(client, page, source, error=None, request_id=None, online=True):
    if not client.is_connected():
        return None
    state = {
        "screen": page,
        "online": online,
        "source": source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "request_id": request_id,
    }
    return client.publish(STATE_TOPIC, json.dumps(state), qos=0, retain=True)


def acquire_lock(path):
    import fcntl

    handle = open(path, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit(
            "Another LCD controller using this snapshot is already running"
        )
    return handle


def terminate(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default=os.getenv("MQTT_BROKER", "10.88.35.39"))
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--snapshot", default=SNAPSHOT_PATH)
    args = parser.parse_args()
    instance_lock = acquire_lock(args.snapshot + ".lcd.lock")
    signal.signal(signal.SIGTERM, terminate)
    import paho.mqtt.client as mqtt

    commands = queue.Queue(maxsize=32)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print(f"[LCD MQTT] Connection rejected: {reason_code}")
            return
        result, _ = client.subscribe(COMMAND_TOPIC, qos=1)
        print(
            f"[LCD MQTT] Connected to {args.broker}:{args.port}; subscribe result {result}"
        )
        try:
            commands.put_nowait((time.monotonic(), "refresh", "reconnect", None))
        except queue.Full:
            pass

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        if any(code.is_failure for code in reason_codes):
            print(f"[LCD MQTT] Subscription rejected: {reason_codes}")
        else:
            print(f"[LCD MQTT] Listening on {COMMAND_TOPIC}")

    def on_message(client, userdata, message):
        if message.topic != COMMAND_TOPIC:
            return
        try:
            screen, request_id = parse_command(message.payload, message.retain)
            commands.put_nowait((time.monotonic(), screen, "web", request_id))
        except (ValueError, UnicodeError, queue.Full) as exc:
            print(f"[LCD MQTT] Command rejected: {exc}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_connect_fail = lambda c, u: print(
        "[LCD MQTT] Broker unavailable; retrying"
    )
    client.on_disconnect = lambda c, u, f, r, p: print(f"[LCD MQTT] Disconnected: {r}")
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.will_set(
        STATE_TOPIC,
        json.dumps({"online": False, "screen": None, "source": "connection_lost"}),
        qos=0,
        retain=True,
    )
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.environ["MQTT_USERNAME"], os.getenv("MQTT_PASSWORD"))
    display = create_display()
    renderer = Renderer(display.width, display.height)
    print(f"LCD ready: {display.width}x{display.height}; starting on IP page")
    print(f"Reading {args.snapshot}; Ctrl+C stops only the LCD controller")
    page, source, request_id = "ip", "startup", None
    cursor = GestureCursor()
    last_state = -math.inf
    last_selection_time = -math.inf
    last_error = None
    network_started = False
    last_image_bytes = None
    try:
        client.connect_async(args.broker, args.port, keepalive=30)
        client.loop_start()
        network_started = True
        while True:
            now = time.monotonic()
            data = load_snapshot(args.snapshot, now)
            events = []
            advances = cursor.consume(data)
            if advances:
                stamp = data.get("gesture_time")
                if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                    stamp = now
                for _ in range(advances):
                    events.append((stamp, "next", "proximity", None))
            for _ in range(32):
                try:
                    events.append(commands.get_nowait())
                except queue.Empty:
                    break

            # Process commands and physical gestures in the order they occurred.
            for event_time, target, event_source, event_id in sorted(
                events, key=lambda item: item[0]
            ):
                if event_time < last_selection_time:
                    continue
                if target == "next":
                    page = PAGES[(PAGES.index(page) + 1) % len(PAGES)]
                elif target == "refresh":
                    continue
                else:
                    page = target
                source, request_id = event_source, event_id
                last_selection_time = event_time
                print(f"[LCD] {page} ({source})")
            reading = data["reading"] if data else {}
            remaining = data["gas_warmup_remaining_s"] if data else 900
            sample_age = now - data["sampled_monotonic"] if data else math.inf
            error = None
            try:
                frame = renderer.render(
                    page, reading, remaining, client.is_connected(), sample_age
                )
                frame_bytes = frame.tobytes()
                if frame_bytes != last_image_bytes:
                    display.display(frame)
                    last_image_bytes = frame_bytes
            except Exception as exc:
                error = f"LCD render failed: {exc}"
                if error != last_error:
                    print(f"[LCD] {error}")
            if events or error != last_error or now - last_state >= 5:
                publish_state(client, page, source, error, request_id)
                last_state = now
            last_error = error
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nLCD controller stopped. Sensor loop is independent.")
    finally:
        if network_started:
            info = publish_state(client, page, "shutdown", online=False)
            if info is not None:
                try:
                    info.wait_for_publish(timeout=1)
                except (RuntimeError, ValueError):
                    pass
            client.disconnect()
            client.loop_stop()
        instance_lock.close()


if __name__ == "__main__":
    main()
