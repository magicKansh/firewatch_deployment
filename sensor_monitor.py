"""Raspberry Pi node: MLX90640 thermal sampling, heat-map snapshots and event upload.

Every SAMPLE_SECONDS the node reads one thermal frame and saves a heat-map snapshot to
a temp folder. Cool snapshots are deleted. A hot snapshot triggers a short burst of
extra frames; if most of them are hot too, the hottest frame's heat-map (plus a Pi
Camera photo, when a camera is attached) is queued in PENDING_DIR and uploaded as an
UNCONFIRMED event for human review. Queued files are only deleted after the server
accepts them, so events survive network outages and reboots.
"""
import functools
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from thermal import analyze, render_heatmap

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("FIREWATCH_CAMERA_CONFIG", str(BASE_DIR / "camera_config.json")))
TEMP_DIR = Path(os.getenv("FIREWATCH_TEMP_DIR", str(Path(tempfile.gettempdir()) / "firewatch")))
PENDING_DIR = Path(os.getenv("FIREWATCH_PENDING_DIR", str(BASE_DIR / "pending")))
SERVER_URL = os.getenv("FIREWATCH_SERVER_URL", "http://localhost:8000").rstrip("/")
SOFTWARE_VERSION = os.getenv("FIREWATCH_NODE_VERSION", "2.0-thermal")

HOT_TEMP_C = float(os.getenv("HOT_TEMP_C", "80"))
AMBIENT_DELTA_C = float(os.getenv("AMBIENT_DELTA_C", "25"))
MIN_CLUSTER_PIXELS = int(os.getenv("MIN_CLUSTER_PIXELS", "6"))
MIN_HOT_PERCENT = float(os.getenv("MIN_HOT_PERCENT", "0"))
SAMPLE_SECONDS = float(os.getenv("SAMPLE_SECONDS", "60"))
CONFIRM_FRAMES = int(os.getenv("CONFIRM_FRAMES", "5"))
CONFIRM_INTERVAL_SECONDS = float(os.getenv("CONFIRM_INTERVAL_SECONDS", "1"))
EVENT_RESET_SAMPLES = int(os.getenv("EVENT_RESET_SAMPLES", "3"))
MAX_PENDING_EVENTS = int(os.getenv("MAX_PENDING_EVENTS", "20"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SENSOR_READ_ATTEMPTS = 3


def load_config(path=CONFIG_PATH):
    with Path(path).open(encoding="utf-8") as f:
        cfg = json.load(f)
    required = ["camera_id", "camera_key", "latitude", "longitude"]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise RuntimeError(f"Missing camera configuration: {', '.join(missing)}")
    lat, lon = float(cfg["latitude"]), float(cfg["longitude"])
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise RuntimeError("Camera latitude/longitude is outside valid geographic bounds.")
    cfg["latitude"], cfg["longitude"] = lat, lon
    return cfg


def _analyze(temps):
    return analyze(temps, HOT_TEMP_C, AMBIENT_DELTA_C, MIN_CLUSTER_PIXELS, MIN_HOT_PERCENT)


def _stamp():
    now = datetime.now(timezone.utc)
    # Sortable timestamp plus a short random suffix so names never collide.
    return now.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6], now


def _unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


class Node:
    def __init__(self, cfg, read_frame, upload, capture_photo=None,
                 temp_dir=TEMP_DIR, pending_dir=PENDING_DIR, sleep=time.sleep):
        self.cfg = cfg
        self.read_frame = read_frame        # () -> 24x32 temperatures in C
        self.upload = upload                # (meta, heatmap_path, photo_path|None) -> raises on failure
        self.capture_photo = capture_photo  # (path) -> saves a JPEG, or None without a Pi Camera
        self.temp_dir = Path(temp_dir)
        self.pending_dir = Path(pending_dir)
        self.sleep = sleep
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.event_active = False
        self.cool_samples = 0

    def _read(self):
        for attempt in range(SENSOR_READ_ATTEMPTS):
            try:
                return _analyze_frame(self.read_frame())
            except Exception as exc:
                print(f"Thermal read error (attempt {attempt + 1}):", exc)
                self.sleep(0.5)
        return None

    def sample(self):
        """Take one scheduled snapshot; queue and upload an event if heat is confirmed."""
        reading = self._read()
        if reading is None:
            return
        temps, stats = reading
        name, now = _stamp()
        snapshot = self.temp_dir / f"sample_{name}.jpg"
        render_heatmap(temps, stats, self._label(now)).save(snapshot, format="JPEG", quality=85)
        print(f"Sample: max {stats['max_temp']:.1f} C, background {stats['ambient']:.1f} C, "
              f"cluster {stats['cluster_pixels']} px, hot={stats['hot']}")
        try:
            if not stats["hot"]:
                self.cool_samples += 1
                if self.event_active and self.cool_samples >= EVENT_RESET_SAMPLES:
                    self.event_active = False
                return
            self.cool_samples = 0
            if self.event_active:
                return  # Same fire is still burning; the reviewer already has an alert.
            started = time.monotonic()
            confirmed = self._confirm(temps, stats)
            if confirmed:
                self._queue(name, now, *confirmed, duration=time.monotonic() - started)
                self.event_active = True
        finally:
            _unlink(snapshot)
        self.flush()

    def _confirm(self, temps, stats):
        """Read a burst of frames; return the hottest (temps, stats) if most are hot."""
        best, hot_count = (temps, stats), 0
        for _ in range(CONFIRM_FRAMES):
            self.sleep(CONFIRM_INTERVAL_SECONDS)
            reading = self._read()
            if reading is None or not reading[1]["hot"]:
                continue
            hot_count += 1
            if reading[1]["cluster_pixels"] > best[1]["cluster_pixels"]:
                best = reading
        if hot_count * 2 > CONFIRM_FRAMES:
            return best
        print(f"Heat not confirmed ({hot_count}/{CONFIRM_FRAMES} burst frames hot); ignoring.")
        return None

    def _label(self, now):
        return f"{self.cfg['camera_id']}  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC"

    def _queue(self, name, now, temps, stats, duration):
        heatmap = self.pending_dir / f"{name}.jpg"
        render_heatmap(temps, stats, self._label(now)).save(heatmap, format="JPEG", quality=90)
        if self.capture_photo:
            photo = self.pending_dir / f"{name}_photo.jpg"
            try:
                self.capture_photo(photo)
            except Exception as exc:
                print("Pi Camera photo failed; sending heat-map only:", exc)
                _unlink(photo)
        meta = {
            "captured_at": now.isoformat(),
            "fire_size_percent": round(stats["hot_percent"], 3),
            "max_temperature_c": round(stats["max_temp"], 3),
            "duration_seconds": round(max(0.0, duration), 2),
            "detection_method": "thermal",
        }
        # Write the JSON last (atomically): its presence marks the event as complete.
        tmp = self.pending_dir / f"{name}.json.tmp"
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        tmp.replace(self.pending_dir / f"{name}.json")
        print("Fire event queued:", name)
        self._prune()

    def _files(self, meta_path):
        name = meta_path.name[: -len(".json")]
        return meta_path, self.pending_dir / f"{name}.jpg", self.pending_dir / f"{name}_photo.jpg"

    def _prune(self):
        queued = sorted(self.pending_dir.glob("*.json"))
        for meta_path in queued[: max(0, len(queued) - MAX_PENDING_EVENTS)]:
            print("Pending queue full; dropping oldest event", meta_path.name)
            for path in self._files(meta_path):
                _unlink(path)

    def flush(self):
        """Upload queued events oldest-first. Stops at the first failure. Returns count sent."""
        sent = 0
        for meta_path in sorted(self.pending_dir.glob("*.json")):
            meta_path, heatmap, photo = self._files(meta_path)
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self.upload(meta, heatmap, photo if photo.exists() else None)
            except Exception as exc:
                print("Upload failed; will retry:", exc)
                break
            for path in (meta_path, heatmap, photo):
                _unlink(path)
            sent += 1
            print("Event uploaded:", meta_path.name)
        return sent


def _analyze_frame(temps):
    stats = _analyze(temps)
    if stats is None:
        raise ValueError("Thermal frame contained no valid pixels.")
    return temps, stats


def upload_event(cfg, meta, heatmap_path, photo_path):
    form = {
        "camera_id": cfg["camera_id"], "camera_key": cfg["camera_key"],
        "latitude": cfg["latitude"], "longitude": cfg["longitude"],
        "fire_size_percent": meta["fire_size_percent"],
        "max_temperature_c": meta["max_temperature_c"],
        "duration_seconds": meta["duration_seconds"],
        "detection_method": meta["detection_method"],
    }
    with open(heatmap_path, "rb") as image:
        files = {"image": (Path(heatmap_path).name, image, "image/jpeg")}
        if photo_path:
            with open(photo_path, "rb") as photo:
                files["photo"] = (Path(photo_path).name, photo, "image/jpeg")
                response = requests.post(SERVER_URL + "/api/camera-event", data=form, files=files, timeout=60)
        else:
            response = requests.post(SERVER_URL + "/api/camera-event", data=form, files=files, timeout=60)
    response.raise_for_status()
    return response.json()


def heartbeat(cfg):
    try:
        response = requests.post(SERVER_URL + "/api/heartbeat", data={
            "camera_id": cfg["camera_id"], "camera_key": cfg["camera_key"],
            "software_version": SOFTWARE_VERSION,
        }, timeout=15)
        response.raise_for_status()
        return True
    except Exception as exc:
        print("Heartbeat failed:", exc)
        return False


def create_thermal_reader():
    import adafruit_mlx90640
    import board
    import busio
    import numpy as np

    i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
    mlx = adafruit_mlx90640.MLX90640(i2c)
    mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
    buffer = [0.0] * 768

    def read_frame():
        mlx.getFrame(buffer)
        return np.asarray(buffer, dtype=np.float32).reshape(24, 32)

    return read_frame


def create_photo_capture():
    """Return a photo-capture function if a Pi Camera is attached, else None."""
    try:
        from picamera2 import Picamera2

        camera = Picamera2()
        camera.configure(camera.create_still_configuration(main={"size": (1280, 720)}))
        camera.start()
        time.sleep(2)
    except Exception as exc:
        print(f"No Pi Camera available ({exc}); running thermal-only.")
        return None
    print("Pi Camera found; alerts will include a photo.")
    return lambda path: camera.capture_file(str(path))


def main():
    cfg = load_config()
    node = Node(cfg, create_thermal_reader(), functools.partial(upload_event, cfg),
                capture_photo=create_photo_capture())
    print(f"Firewatch camera {cfg['camera_id']} @ fixed location {cfg['latitude']}, {cfg['longitude']}")
    print(f"Server: {SERVER_URL}; sampling every {SAMPLE_SECONDS:.0f}s; pending queue: {node.pending_dir}")
    last_heartbeat = last_sample = float("-inf")
    try:
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                heartbeat(cfg)
                node.flush()  # retry anything left over from an outage
                last_heartbeat = now
            if now - last_sample >= SAMPLE_SECONDS:
                node.sample()
                last_sample = now
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping Firewatch node.")


if __name__ == "__main__":
    main()
