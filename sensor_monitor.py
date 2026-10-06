"""
Raspberry Pi fire-monitoring module for the Firewatch project.

Hardware assumed:
- Raspberry Pi
- Raspberry Pi Camera supported by Picamera2
- Adafruit MLX90640 thermal camera
- Adafruit Ultimate GPS HAT / compatible NMEA GPS

When a persistent hot region is detected, this program:
1. captures a JPEG;
2. reads the latest GPS latitude/longitude;
3. records the event time;
4. records the thermal hot-region percentage;
5. appends one row to data/device_fires.csv.

IMPORTANT:
fire_size_percent is the percentage of MLX90640 thermal pixels above
HOT_TEMP_C. It is NOT a measurement of physical fire area in square feet
or square meters. Calibrate the threshold for your intended environment.
"""

import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import board
import busio
import numpy as np
import adafruit_gps
import adafruit_mlx90640
import serial
from picamera2 import Picamera2


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
IMAGE_DIR = BASE_DIR / "images"
CSV_PATH = DATA_DIR / "device_fires.csv"

# Detection settings
HOT_TEMP_C = float(os.getenv("HOT_TEMP_C", "80"))
MIN_HOT_PERCENT = float(os.getenv("MIN_HOT_PERCENT", "10"))
REQUIRED_FRAMES = int(os.getenv("REQUIRED_FRAMES", "5"))
LOOP_DELAY = float(os.getenv("LOOP_DELAY", "0.05"))

# GPS:
# /dev/serial0 is a common default for a HAT on Raspberry Pi.
# Override with GPS_PORT=/dev/ttyUSB0 for a USB GPS.
GPS_PORT = os.getenv("GPS_PORT", "/dev/serial0")
GPS_BAUD = int(os.getenv("GPS_BAUD", "9600"))

CSV_FIELDS = [
    "timestamp",
    "latitude",
    "longitude",
    "fire_size_percent",
    "hot_pixels_percent",
    "max_temperature_c",
    "image",
    "place",
    "source",
]


def ensure_files():
    DATA_DIR.mkdir(exist_ok=True)
    IMAGE_DIR.mkdir(exist_ok=True)

    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def create_gps():
    # The Adafruit GPS Python library supports a normal pyserial UART on Pi.
    uart = serial.Serial(GPS_PORT, baudrate=GPS_BAUD, timeout=1)
    gps = adafruit_gps.GPS(uart, debug=False)
    return gps


def update_gps(gps):
    """Read available GPS messages and return the latest fix."""
    try:
        gps.update()
    except Exception as exc:
        print(f"GPS read error: {exc}")
        return None, None

    if not gps.has_fix:
        return None, None

    return gps.latitude, gps.longitude


def create_thermal():
    i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
    mlx = adafruit_mlx90640.MLX90640(i2c)

    # 8 Hz gives the detector several readings per second while leaving
    # processing time on a Raspberry Pi.
    mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_8_HZ
    return mlx


def get_thermal_stats(mlx, frame):
    """Return max temperature and percentage of hot thermal pixels."""
    try:
        mlx.getFrame(frame)
    except ValueError:
        return None

    temps = np.asarray(frame, dtype=np.float32).reshape(24, 32)

    valid = np.isfinite(temps)
    if not valid.any():
        return None

    max_temp = float(np.nanmax(temps))
    hot_mask = valid & (temps >= HOT_TEMP_C)
    hot_percent = float(hot_mask.sum() / valid.sum() * 100.0)

    # Fire-size metric: percentage of the thermal sensor's pixels
    # currently above the configured temperature threshold.
    fire_size_percent = hot_percent

    return max_temp, hot_percent, fire_size_percent


def capture_image(camera):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    image_path = IMAGE_DIR / f"fire_{timestamp}.jpg"
    camera.capture_file(str(image_path))
    return image_path


def write_event(timestamp, latitude, longitude, fire_size_percent,
                hot_percent, max_temp, image_path):
    row = {
        "timestamp": timestamp,
        "latitude": "" if latitude is None else f"{latitude:.7f}",
        "longitude": "" if longitude is None else f"{longitude:.7f}",
        "fire_size_percent": f"{fire_size_percent:.2f}",
        "hot_pixels_percent": f"{hot_percent:.2f}",
        "max_temperature_c": f"{max_temp:.2f}",
        "image": str(image_path.relative_to(BASE_DIR)),
        "place": "Raspberry Pi detection",
        "source": "device",
    }

    with CSV_PATH.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)

    return row


def main():
    ensure_files()

    print("Starting Raspberry Pi Firewatch sensor...")
    print(f"Thermal threshold: {HOT_TEMP_C:.1f} C")
    print(f"Required hot area: {MIN_HOT_PERCENT:.1f}%")
    print(f"Required consecutive frames: {REQUIRED_FRAMES}")
    print(f"GPS port: {GPS_PORT}")

    gps = create_gps()
    mlx = create_thermal()

    camera = Picamera2()
    camera.configure(camera.create_still_configuration())
    camera.start()
    time.sleep(2)

    frame = [0.0] * 768
    consecutive = 0
    event_active = False

    try:
        while True:
            update_gps(gps)
            stats = get_thermal_stats(mlx, frame)

            if stats is None:
                time.sleep(LOOP_DELAY)
                continue

            max_temp, hot_percent, fire_size_percent = stats

            detected = (
                max_temp >= HOT_TEMP_C
                and hot_percent >= MIN_HOT_PERCENT
            )

            if detected:
                consecutive += 1
            else:
                consecutive = 0
                event_active = False

            if detected and consecutive >= REQUIRED_FRAMES and not event_active:
                # Take the photo only once for each detection episode.
                image_path = capture_image(camera)

                latitude, longitude = update_gps(gps)

                timestamp = datetime.now(timezone.utc).isoformat()

                row = write_event(
                    timestamp=timestamp,
                    latitude=latitude,
                    longitude=longitude,
                    fire_size_percent=fire_size_percent,
                    hot_percent=hot_percent,
                    max_temp=max_temp,
                    image_path=image_path,
                )

                print("FIREWATCH EVENT:")
                print(row)

                event_active = True

            time.sleep(LOOP_DELAY)

    except KeyboardInterrupt:
        print("\nStopping Firewatch sensor...")
    finally:
        try:
            camera.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
