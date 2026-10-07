import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from database import get_camera, get_connection, migrate_legacy_csvs
from fire_data import invalidate_cache
from map import app
from notifications import send_fire_confirmation

IMAGE_DIR = Path(__file__).resolve().parent / "images"

migrate_legacy_csvs()


def _severity(fire_size_percent, duration_seconds, max_temperature_c):
    """Relative 0-100 severity index, not a physical fire-energy measurement."""
    area_factor = min(max(float(fire_size_percent) / 50.0, 0.0), 1.0)
    duration_factor = min(max(float(duration_seconds) / 1800.0, 0.0), 1.0)
    temp_factor = min(max((float(max_temperature_c) - 60.0) / 140.0, 0.0), 1.0)
    score = 100.0 * (0.55 * area_factor + 0.30 * duration_factor + 0.15 * temp_factor)
    if score < 20:
        label = "Low"
    elif score < 45:
        label = "Moderate"
    elif score < 70:
        label = "High"
    else:
        label = "Extreme"
    return round(score, 1), label


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


@app.post("/api/camera-event")
async def camera_event(
    camera_id: str = Form(...),
    camera_key: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    fire_size_percent: float = Form(...),
    max_temperature_c: float = Form(...),
    duration_seconds: float = Form(...),
    image: UploadFile | None = File(None),
):
    camera = get_camera(camera_id)
    if not camera or not secrets.compare_digest(str(camera["camera_key"]), str(camera_key)):
        raise HTTPException(status_code=401, detail="Invalid camera credentials.")

    # Registered server coordinates are authoritative; the Pi does not set its location.
    latitude = float(camera["latitude"])
    longitude = float(camera["longitude"])

    if not 0 <= fire_size_percent <= 100:
        raise HTTPException(status_code=400, detail="Invalid fire size.")
    if duration_seconds < 0:
        raise HTTPException(status_code=400, detail="Invalid duration.")

    event_id = uuid.uuid4().hex[:12]
    confirm_token = secrets.token_urlsafe(32)
    score, magnitude = _severity(fire_size_percent, duration_seconds, max_temperature_c)

    image_name = ""
    if image:
        IMAGE_DIR.mkdir(exist_ok=True)
        suffix = Path(image.filename or ".jpg").suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png"}:
            suffix = ".jpg"
        content = await image.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Image too large.")
        image_name = f"{event_id}{suffix}"
        (IMAGE_DIR / image_name).write_bytes(content)

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "event_id": event_id,
        "camera_id": camera_id,
        "timestamp": now,
        "latitude": latitude,
        "longitude": longitude,
        "fire_size_percent": round(fire_size_percent, 2),
        "max_temperature_c": round(max_temperature_c, 2),
        "duration_seconds": round(duration_seconds, 1),
        "severity_index": score,
        "magnitude": magnitude,
        "status": "unconfirmed",
        "image": f"images/{image_name}" if image_name else "",
        "place": camera.get("name") or camera_id,
        "source": "device",
    }

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO device_events
            (event_id,camera_id,timestamp,latitude,longitude,fire_size_percent,
             max_temperature_c,duration_seconds,severity_index,magnitude,status,
             image,place,source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row[k] for k in [
                "event_id","camera_id","timestamp","latitude","longitude",
                "fire_size_percent","max_temperature_c","duration_seconds",
                "severity_index","magnitude","status","image","place","source"
            ])
        )
        conn.execute(
            "INSERT INTO confirmation_tokens(event_id,token_hash,created_at) VALUES (?,?,?)",
            (event_id, _hash_token(confirm_token), now),
        )

    invalidate_cache()
    try:
        result = send_fire_confirmation({**row, "confirm_token": confirm_token})
    except Exception as exc:
        result = {"sent": 0, "error": str(exc)}
        print(f"Notification error: {exc}")

    return {
        "ok": True,
        "event_id": event_id,
        "status": "unconfirmed",
        "severity_index": score,
        "magnitude": magnitude,
        "notifications": result,
    }


def _token_valid(event_id, token):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT token_hash FROM confirmation_tokens WHERE event_id=?", (event_id,)
        ).fetchone()
    if not row:
        return False
    return hmac.compare_digest(row["token_hash"], _hash_token(token))


@app.get("/confirm/{event_id}/{answer}")
def confirm_event(event_id: str, answer: str, token: str):
    if answer not in {"yes", "no"}:
        raise HTTPException(status_code=400, detail="Invalid confirmation.")
    if not _token_valid(event_id, token):
        raise HTTPException(status_code=403, detail="Invalid confirmation token.")

    status = "confirmed" if answer == "yes" else "false_alarm"
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE device_events SET status=? WHERE event_id=?", (status, event_id)
        ).rowcount
    if not updated:
        raise HTTPException(status_code=404, detail="Event not found.")

    invalidate_cache()
    return RedirectResponse(url=f"/?confirmed={status}", status_code=303)


@app.get("/api/device-events")
def device_events():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM device_events ORDER BY timestamp DESC").fetchall()
    return {"events": [dict(r) for r in rows]}
