"""Raspberry Pi node: MLX90640 heat detection -> heat-signature image -> upload for human review.

Runs thermal-only by default when no Pi Camera is attached. With a Pi Camera, it also adds
a visible photo, a short MP4 clip, and a simple warm-colour video heuristic.

This is a conservative prototype detector, not a trained fire-classification model.
Every detection creates an UNCONFIRMED event that a person reviews on Firewatch.
"""
import json
import os
import shutil
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
from PIL import Image

import thermal
from node_storage import Outbox, PermanentUploadError, SnapshotRing, default_snapshot_dir, utc_stamp

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name, default="0"):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


CONFIG_PATH = Path(os.getenv("FIREWATCH_CAMERA_CONFIG", str(BASE_DIR / "camera_config.json")))
HOT_TEMP_C = float(os.getenv("HOT_TEMP_C", "80"))
MIN_HOT_PERCENT = float(os.getenv("MIN_HOT_PERCENT", "0.5"))
MIN_CLUSTER_PIXELS = int(os.getenv("MIN_CLUSTER_PIXELS", "6"))
AMBIENT_DELTA_C = float(os.getenv("AMBIENT_DELTA_C", "25"))
REQUIRED_FRAMES = int(os.getenv("REQUIRED_FRAMES", "5"))
QUIET_FRAMES_TO_RESET = int(os.getenv("QUIET_FRAMES_TO_RESET", "30"))
EVENT_UPDATE_SECONDS = int(os.getenv("EVENT_UPDATE_SECONDS", "300"))
THERMAL_REFRESH_HZ = os.getenv("THERMAL_REFRESH_HZ", "4")
MAX_READ_ERRORS = int(os.getenv("MAX_READ_ERRORS", "60"))
THERMAL_FLIP_H = _env_bool("THERMAL_FLIP_H")
THERMAL_FLIP_V = _env_bool("THERMAL_FLIP_V")

SNAPSHOT_SECONDS = int(os.getenv("SNAPSHOT_SECONDS", "60"))
SNAPSHOT_KEEP = int(os.getenv("SNAPSHOT_KEEP", "5"))
CONTEXT_SNAPSHOTS = int(os.getenv("CONTEXT_SNAPSHOTS", "2"))
SNAPSHOT_DIR = Path(os.getenv("FIREWATCH_SNAPSHOT_DIR", str(default_snapshot_dir())))
OUTBOX_DIR = Path(os.getenv("FIREWATCH_OUTBOX_DIR", str(BASE_DIR / "outbox")))
OUTBOX_MAX = int(os.getenv("OUTBOX_MAX", "50"))
OUTBOX_RETRY_SECONDS = int(os.getenv("OUTBOX_RETRY_SECONDS", "60"))

PICAMERA_MODE = os.getenv("FIREWATCH_PICAMERA", "auto").strip().lower()   # auto | on | off
VISUAL_REQUIRED_FRAMES = int(os.getenv("VISUAL_REQUIRED_FRAMES", "4"))
VISUAL_FIRE_THRESHOLD = float(os.getenv("VISUAL_FIRE_THRESHOLD", "0.015"))
VISUAL_SAMPLE_SECONDS = float(os.getenv("VISUAL_SAMPLE_SECONDS", "0.5"))
CLIP_SECONDS = int(os.getenv("FIREWATCH_CLIP_SECONDS", "6"))

HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SERVER_URL = os.getenv("FIREWATCH_SERVER_URL", "http://localhost:8000").rstrip("/")
SOFTWARE_VERSION = os.getenv("FIREWATCH_NODE_VERSION", "2.0-thermal-review")


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as f:
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


# --- Hardware -----------------------------------------------------------------------------

def create_thermal():
    import adafruit_mlx90640
    import board
    import busio

    # The real bus speed comes from /boot/firmware/config.txt (dtparam=i2c_arm_baudrate);
    # see pi/install.sh. Higher refresh rates need the faster bus.
    i2c = busio.I2C(board.SCL, board.SDA, frequency=1_000_000)
    mlx = adafruit_mlx90640.MLX90640(i2c)
    rates = {"1": "REFRESH_1_HZ", "2": "REFRESH_2_HZ", "4": "REFRESH_4_HZ", "8": "REFRESH_8_HZ"}
    mlx.refresh_rate = getattr(adafruit_mlx90640.RefreshRate, rates.get(THERMAL_REFRESH_HZ, "REFRESH_4_HZ"))
    return mlx


def start_picamera():
    """Return a running Picamera2, or None for thermal-only operation."""
    if PICAMERA_MODE == "off":
        return None
    try:
        from picamera2 import Picamera2
        camera = Picamera2()
        camera.configure(camera.create_video_configuration(
            # H264 uses the YUV420 main stream; the RGB888 lores stream is for sampling/photos.
            main={"size": (1280, 720), "format": "YUV420"},
            lores={"size": (640, 360), "format": "RGB888"},
            controls={"FrameRate": 15},
        ))
        camera.start()
        time.sleep(2)
        return camera
    except Exception as exc:
        if PICAMERA_MODE == "on":
            raise
        print(f"No Pi Camera available, running thermal-only ({exc})")
        return None


def visual_flame_score(frame):
    """Warm, bright pixel fraction (0..1). A heuristic: red/yellow objects and sunlight trigger it."""
    if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
        return 0.0
    data = frame[:, :, :3].astype(np.uint16, copy=False)
    c0, green, c2 = data[:, :, 0], data[:, :, 1], data[:, :, 2]

    def warm(red, blue):
        return (red > 150) & (green > 65) & (red > green * 1.05) & (green > blue * 1.12) & ((red - blue) > 45)

    mask = warm(c0, c2) | warm(c2, c0)
    return float(np.count_nonzero(mask) / mask.size)


def save_visible_photo(camera, path):
    frame = camera.capture_array("lores")
    # Picamera2's RGB888 buffers are stored B,G,R; flip to RGB for Pillow.
    Image.fromarray(np.ascontiguousarray(frame[:, :, 2::-1])).save(path, format="JPEG", quality=88)


def record_clip(camera, video_path):
    """Record a short MP4. Returns an error string, or None on success."""
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FfmpegOutput
    try:
        encoder = H264Encoder(bitrate=2_000_000)
        encoder.output = FfmpegOutput(str(video_path))
        camera.start_encoder(encoder, name="firewatch-event")
        time.sleep(max(2, min(CLIP_SECONDS, 15)))
        camera.stop_encoder(name="firewatch-event")
        if not video_path.exists() or video_path.stat().st_size == 0:
            raise RuntimeError("Camera returned an empty video clip.")
        return None
    except Exception as exc:
        try:
            camera.stop_encoder(name="firewatch-event")
        except Exception:
            pass
        video_path.unlink(missing_ok=True)
        return str(exc)


# --- Server communication -------------------------------------------------------------------

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


def upload_event(form, files):
    """POST an event. files: list of (field, path, mime). Raises PermanentUploadError on 4xx."""
    with ExitStack() as stack:
        parts = [
            (field, (Path(path).name, stack.enter_context(open(path, "rb")), mime))
            for field, path, mime in files if Path(path).exists()
        ]
        has_video = any(field == "video" for field, _ in parts)
        response = requests.post(SERVER_URL + "/api/camera-event", data=form, files=parts,
                                 timeout=90 if has_video else 30)
    if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
        raise PermanentUploadError(f"HTTP {response.status_code}: {response.text[:200]}")
    response.raise_for_status()
    return response.json()


# --- Event packaging ------------------------------------------------------------------------

def build_event(cfg, temps, stats, duration, method, visual_score, snapshots, camera, work_dir):
    """Write event media into work_dir. Returns (form, files)."""
    stamp = utc_stamp()
    prefix = f"event_{cfg['camera_id']}_{stamp}"
    files = []

    heat_path = work_dir / f"{prefix}_heat.jpg"
    thermal.render(temps, stats, title=f"{cfg['camera_id']}  HEAT DETECTED").save(heat_path, "JPEG", quality=90)
    files.append(("image", heat_path, "image/jpeg"))

    # Copy (not move) the recent per-minute snapshots: they show what the scene looked like before.
    for snap in snapshots.recent(CONTEXT_SNAPSHOTS):
        copy = work_dir / f"{prefix}_before_{snap.name}"
        shutil.copyfile(snap, copy)
        files.append(("context_images", copy, "image/jpeg"))

    if camera is not None:
        photo = work_dir / f"{prefix}_visible.jpg"
        try:
            save_visible_photo(camera, photo)
            files.append(("context_images", photo, "image/jpeg"))
        except Exception as exc:
            print("Visible photo unavailable:", exc)
        video = work_dir / f"{prefix}.mp4"
        error = record_clip(camera, video)
        if error:
            print("Video clip unavailable; submitting images only:", error)
        else:
            files.append(("video", video, "video/mp4"))

    form = {
        "camera_id": cfg["camera_id"], "camera_key": cfg["camera_key"],
        "latitude": cfg["latitude"], "longitude": cfg["longitude"],
        "fire_size_percent": round(float(stats.hot_percent), 3),
        "max_temperature_c": round(float(stats.max_temp), 3),
        "duration_seconds": round(max(0.0, float(duration)), 2),
        "visual_fire_score": round(float(visual_score), 5),
        "detection_method": method,
        "hotspot_x": round(stats.hotspot_x, 2),
        "hotspot_y": round(stats.hotspot_y, 2),
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    return form, files


def submit(form, files, outbox):
    """Upload now, or park the event in the outbox if the server can't be reached."""
    try:
        print("Event submitted:", upload_event(form, files))
    except PermanentUploadError as exc:
        print("Server rejected the event (check camera_id/camera_key):", exc)
    except Exception as exc:
        print("Upload failed, queued for retry:", exc)
        outbox.put(form, files)
    finally:
        for _field, path, _mime in files:
            Path(path).unlink(missing_ok=True)


# --- Main loop ------------------------------------------------------------------------------

def main():
    cfg = load_config()
    mlx = create_thermal()
    camera = start_picamera()
    snapshots = SnapshotRing(SNAPSHOT_DIR, SNAPSHOT_KEEP)
    outbox = Outbox(OUTBOX_DIR, OUTBOX_MAX)
    work_dir = SNAPSHOT_DIR / "events"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    buffer = [0.0] * (thermal.ROWS * thermal.COLS)
    thermal_streak = visual_streak = quiet_streak = read_errors = 0
    event_active = False
    candidate_started = None
    last_hb = last_snapshot = last_visual = last_flush = last_update = -1e9
    visual_score = 0.0

    print(f"Firewatch camera {cfg['camera_id']} @ {cfg['latitude']}, {cfg['longitude']} -> {SERVER_URL}")
    print(f"Mode: {'thermal + Pi Camera' if camera else 'thermal only'}; snapshots every {SNAPSHOT_SECONDS}s "
          f"in {SNAPSHOT_DIR} (keeping {SNAPSHOT_KEEP}); outbox {OUTBOX_DIR}")
    try:
        while True:
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_SECONDS:
                heartbeat(cfg)
                last_hb = now
            if now - last_flush >= OUTBOX_RETRY_SECONDS:
                last_flush = now
                if outbox.items():
                    sent, dropped = outbox.flush(upload_event)
                    if sent or dropped:
                        print(f"Outbox: {sent} sent, {dropped} dropped, {len(outbox.items())} waiting")

            try:
                mlx.getFrame(buffer)
                read_errors = 0
            except Exception as exc:   # the MLX90640 driver raises on occasional bad I2C reads
                read_errors += 1
                print("Thermal read error:", exc)
                if read_errors >= MAX_READ_ERRORS:
                    # Exit so systemd restarts us; if the sensor is really gone, heartbeats stop
                    # and the dashboard shows the camera offline instead of falsely online.
                    raise SystemExit(f"Thermal sensor failed {read_errors} times in a row")
                time.sleep(0.5)
                continue
            temps = thermal.to_grid(buffer, THERMAL_FLIP_H, THERMAL_FLIP_V)
            stats = thermal.analyze(temps, HOT_TEMP_C, AMBIENT_DELTA_C, MIN_CLUSTER_PIXELS)
            if stats is None:
                time.sleep(0.05)
                continue

            if SNAPSHOT_SECONDS > 0 and now - last_snapshot >= SNAPSHOT_SECONDS:
                last_snapshot = now
                try:
                    snapshots.add(thermal.render(temps, stats, title=cfg["camera_id"]))
                except Exception as exc:
                    print("Snapshot failed:", exc)

            thermal_detected = (stats.max_temp >= HOT_TEMP_C and stats.hot_percent >= MIN_HOT_PERCENT
                                and stats.cluster >= MIN_CLUSTER_PIXELS)
            thermal_streak = thermal_streak + 1 if thermal_detected else 0

            if camera is not None and now - last_visual >= VISUAL_SAMPLE_SECONDS:
                last_visual = now
                try:
                    visual_score = visual_flame_score(camera.capture_array("lores"))
                except Exception as exc:
                    print("Video frame sampling error:", exc)
                    visual_score = 0.0
                visual_streak = visual_streak + 1 if visual_score >= VISUAL_FIRE_THRESHOLD else 0
            visual_hit = camera is not None and visual_score >= VISUAL_FIRE_THRESHOLD

            candidate = thermal_detected or visual_hit
            if candidate and candidate_started is None:
                candidate_started = now
            if not candidate and not event_active:
                candidate_started = None

            thermal_persistent = thermal_streak >= REQUIRED_FRAMES
            visual_persistent = visual_streak >= VISUAL_REQUIRED_FRAMES
            persistent = thermal_persistent or visual_persistent
            send_new = persistent and not event_active
            send_update = (event_active and candidate and EVENT_UPDATE_SECONDS > 0
                           and now - last_update >= EVENT_UPDATE_SECONDS)
            if send_new or send_update:
                method = ("thermal+video-heuristic" if thermal_persistent and visual_persistent
                          else "thermal" if thermal_persistent or not visual_persistent else "video-heuristic")
                duration = now - (candidate_started if candidate_started is not None else now)
                form, files = build_event(cfg, temps, stats, duration, method, visual_score,
                                          snapshots, camera, work_dir)
                submit(form, files, outbox)
                event_active, quiet_streak, last_update = True, 0, now

            if event_active:
                quiet_streak = 0 if candidate else quiet_streak + 1
                if quiet_streak >= QUIET_FRAMES_TO_RESET:
                    print("Heat source gone; ready for a new event.")
                    event_active = False
                    thermal_streak = visual_streak = quiet_streak = 0
                    candidate_started = None
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("Stopping Firewatch node.")
    finally:
        if camera is not None:
            try:
                camera.stop_encoder(name="firewatch-event")
            except Exception:
                pass
            camera.stop()


if __name__ == "__main__":
    main()
