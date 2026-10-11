"""Raspberry Pi node: thermal persistence + video flame heuristic + event clip upload.

This is a conservative prototype detector, not a trained fire-classification model.
Thermal/video evidence creates an UNCONFIRMED event for human review on Firewatch.
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
from PIL import Image
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("FIREWATCH_CAMERA_CONFIG", str(BASE_DIR / "camera_config.json")))
HOT_TEMP_C = float(os.getenv("HOT_TEMP_C", "80"))
MIN_HOT_PERCENT = float(os.getenv("MIN_HOT_PERCENT", "10"))
MIN_CLUSTER_PIXELS = int(os.getenv("MIN_CLUSTER_PIXELS", "6"))
AMBIENT_DELTA_C = float(os.getenv("AMBIENT_DELTA_C", "25"))
REQUIRED_FRAMES = int(os.getenv("REQUIRED_FRAMES", "5"))
VISUAL_REQUIRED_FRAMES = int(os.getenv("VISUAL_REQUIRED_FRAMES", "4"))
VISUAL_FIRE_THRESHOLD = float(os.getenv("VISUAL_FIRE_THRESHOLD", "0.015"))
VISUAL_SAMPLE_SECONDS = float(os.getenv("VISUAL_SAMPLE_SECONDS", "0.5"))
CLIP_SECONDS = int(os.getenv("FIREWATCH_CLIP_SECONDS", "6"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SERVER_URL = os.getenv("FIREWATCH_SERVER_URL", "http://localhost:8000").rstrip("/")
SOFTWARE_VERSION = os.getenv("FIREWATCH_NODE_VERSION", "1.2-video")


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


def create_thermal():
    i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
    mlx = adafruit_mlx90640.MLX90640(i2c)
    mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_8_HZ
    return mlx


def largest_component(mask):
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    largest = 0
    for y, x in zip(*np.where(mask)):
        if seen[y, x]:
            continue
        stack = [(y, x)]
        seen[y, x] = True
        count = 0
        while stack:
            cy, cx = stack.pop()
            count += 1
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if not (dy or dx):
                        continue
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        largest = max(largest, count)
    return largest


def get_thermal_stats(mlx, frame):
    mlx.getFrame(frame)
    temps = np.asarray(frame, dtype=np.float32).reshape(24, 32)
    valid = np.isfinite(temps)
    if not valid.any():
        return None
    vals = temps[valid]
    max_temp = float(np.max(vals))
    ambient = float(np.percentile(vals, 60))
    threshold = max(HOT_TEMP_C, ambient + AMBIENT_DELTA_C)
    hot = valid & (temps >= threshold)
    cluster = largest_component(hot)
    hot_percent = float(hot.sum() / valid.sum() * 100.0) if cluster >= MIN_CLUSTER_PIXELS else 0.0
    return max_temp, hot_percent, ambient, cluster


def visual_flame_score(frame):
    """Return warm, bright pixel fraction (0..1) for RGB888 or BGR-shaped frames.

    This lightweight heuristic deliberately does not claim to classify fire reliably;
    red/yellow objects, sunlight and reflections can trigger it.
    """
    if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
        return 0.0
    data = frame[:, :, :3].astype(np.uint16, copy=False)
    c0, green, c2 = data[:, :, 0], data[:, :, 1], data[:, :, 2]
    def warm(red, blue):
        return (red > 150) & (green > 65) & (red > green * 1.05) & (green > blue * 1.12) & ((red - blue) > 45)
    # Picamera2 RGB888 buffer order can vary with the selected stream/driver.
    mask = warm(c0, c2) | warm(c2, c0)
    return float(np.count_nonzero(mask) / mask.size)


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


def capture_event_media(camera, image_path, video_path):
    """Save a snapshot and short MP4 clip. Video failure leaves the snapshot usable."""
    frame = camera.capture_array("lores")
    Image.fromarray(frame).save(image_path, format="JPEG", quality=90, optimize=True)
    video_error = None
    try:
        encoder = H264Encoder(bitrate=2_000_000)
        encoder.output = FfmpegOutput(str(video_path))
        # The camera is already running for frame sampling, so attach/detach
        # only the encoder instead of attempting to start the camera twice.
        camera.start_encoder(encoder, name="firewatch-event")
        time.sleep(max(2, min(CLIP_SECONDS, 15)))
        camera.stop_encoder(name="firewatch-event")
        if not video_path.exists() or video_path.stat().st_size == 0:
            raise RuntimeError("Camera returned an empty video clip.")
    except Exception as exc:
        video_error = str(exc)
        # Do not leave a recording active if the encoder/output failed mid-session.
        try:
            camera.stop_encoder(name="firewatch-event")
        except Exception:
            pass
        try:
            if video_path.exists() and video_path.stat().st_size == 0:
                video_path.unlink()
        except OSError:
            pass
    return video_error


def send_event(cfg, image_path, video_path, max_temp, hot_percent, duration, visual_score, method):
    form = {
        "camera_id": cfg["camera_id"], "camera_key": cfg["camera_key"],
        "latitude": cfg["latitude"], "longitude": cfg["longitude"],
        "fire_size_percent": round(float(hot_percent), 3),
        "max_temperature_c": round(float(max_temp), 3),
        "duration_seconds": round(max(0.0, float(duration)), 2),
        "visual_fire_score": round(float(visual_score), 5),
        "detection_method": method,
    }
    files = {}
    with open(image_path, "rb") as image_file:
        files["image"] = (Path(image_path).name, image_file, "image/jpeg")
        if video_path and Path(video_path).exists() and Path(video_path).stat().st_size:
            with open(video_path, "rb") as video_file:
                files["video"] = (Path(video_path).name, video_file, "video/mp4")
                response = requests.post(SERVER_URL + "/api/camera-event", data=form, files=files, timeout=90)
                response.raise_for_status()
                return response.json()
        response = requests.post(SERVER_URL + "/api/camera-event", data=form, files=files, timeout=30)
        response.raise_for_status()
        return response.json()


def main():
    cfg = load_config()
    mlx = create_thermal()
    camera = Picamera2()
    camera.configure(camera.create_video_configuration(
        # H264 encoding uses the camera's YUV420 main stream. The lower-rate
        # RGB888 lores stream is used for lightweight visual sampling/snapshots.
        main={"size": (1280, 720), "format": "YUV420"},
        lores={"size": (640, 360), "format": "RGB888"},
        controls={"FrameRate": 15},
    ))
    camera.start()
    time.sleep(2)

    thermal_buffer = [0.0] * 768
    thermal_streak = 0
    visual_streak = 0
    event_active = False
    candidate_started = None
    last_hb = 0.0
    last_visual_sample = 0.0
    latest_visual_score = 0.0
    latest_thermal = (0.0, 0.0, 0.0, 0)
    reset_streak = 0

    print(f"Firewatch camera {cfg['camera_id']} @ fixed location {cfg['latitude']}, {cfg['longitude']}")
    print(f"Server: {SERVER_URL}; visual sampling every {VISUAL_SAMPLE_SECONDS}s")
    try:
        while True:
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_SECONDS:
                heartbeat(cfg)
                last_hb = now

            try:
                thermal = get_thermal_stats(mlx, thermal_buffer)
            except Exception as exc:
                print("Thermal read error:", exc)
                time.sleep(0.5)
                continue
            if thermal is None:
                time.sleep(0.05)
                continue
            latest_thermal = thermal
            max_temp, hot_percent, _ambient, cluster = thermal
            thermal_detected = max_temp >= HOT_TEMP_C and hot_percent >= MIN_HOT_PERCENT and cluster >= MIN_CLUSTER_PIXELS
            thermal_streak = thermal_streak + 1 if thermal_detected else 0

            if now - last_visual_sample >= VISUAL_SAMPLE_SECONDS:
                try:
                    latest_visual_score = visual_flame_score(camera.capture_array("lores"))
                except Exception as exc:
                    print("Video frame sampling error:", exc)
                    latest_visual_score = 0.0
                visual_streak = visual_streak + 1 if latest_visual_score >= VISUAL_FIRE_THRESHOLD else 0
                last_visual_sample = now

            candidate = thermal_detected or latest_visual_score >= VISUAL_FIRE_THRESHOLD
            if candidate and candidate_started is None:
                candidate_started = now
            if not candidate:
                candidate_started = None

            thermal_persistent = thermal_streak >= REQUIRED_FRAMES
            visual_persistent = visual_streak >= VISUAL_REQUIRED_FRAMES
            if (thermal_persistent or visual_persistent) and not event_active:
                duration = now - (candidate_started if candidate_started is not None else now)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                image_path = BASE_DIR / f"event_{cfg['camera_id']}_{stamp}.jpg"
                video_path = BASE_DIR / f"event_{cfg['camera_id']}_{stamp}.mp4"
                try:
                    video_error = capture_event_media(camera, image_path, video_path)
                    if video_error:
                        print("Video clip unavailable; submitting snapshot only:", video_error)
                    if thermal_persistent and visual_persistent:
                        method = "thermal+video-heuristic"
                    elif thermal_persistent:
                        method = "thermal"
                    else:
                        method = "video-heuristic"
                    result = send_event(
                        cfg, image_path, video_path if not video_error else None,
                        latest_thermal[0], latest_thermal[1], duration, latest_visual_score, method,
                    )
                    print("Event submitted:", result)
                except Exception as exc:
                    print("Could not submit fire event:", exc)
                finally:
                    for path in (image_path, video_path):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                event_active = True
                reset_streak = 0

            if event_active:
                if not thermal_detected and latest_visual_score < VISUAL_FIRE_THRESHOLD:
                    reset_streak += 1
                else:
                    reset_streak = 0
                if reset_streak >= 30:
                    event_active = False
                    reset_streak = 0
                    thermal_streak = visual_streak = 0
                    candidate_started = None
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("Stopping Firewatch node.")
    finally:
        try:
            camera.stop_encoder(name="firewatch-event")
        except Exception:
            pass
        camera.stop()


if __name__ == "__main__":
    main()
