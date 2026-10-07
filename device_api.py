import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from database import camera_key_matches, get_camera, get_connection
from fire_data import invalidate_cache
from notifications import send_fire_confirmation

router = APIRouter()
IMAGE_DIR = Path(__file__).resolve().parent / "images"
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
EVENT_DEDUPE_SECONDS = int(os.getenv("EVENT_DEDUPE_SECONDS", "600"))
TOKEN_TTL_HOURS = int(os.getenv("CONFIRMATION_TOKEN_TTL_HOURS", "24"))
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "90"))


def utc_now():
    return datetime.now(timezone.utc)


def _severity(fire_size_percent, duration_seconds, max_temperature_c):
    area_factor = min(max(float(fire_size_percent) / 50.0, 0.0), 1.0)
    duration_factor = min(max(float(duration_seconds) / 1800.0, 0.0), 1.0)
    temp_factor = min(max((float(max_temperature_c) - 60.0) / 140.0, 0.0), 1.0)
    score = 100.0 * (0.55 * area_factor + 0.30 * duration_factor + 0.15 * temp_factor)
    label = "Low" if score < 20 else "Moderate" if score < 45 else "High" if score < 70 else "Extreme"
    return round(score, 1), label


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _authenticate(camera_id, camera_key):
    camera = get_camera(camera_id)
    if not camera or not camera_key_matches(camera["camera_key"], camera_key):
        raise HTTPException(status_code=401, detail="Invalid camera credentials.")
    return camera


def _save_image(image, event_id):
    if not image:
        return ""
    IMAGE_DIR.mkdir(exist_ok=True)
    suffix = Path(image.filename or ".jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        suffix = ".jpg"
    content = image.file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large.")
    name = f"{event_id}{suffix}"
    (IMAGE_DIR / name).write_bytes(content)
    return f"images/{name}"


def _dedupe_key(camera_id):
    return hashlib.sha256(camera_id.encode()).hexdigest()[:16]


def _find_open_event(conn, camera_id, now):
    cutoff = (now - timedelta(seconds=EVENT_DEDUPE_SECONDS)).isoformat()
    return conn.execute(
        """SELECT * FROM device_events
           WHERE camera_id=? AND status IN ('unconfirmed','confirmed','active')
             AND last_seen_at >= ?
           ORDER BY last_seen_at DESC LIMIT 1""",
        (camera_id, cutoff),
    ).fetchone()


def _update_existing(conn, row, now, fire_size_percent, max_temperature_c, duration_seconds, image_path):
    score, magnitude = _severity(fire_size_percent, duration_seconds, max_temperature_c)
    old_score = float(row["severity_index"])
    old_temp = float(row["max_temperature_c"])
    old_area = float(row["fire_size_percent"])
    old_duration = float(row["duration_seconds"])
    new_score = max(old_score, score)
    new_status = row["status"]
    conn.execute(
        """UPDATE device_events SET timestamp=?,last_seen_at=?,fire_size_percent=?,
           max_temperature_c=?,duration_seconds=?,severity_index=?,magnitude=?,
           image=CASE WHEN ? <> '' THEN ? ELSE image END,status=? WHERE event_id=?""",
        (now.isoformat(), now.isoformat(), max(old_area, fire_size_percent),
         max(old_temp, max_temperature_c), max(old_duration, duration_seconds), new_score,
         magnitude if score >= old_score else row["magnitude"], image_path, image_path,
         new_status, row["event_id"]),
    )
    conn.execute(
        """INSERT INTO event_observations
        (event_id,timestamp,fire_size_percent,max_temperature_c,duration_seconds,image)
        VALUES (?,?,?,?,?,?)""",
        (row["event_id"], now.isoformat(), fire_size_percent, max_temperature_c, duration_seconds, image_path),
    )
    return row["event_id"], False, new_score, magnitude


@router.post("/api/camera-event")
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
    camera = _authenticate(camera_id, camera_key)
    if not 0 <= fire_size_percent <= 100:
        raise HTTPException(status_code=400, detail="Invalid fire size.")
    if duration_seconds < 0 or duration_seconds > 86400:
        raise HTTPException(status_code=400, detail="Invalid duration.")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise HTTPException(status_code=400, detail="Invalid coordinates.")
    if max_temperature_c < -100 or max_temperature_c > 1000:
        raise HTTPException(status_code=400, detail="Invalid temperature.")

    now = utc_now()
    image_path = ""
    # Registered server coordinates are authoritative; submitted coordinates are diagnostics only.
    with get_connection() as conn:
        existing = _find_open_event(conn, camera_id, now)
        if existing:
            image_path = _save_image(image, existing["event_id"]) if image else ""
            event_id, created, score, magnitude = _update_existing(
                conn, existing, now, fire_size_percent, max_temperature_c, duration_seconds, image_path
            )
            conn.execute(
                "UPDATE cameras SET last_seen_at=?,status='online' WHERE camera_id=?",
                (now.isoformat(), camera_id),
            )
            return {"ok": True, "event_id": event_id, "deduplicated": True, "status": existing["status"],
                    "severity_index": score, "magnitude": magnitude}

        event_id = uuid.uuid4().hex[:12]
        token = secrets.token_urlsafe(32)
        score, magnitude = _severity(fire_size_percent, duration_seconds, max_temperature_c)
        image_path = _save_image(image, event_id) if image else ""
        row = {
            "event_id": event_id, "camera_id": camera_id, "timestamp": now.isoformat(),
            "first_seen_at": now.isoformat(), "last_seen_at": now.isoformat(),
            "latitude": float(camera["latitude"]), "longitude": float(camera["longitude"]),
            "fire_size_percent": round(fire_size_percent, 2), "max_temperature_c": round(max_temperature_c, 2),
            "duration_seconds": round(duration_seconds, 1), "severity_index": score,
            "magnitude": magnitude, "status": "unconfirmed", "image": image_path,
            "place": camera.get("name") or camera_id, "source": "device", "dedupe_key": _dedupe_key(camera_id),
        }
        conn.execute(
            """INSERT INTO device_events
            (event_id,camera_id,timestamp,first_seen_at,last_seen_at,latitude,longitude,fire_size_percent,
             max_temperature_c,duration_seconds,severity_index,magnitude,status,image,place,source,dedupe_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row[k] for k in ["event_id","camera_id","timestamp","first_seen_at","last_seen_at","latitude","longitude",
                                   "fire_size_percent","max_temperature_c","duration_seconds","severity_index","magnitude",
                                   "status","image","place","source","dedupe_key"]),
        )
        conn.execute(
            """INSERT INTO event_observations(event_id,timestamp,fire_size_percent,max_temperature_c,duration_seconds,image)
            VALUES (?,?,?,?,?,?)""",
            (event_id, now.isoformat(), fire_size_percent, max_temperature_c, duration_seconds, image_path),
        )
        conn.execute(
            "INSERT INTO confirmation_tokens(event_id,token_hash,created_at,expires_at) VALUES (?,?,?,?)",
            (event_id, _hash_token(token), now.isoformat(), (now + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()),
        )
        conn.execute("UPDATE cameras SET last_seen_at=?,status='online' WHERE camera_id=?", (now.isoformat(), camera_id))

    invalidate_cache()
    try:
        result = send_fire_confirmation({**row, "confirm_token": token})
    except Exception as exc:
        result = {"sms_sent": 0, "email_sent": 0, "error": str(exc)}

    return {"ok": True, "event_id": event_id, "status": "unconfirmed", "severity_index": score,
            "magnitude": magnitude, "notifications": result}


@router.post("/api/heartbeat")
async def heartbeat(
    camera_id: str = Form(...), camera_key: str = Form(...), software_version: str = Form("unknown"),
):
    camera = _authenticate(camera_id, camera_key)
    now = utc_now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """UPDATE cameras SET last_seen_at=?,last_heartbeat_at=?,status='online',software_version=?
               WHERE camera_id=?""",
            (now, now, software_version[:100], camera_id),
        )
    return {"ok": True, "camera_id": camera_id, "status": "online", "server_time": now,
            "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS}


@router.get("/confirm/{event_id}/{answer}")
def confirm_event(event_id: str, answer: str, token: str):
    if answer not in {"yes", "no"}:
        raise HTTPException(status_code=400, detail="Invalid confirmation.")
    now = utc_now()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM confirmation_tokens WHERE event_id=?", (event_id,)).fetchone()
        if not row or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
            raise HTTPException(status_code=403, detail="Invalid confirmation token.")
        if row["used_at"] or datetime.fromisoformat(row["expires_at"]) < now:
            raise HTTPException(status_code=403, detail="Confirmation token expired or already used.")
        status = "confirmed" if answer == "yes" else "false_alarm"
        if status == "confirmed":
            conn.execute("UPDATE device_events SET status=?,confirmed_at=? WHERE event_id=?", (status, now.isoformat(), event_id))
        else:
            conn.execute("UPDATE device_events SET status=?,false_alarm_at=? WHERE event_id=?", (status, now.isoformat(), event_id))
        conn.execute("UPDATE confirmation_tokens SET used_at=? WHERE event_id=?", (now.isoformat(), event_id))
    invalidate_cache()
    return RedirectResponse(url=f"/?confirmed={status}", status_code=303)


@router.get("/api/device-events")
def device_events(status: str | None = None, limit: int = 100):
    limit = max(1, min(limit, 500))
    with get_connection() as conn:
        if status:
            rows = conn.execute("SELECT * FROM device_events WHERE status=? ORDER BY timestamp DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM device_events ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return {"events": [dict(r) for r in rows]}


@router.get("/api/cameras/status")
def camera_status():
    now = utc_now()
    with get_connection() as conn:
        rows = conn.execute("SELECT camera_id,name,latitude,longitude,enabled,status,last_seen_at,last_heartbeat_at,software_version FROM cameras ORDER BY camera_id").fetchall()
    result = []
    for r in rows:
        item = dict(r)
        last = item.get("last_seen_at")
        if not item["enabled"]:
            item["status"] = "disabled"
        elif not last:
            item["status"] = "offline"
        else:
            age = (now - datetime.fromisoformat(last)).total_seconds()
            item["status"] = "online" if age <= 60 else "stale" if age <= 300 else "offline"
            item["seconds_since_seen"] = round(age, 1)
        result.append(item)
    return {"cameras": result}
