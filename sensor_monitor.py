"""Raspberry Pi node: MLX90640 detection + Pi camera + Firewatch heartbeat."""
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
MIN_CLUSTER_PIXELS = int(os.getenv("MIN_CLUSTER_PIXELS", "6"))
AMBIENT_DELTA_C = float(os.getenv("AMBIENT_DELTA_C", "25"))
REQUIRED_FRAMES = int(os.getenv("REQUIRED_FRAMES", "5"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SERVER_URL = os.getenv("FIREWATCH_SERVER_URL", "http://localhost:8000").rstrip("/")
SOFTWARE_VERSION = os.getenv("FIREWATCH_NODE_VERSION", "1.1")


def load_config():
    with CONFIG_PATH.open() as f: cfg = json.load(f)
    required = ["camera_id", "camera_key", "latitude", "longitude"]
    missing = [x for x in required if x not in cfg]
    if missing: raise RuntimeError(f"Missing camera configuration: {', '.join(missing)}")
    return cfg


def create_thermal():
    i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
    mlx = adafruit_mlx90640.MLX90640(i2c)
    mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_8_HZ
    return mlx


def largest_component(mask):
    h, w = mask.shape; seen = np.zeros_like(mask, dtype=bool); largest = 0
    for y, x in zip(*np.where(mask)):
        if seen[y, x]: continue
        stack = [(y, x)]; seen[y, x] = True; count = 0
        while stack:
            cy, cx = stack.pop(); count += 1
            for dy in (-1,0,1):
                for dx in (-1,0,1):
                    if not (dy or dx): continue
                    ny, nx = cy+dy, cx+dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny,nx] and not seen[ny,nx]:
                        seen[ny,nx] = True; stack.append((ny,nx))
        largest = max(largest, count)
    return largest


def get_stats(mlx, frame):
    mlx.getFrame(frame)
    temps = np.asarray(frame, dtype=np.float32).reshape(24, 32)
    valid = np.isfinite(temps)
    if not valid.any(): return None
    vals = temps[valid]; max_temp = float(np.max(vals))
    ambient = float(np.percentile(vals, 60))
    threshold = max(HOT_TEMP_C, ambient + AMBIENT_DELTA_C)
    hot = valid & (temps >= threshold)
    cluster = largest_component(hot)
    # Area uses the actual hot mask, while the cluster gate rejects isolated hot pixels.
    hot_percent = float(hot.sum() / valid.sum() * 100.0) if cluster >= MIN_CLUSTER_PIXELS else 0.0
    return max_temp, hot_percent, ambient, cluster


def post_form(path, cfg, extra):
    data = {"camera_id": cfg["camera_id"], "camera_key": cfg["camera_key"], **extra}
    return requests.post(SERVER_URL + path, data=data, timeout=15)


def heartbeat(cfg):
    try:
        r = post_form("/api/heartbeat", cfg, {"software_version": SOFTWARE_VERSION})
        r.raise_for_status(); return True
    except Exception as exc:
        print("Heartbeat failed:", exc); return False


def send_event(cfg, image_path, max_temp, hot_percent, duration):
    with open(image_path, "rb") as f:
        files = {"image": (Path(image_path).name, f, "image/jpeg")}
        data = {"camera_id": cfg["camera_id"], "camera_key": cfg["camera_key"],
                "latitude": cfg["latitude"], "longitude": cfg["longitude"],
                "fire_size_percent": hot_percent, "max_temperature_c": max_temp,
                "duration_seconds": duration}
        r = requests.post(SERVER_URL + "/api/camera-event", data=data, files=files, timeout=30)
    r.raise_for_status(); return r.json()


def main():
    cfg = load_config(); mlx = create_thermal(); camera = Picamera2()
    camera.configure(camera.create_still_configuration()); camera.start(); time.sleep(2)
    frame = [0.0] * 768; consecutive = 0; event_active = False; started_at = None; last_hb = 0
    print(f"Firewatch camera {cfg['camera_id']} @ {cfg['latitude']}, {cfg['longitude']}")
    try:
        while True:
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_SECONDS:
                heartbeat(cfg); last_hb = now
            try: stats = get_stats(mlx, frame)
            except Exception as exc: print("Thermal read error:", exc); time.sleep(1); continue
            if stats is None: continue
            max_temp, hot_percent, ambient, cluster = stats
            detected = max_temp >= HOT_TEMP_C and hot_percent >= MIN_HOT_PERCENT and cluster >= MIN_CLUSTER_PIXELS
            if detected:
                if consecutive == 0: started_at = now
                consecutive += 1
            else:
                consecutive = 0; started_at = None; event_active = False
            if detected and consecutive >= REQUIRED_FRAMES and not event_active:
                duration = now - started_at
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                image_path = BASE_DIR / f"fire_{stamp}.jpg"; camera.capture_file(str(image_path))
                try: print("Event submitted:", send_event(cfg, image_path, max_temp, hot_percent, duration))
                except Exception as exc: print("Could not contact server:", exc)
                event_active = True
                try: image_path.unlink()
                except OSError: pass
            time.sleep(.05)
    except KeyboardInterrupt: print("Stopping.")
    finally: camera.stop()

if __name__ == "__main__": main()
