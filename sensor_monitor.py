"""
Run this on each Raspberry Pi camera node.

The camera's location is configured locally in camera_config.json and is
also registered centrally in data/cameras.csv. No GPS hardware is required.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import board
import busio
import numpy as np
import adafruit_mlx90640
import requests
from picamera2 import Picamera2


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "camera_config.json"

HOT_TEMP_C = float(os.getenv("HOT_TEMP_C", "80"))
MIN_HOT_PERCENT = float(os.getenv("MIN_HOT_PERCENT", "10"))
REQUIRED_FRAMES = int(os.getenv("REQUIRED_FRAMES", "5"))
SERVER_URL = os.getenv("FIREWATCH_SERVER_URL", "http://localhost:8000")


def load_config():
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)

    required = ["camera_id", "camera_key", "latitude", "longitude"]
    missing = [x for x in required if x not in cfg]
    if missing:
        raise RuntimeError(f"Missing camera configuration: {', '.join(missing)}")

    return cfg


def create_thermal():
    i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
    mlx = adafruit_mlx90640.MLX90640(i2c)
    mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_8_HZ
    return mlx


def get_stats(mlx, frame):
    mlx.getFrame(frame)
    temps = np.asarray(frame, dtype=np.float32).reshape(24, 32)
    valid = np.isfinite(temps)
    if not valid.any():
        return None

    max_temp = float(np.nanmax(temps))
    hot = valid & (temps >= HOT_TEMP_C)
    hot_percent = float(hot.sum() / valid.sum() * 100.0)
    return max_temp, hot_percent


def send_event(cfg, camera, max_temp, hot_percent, duration, image_path):
    url = SERVER_URL.rstrip("/") + "/api/camera-event"
    with open(image_path, "rb") as f:
        files = {"image": (Path(image_path).name, f, "image/jpeg")}
        data = {
            "camera_id": cfg["camera_id"],
            "camera_key": cfg["camera_key"],
            # These are included for diagnostics; the server uses its
            # registered camera coordinates as authoritative.
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "fire_size_percent": hot_percent,
            "max_temperature_c": max_temp,
            "duration_seconds": duration,
        }
        response = requests.post(url, data=data, files=files, timeout=30)
    response.raise_for_status()
    return response.json()


def main():
    cfg = load_config()
    mlx = create_thermal()

    camera = Picamera2()
    camera.configure(camera.create_still_configuration())
    camera.start()
    time.sleep(2)

    frame = [0.0] * 768
    consecutive = 0
    event_active = False
    started_at = None

    print(f"Firewatch camera {cfg['camera_id']}")
    print(f"Fixed location: {cfg['latitude']}, {cfg['longitude']}")
    print(f"Server: {SERVER_URL}")

    try:
        while True:
            try:
                stats = get_stats(mlx, frame)
            except Exception as exc:
                print(f"Thermal read error: {exc}")
                time.sleep(1)
                continue

            if stats is None:
                continue

            max_temp, hot_percent = stats
            detected = (
                max_temp >= HOT_TEMP_C and
                hot_percent >= MIN_HOT_PERCENT
            )

            if detected:
                if consecutive == 0:
                    started_at = time.monotonic()
                consecutive += 1
            else:
                consecutive = 0
                started_at = None
                event_active = False

            if detected and consecutive >= REQUIRED_FRAMES and not event_active:
                duration = time.monotonic() - started_at

                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                image_path = BASE_DIR / f"fire_{stamp}.jpg"
                camera.capture_file(str(image_path))

                try:
                    result = send_event(
                        cfg, camera, max_temp, hot_percent,
                        duration, image_path
                    )
                    print("Event submitted:", result)
                except Exception as exc:
                    print("Could not contact Firewatch server:", exc)

                event_active = True

                try:
                    image_path.unlink()
                except OSError:
                    pass

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        camera.stop()


if __name__ == "__main__":
    main()
