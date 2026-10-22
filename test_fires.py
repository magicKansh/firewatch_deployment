"""Admin test fires: fake detections that exercise the real pipeline but stay admin-only.

Every event created here has is_test=1. Public and camera-login views never include them, and
dedupe/false-alarm matching never mixes them with real events (see device_api).
"""
import json
import secrets

import numpy as np

import thermal
from database import IMAGE_DIR, TEST_CAMERA_ID, get_connection, hash_camera_key, utc_now
from device_api import create_event, new_event_id, notify_new_event
from fire_data import invalidate_cache

SEVERITY_INDEX = {"Low": 10, "Moderate": 30, "High": 55, "Extreme": 85}
# Same detector settings as the Pi defaults, so the heat-map looks like a real alert.
HOT_TEMP_C, AMBIENT_DELTA_C, MIN_CLUSTER_PIXELS = 80.0, 25.0, 6


def ensure_test_camera(conn):
    """The hidden, disabled camera that owns "drop a fire on the map" events."""
    row = conn.execute("SELECT * FROM cameras WHERE camera_id=?", (TEST_CAMERA_ID,)).fetchone()
    if not row:
        conn.execute(
            """INSERT INTO cameras(camera_id,camera_key,latitude,longitude,name,enabled,status,created_at)
               VALUES (?,?,0,0,'Test fires',0,'disabled',?)""",
            (TEST_CAMERA_ID, hash_camera_key(secrets.token_urlsafe(32)), utc_now()),
        )
        row = conn.execute("SELECT * FROM cameras WHERE camera_id=?", (TEST_CAMERA_ID,)).fetchone()
    return dict(row)


def synthetic_frame(max_temperature_c, hot_pixels, ambient_c=20.0):
    """A 24x32 thermal frame with exactly `hot_pixels` connected pixels above the detector
    threshold (hottest = max_temperature_c), a faint glow around them, and noisy background."""
    rows, cols = thermal.ROWS, thermal.COLS
    yy, xx = np.mgrid[0:rows, 0:cols]
    distance = np.hypot(yy - 12.3, xx - 20.2)          # off-centre so ties are rare
    order = np.argsort(distance, axis=None, kind="stable")
    hot = np.zeros(rows * cols, dtype=bool)
    hot[order[:hot_pixels]] = True
    hot = hot.reshape(rows, cols)

    rng = np.random.default_rng()
    temps = ambient_c + rng.uniform(-0.5, 0.5, size=(rows, cols))
    radius = distance[hot].max()
    glow = np.clip(1 - (distance - radius) / 3.0, 0, 1)  # fades out within 3 pixels of the hot spot
    temps += 35.0 * glow * ~hot                          # stays well under the 80 C threshold
    hot_distance = distance[hot]
    span = max(hot_distance.max() - hot_distance.min(), 1e-6)
    temps[hot] = max_temperature_c - 20.0 * (hot_distance - hot_distance.min()) / span
    return temps.astype(np.float32)


def simulate_detection(camera_id, max_temperature_c=350.0, hot_pixels=12, send_alerts=True):
    """Pretend `camera_id` saw a fire: heat-map + unconfirmed test event (+ real [TEST] alerts)."""
    if not 100 <= max_temperature_c <= 1000:
        raise ValueError("Max temperature must be between 100 and 1000 C.")
    if not 6 <= hot_pixels <= 200:
        raise ValueError("Hot spot size must be between 6 and 200 sensor pixels.")
    event_id = new_event_id()
    with get_connection() as conn:
        camera = conn.execute("SELECT * FROM cameras WHERE camera_id=? AND camera_id<>? AND enabled=1",
                              (camera_id, TEST_CAMERA_ID)).fetchone()
        if not camera:
            raise ValueError(f"Camera {camera_id} not found or disabled.")
        camera = dict(camera)
        temps = synthetic_frame(max_temperature_c, hot_pixels)
        stats = thermal.analyze(temps, HOT_TEMP_C, AMBIENT_DELTA_C, MIN_CLUSTER_PIXELS)
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        thermal.render(temps, stats, title=f"[TEST] {camera['name'] or camera_id}").save(
            IMAGE_DIR / f"{event_id}.jpg", format="JPEG", quality=90)
        row, token = create_event(
            conn, event_id, camera, fire_size_percent=stats.hot_percent, max_temperature_c=stats.max_temp,
            duration_seconds=60, image_path=f"images/{event_id}.jpg", detection_method="admin-simulation",
            hotspot_x=stats.hotspot_x, hotspot_y=stats.hotspot_y, is_test=True,
        )
    invalidate_cache()
    notifications = notify_new_event(row, token) if send_alerts else None
    return {"event_id": event_id, "token": token, "notifications": notifications}


def drop_test_fire(latitude, longitude, severity):
    """An already-confirmed test fire at a chosen spot (shows on the admin map and admin routing)."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Latitude must be -90..90 and longitude -180..180.")
    if severity not in SEVERITY_INDEX:
        raise ValueError(f"Severity must be one of: {', '.join(SEVERITY_INDEX)}.")
    event_id = new_event_id()
    with get_connection() as conn:
        create_event(
            conn, event_id, ensure_test_camera(conn), fire_size_percent=0.0, max_temperature_c=0.0,
            duration_seconds=0.0, detection_method="admin-drop", location=(latitude, longitude),
            status="confirmed", severity=(SEVERITY_INDEX[severity], severity), is_test=True,
        )
    invalidate_cache()
    return event_id


def list_test_fires():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM device_events WHERE is_test=1 ORDER BY timestamp DESC").fetchall()
    return [dict(r) for r in rows]


def _event_files(conn, row):
    paths = [row["image"], row["video"], *json.loads(row["context_images"] or "[]")]
    paths += [r["image"] for r in conn.execute("SELECT image FROM event_observations WHERE event_id=?", (row["event_id"],))]
    return {p for p in paths if p and p.startswith("images/")}


def delete_test_fires(event_ids=None):
    """Delete test events (all when event_ids is None) and their image files. Returns rows deleted."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM device_events WHERE is_test=1").fetchall()
        rows = [r for r in rows if event_ids is None or r["event_id"] in event_ids]
        files = set()
        for row in rows:
            files |= _event_files(conn, row)
            conn.execute("DELETE FROM device_events WHERE event_id=?", (row["event_id"],))
    for relative in files:
        (IMAGE_DIR / relative[len("images/"):]).unlink(missing_ok=True)
    if rows:
        invalidate_cache()
    return len(rows)
