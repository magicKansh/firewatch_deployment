import hashlib
import hmac
import json
import math
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from auth import Viewer, get_viewer
from camera_api import _auth as require_admin
from database import IMAGE_DIR, PUBLIC_STATUSES, TEST_CAMERA_ID, camera_key_matches, get_camera, get_connection  # noqa: F401 (re-exported)
from fire_data import invalidate_cache
from notifications import send_fire_confirmation

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(30 * 1024 * 1024)))
MAX_CONTEXT_IMAGES = int(os.getenv("MAX_CONTEXT_IMAGES", "4"))
EVENT_DEDUPE_SECONDS = int(os.getenv("EVENT_DEDUPE_SECONDS", "600"))
TOKEN_TTL_HOURS = int(os.getenv("CONFIRMATION_TOKEN_TTL_HOURS", "24"))
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "90"))
# After a reviewer marks a hotspot as "something else", the same spot (within RADIUS sensor
# pixels, and not much hotter) is logged quietly instead of re-alerting, for COOLDOWN seconds
# after it was last seen.
FALSE_ALARM_COOLDOWN_SECONDS = int(os.getenv("FALSE_ALARM_COOLDOWN_SECONDS", "3600"))
FALSE_ALARM_RADIUS_PX = float(os.getenv("FALSE_ALARM_RADIUS_PX", "3"))
FALSE_ALARM_TEMP_MARGIN_C = float(os.getenv("FALSE_ALARM_TEMP_MARGIN_C", "15"))

REVIEW_LABELS = {
    "fire": "Fire",
    "sun_glare": "Sun or reflection",
    "vehicle": "Vehicle or machinery",
    "chimney_grill": "Chimney, grill or campfire",
    "controlled_burn": "Controlled / permitted burn",
    "person_animal": "Person or animal",
    "other": "Something else",
}


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


def _save_upload(upload, subdir, name, allowed, default_suffix, max_bytes, what):
    suffix = Path(upload.filename or default_suffix).suffix.lower()
    if suffix not in allowed:
        suffix = default_suffix
    content = upload.file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{what} too large.")
    folder = IMAGE_DIR / subdir if subdir else IMAGE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}{suffix}").write_bytes(content)
    return "/".join(["images", *([subdir] if subdir else []), f"{name}{suffix}"])


def _save_image(image, event_id):
    if not image:
        return ""
    return _save_upload(image, "", event_id, {".jpg", ".jpeg", ".png"}, ".jpg", MAX_IMAGE_BYTES, "Image")


def _save_video(video, event_id):
    if not video:
        return ""
    return _save_upload(video, "videos", event_id, {".mp4", ".mov", ".webm", ".mkv"}, ".mp4", MAX_VIDEO_BYTES, "Video")


def _save_context(images, event_id):
    paths = []
    for index, image in enumerate((images or [])[:MAX_CONTEXT_IMAGES]):
        if image and image.filename:
            kind = "visible" if "visible" in image.filename else "before"   # set by the Pi's file names
            paths.append(_save_upload(image, "context", f"{event_id}_{index}_{kind}", {".jpg", ".jpeg", ".png"},
                                      ".jpg", MAX_IMAGE_BYTES, "Image"))
    return paths


def _dedupe_key(camera_id):
    return hashlib.sha256(camera_id.encode()).hexdigest()[:16]


def _parse_detected_at(value, now):
    """Accept the Pi's own detection time (events can be delayed by the offline queue)."""
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return now
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp if now - timedelta(days=7) <= stamp <= now + timedelta(minutes=5) else now


def _find_open_event(conn, camera_id, now, is_test=False):
    # Test events and real events never merge into each other.
    cutoff = (now - timedelta(seconds=EVENT_DEDUPE_SECONDS)).isoformat()
    return conn.execute(
        """SELECT * FROM device_events
           WHERE camera_id=? AND status IN ('unconfirmed','confirmed','active')
             AND last_seen_at >= ? AND is_test=?
           ORDER BY last_seen_at DESC LIMIT 1""",
        (camera_id, cutoff, int(is_test)),
    ).fetchone()


def _find_dismissed_match(conn, camera_id, now, hotspot_x, hotspot_y, max_temperature_c, is_test=False):
    """A recent false alarm at the same spot in the frame, if this detection repeats it."""
    if hotspot_x < 0 or hotspot_y < 0:
        return None
    cutoff = (now - timedelta(seconds=FALSE_ALARM_COOLDOWN_SECONDS)).isoformat()
    rows = conn.execute(
        """SELECT * FROM device_events
           WHERE camera_id=? AND status='false_alarm' AND last_seen_at >= ? AND is_test=?
             AND hotspot_x >= 0 AND hotspot_y >= 0
           ORDER BY last_seen_at DESC LIMIT 10""",
        (camera_id, cutoff, int(is_test)),
    ).fetchall()
    for row in rows:
        close = math.hypot(row["hotspot_x"] - hotspot_x, row["hotspot_y"] - hotspot_y) <= FALSE_ALARM_RADIUS_PX
        if close and max_temperature_c <= float(row["max_temperature_c"]) + FALSE_ALARM_TEMP_MARGIN_C:
            return row
    return None


def _add_observation(conn, event_id, now, fire_size_percent, max_temperature_c, duration_seconds, image_path):
    conn.execute(
        """INSERT INTO event_observations
        (event_id,timestamp,fire_size_percent,max_temperature_c,duration_seconds,image)
        VALUES (?,?,?,?,?,?)""",
        (event_id, now.isoformat(), fire_size_percent, max_temperature_c, duration_seconds, image_path),
    )


def _update_existing(conn, row, now, fire_size_percent, max_temperature_c, duration_seconds, image_path, video_path, visual_fire_score, detection_method):
    score, magnitude = _severity(fire_size_percent, duration_seconds, max_temperature_c)
    old_score = float(row["severity_index"])
    new_score = max(old_score, score)
    conn.execute(
        """UPDATE device_events SET timestamp=?,last_seen_at=?,fire_size_percent=?,
           max_temperature_c=?,duration_seconds=?,severity_index=?,magnitude=?,
           image=CASE WHEN ? <> '' THEN ? ELSE image END,
           video=CASE WHEN ? <> '' THEN ? ELSE video END,
           visual_fire_score=MAX(visual_fire_score, ?), detection_method=? WHERE event_id=?""",
        (now.isoformat(), now.isoformat(), max(float(row["fire_size_percent"]), fire_size_percent),
         max(float(row["max_temperature_c"]), max_temperature_c),
         max(float(row["duration_seconds"]), duration_seconds), new_score,
         magnitude if score >= old_score else row["magnitude"], image_path, image_path,
         video_path, video_path, visual_fire_score, detection_method[:80], row["event_id"]),
    )
    _add_observation(conn, row["event_id"], now, fire_size_percent, max_temperature_c, duration_seconds, image_path)
    return row["event_id"], new_score, magnitude


def new_event_id():
    return uuid.uuid4().hex[:12]


def create_event(conn, event_id, camera, *, fire_size_percent, max_temperature_c, duration_seconds,
                 image_path="", video_path="", context_paths=(), visual_fire_score=0.0,
                 detection_method="thermal", hotspot_x=-1.0, hotspot_y=-1.0, first_seen=None,
                 location=None, status="unconfirmed", severity=None, is_test=False, now=None):
    """Insert a new device event (+ first observation, + review token when it needs review).

    Shared by Pi uploads and admin test fires. Returns (row, raw review token or None).
    """
    now = now or utc_now()
    score, magnitude = severity or _severity(fire_size_percent, duration_seconds, max_temperature_c)
    latitude, longitude = location or (float(camera["latitude"]), float(camera["longitude"]))
    camera_id = camera["camera_id"]
    row = {
        "event_id": event_id, "camera_id": camera_id, "timestamp": now.isoformat(),
        "first_seen_at": (first_seen or now).isoformat(), "last_seen_at": now.isoformat(),
        "latitude": float(latitude), "longitude": float(longitude),
        "fire_size_percent": round(fire_size_percent, 2), "max_temperature_c": round(max_temperature_c, 2),
        "duration_seconds": round(duration_seconds, 1), "severity_index": score,
        "magnitude": magnitude, "status": status, "image": image_path,
        "video": video_path, "visual_fire_score": round(visual_fire_score, 4),
        "detection_method": detection_method[:80],
        "place": camera.get("name") or camera_id, "source": "device", "dedupe_key": _dedupe_key(camera_id),
        "context_images": json.dumps(list(context_paths)),
        "hotspot_x": hotspot_x if hotspot_x >= 0 else None,
        "hotspot_y": hotspot_y if hotspot_y >= 0 else None,
        "is_test": int(is_test),
    }
    if status == "confirmed":
        row["confirmed_at"] = now.isoformat()
    columns = list(row)
    conn.execute(
        f"INSERT INTO device_events ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
        tuple(row[c] for c in columns),
    )
    _add_observation(conn, event_id, now, fire_size_percent, max_temperature_c, duration_seconds, image_path)
    token = None
    if status == "unconfirmed":
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO confirmation_tokens(event_id,token_hash,created_at,expires_at) VALUES (?,?,?,?)",
            (event_id, _hash_token(token), now.isoformat(), (now + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()),
        )
    return row, token


def notify_new_event(row, token):
    """Send the review alert; never raises (a failed SMS must not lose the event)."""
    try:
        return send_fire_confirmation({**row, "confirm_token": token})
    except Exception as exc:
        return {"sms_sent": 0, "email_sent": 0, "error": str(exc)}


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
    video: UploadFile | None = File(None),
    context_images: list[UploadFile] | None = File(None),
    visual_fire_score: float = Form(0.0),
    detection_method: str = Form("thermal"),
    hotspot_x: float = Form(-1.0),
    hotspot_y: float = Form(-1.0),
    detected_at: str = Form(""),
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
    if not 0 <= visual_fire_score <= 1:
        raise HTTPException(status_code=400, detail="Visual fire score must be between 0 and 1.")

    now = utc_now()
    # Registered server coordinates are authoritative; submitted coordinates are diagnostics only.
    with get_connection() as conn:
        conn.execute("UPDATE cameras SET last_seen_at=?,status='online' WHERE camera_id=?", (now.isoformat(), camera_id))
        existing = _find_open_event(conn, camera_id, now)
        if existing:
            image_path = _save_image(image, f"{existing['event_id']}_{now.strftime('%H%M%S')}")
            video_path = _save_video(video, existing["event_id"])
            event_id, score, magnitude = _update_existing(
                conn, existing, now, fire_size_percent, max_temperature_c, duration_seconds,
                image_path, video_path, visual_fire_score, detection_method
            )
            return {"ok": True, "event_id": event_id, "deduplicated": True, "status": existing["status"],
                    "severity_index": score, "magnitude": magnitude}

        dismissed = _find_dismissed_match(conn, camera_id, now, hotspot_x, hotspot_y, max_temperature_c)
        if dismissed:
            conn.execute("UPDATE device_events SET last_seen_at=?, suppressed_count=suppressed_count+1 WHERE event_id=?",
                         (now.isoformat(), dismissed["event_id"]))
            _add_observation(conn, dismissed["event_id"], now, fire_size_percent, max_temperature_c, duration_seconds, "")
            return {"ok": True, "event_id": dismissed["event_id"], "suppressed": True, "status": "false_alarm",
                    "reason": "Same hotspot was recently reviewed as not a fire."}

        event_id = new_event_id()
        row, token = create_event(
            conn, event_id, camera, fire_size_percent=fire_size_percent, max_temperature_c=max_temperature_c,
            duration_seconds=duration_seconds, image_path=_save_image(image, event_id),
            video_path=_save_video(video, event_id), context_paths=_save_context(context_images, event_id),
            visual_fire_score=visual_fire_score, detection_method=detection_method,
            hotspot_x=hotspot_x, hotspot_y=hotspot_y, first_seen=_parse_detected_at(detected_at, now), now=now,
        )

    invalidate_cache()
    return {"ok": True, "event_id": event_id, "status": "unconfirmed", "severity_index": row["severity_index"],
            "magnitude": row["magnitude"], "notifications": notify_new_event(row, token)}


@router.post("/api/heartbeat")
async def heartbeat(
    camera_id: str = Form(...), camera_key: str = Form(...), software_version: str = Form("unknown"),
):
    _authenticate(camera_id, camera_key)
    now = utc_now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """UPDATE cameras SET last_seen_at=?,last_heartbeat_at=?,status='online',software_version=?
               WHERE camera_id=?""",
            (now, now, software_version[:100], camera_id),
        )
    return {"ok": True, "camera_id": camera_id, "status": "online", "server_time": now,
            "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS}


# --- Human review -------------------------------------------------------------------------------

def _apply_review(conn, event_id, decision, label, note, reviewer):
    """Record a reviewer's decision. 'fire' publishes the event to the community dashboard."""
    if decision not in {"fire", "not_fire"}:
        raise HTTPException(status_code=400, detail="Decision must be 'fire' or 'not_fire'.")
    label = "fire" if decision == "fire" else (label if label in REVIEW_LABELS and label != "fire" else "other")
    now = utc_now().isoformat()
    status = "confirmed" if decision == "fire" else "false_alarm"
    stamp_column = "confirmed_at" if status == "confirmed" else "false_alarm_at"
    cur = conn.execute(
        f"""UPDATE device_events SET status=?, {stamp_column}=?, review_label=?, review_note=?,
            reviewed_at=?, reviewed_by=? WHERE event_id=?""",
        (status, now, label, (note or "").strip()[:500], now, reviewer[:80], event_id),
    )
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Event not found.")
    conn.execute("UPDATE confirmation_tokens SET used_at=COALESCE(used_at, ?) WHERE event_id=?", (now, event_id))
    return status


def _token_state(conn, event_id, token):
    """Return 'ok', 'used', 'expired' or 'invalid' for a review link."""
    row = conn.execute("SELECT * FROM confirmation_tokens WHERE event_id=?", (event_id,)).fetchone()
    if not row or not token or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
        return "invalid"
    if row["used_at"]:
        return "used"
    if datetime.fromisoformat(row["expires_at"]) < utc_now():
        return "expired"
    return "ok"


def _event_for_display(row):
    event = dict(row)
    try:
        event["context_images"] = json.loads(event.get("context_images") or "[]")
    except ValueError:
        event["context_images"] = []
    event["review_label_text"] = REVIEW_LABELS.get(event.get("review_label") or "", "")
    return event


@router.get("/review/{event_id}", response_class=HTMLResponse)
def review_page(request: Request, event_id: str, token: str = "", answer: str = ""):
    """Read-only page; nothing changes until the reviewer presses a button (POST).

    Email security scanners open links automatically, so a GET must never confirm anything.
    """
    with get_connection() as conn:
        state = _token_state(conn, event_id, token)
        row = conn.execute("SELECT * FROM device_events WHERE event_id=?", (event_id,)).fetchone()
        camera = conn.execute("SELECT name FROM cameras WHERE camera_id=?", (row["camera_id"],)).fetchone() if row else None
    if state == "invalid" or not row:
        return templates.TemplateResponse(request=request, name="review.html", status_code=403,
                                          context={"state": "invalid", "event": None})
    return templates.TemplateResponse(request=request, name="review.html", context={
        "state": state, "event": _event_for_display(row), "token": token, "answer": answer,
        "camera_name": (camera["name"] if camera else "") or row["camera_id"],
        "labels": {k: v for k, v in REVIEW_LABELS.items() if k != "fire"},
    })


@router.post("/review/{event_id}")
def submit_review(event_id: str, token: str = Form(...), decision: str = Form(...),
                  label: str = Form("other"), note: str = Form("")):
    with get_connection() as conn:
        state = _token_state(conn, event_id, token)
        if state == "invalid":
            raise HTTPException(status_code=403, detail="Invalid review link.")
        if state == "ok":
            _apply_review(conn, event_id, decision, label, note, reviewer="review-link")
    if state == "ok":
        invalidate_cache()
    # Post/redirect/get: the page now shows the recorded outcome (or why it couldn't be recorded).
    return RedirectResponse(url=f"/review/{event_id}?token={token}", status_code=303)


@router.get("/confirm/{event_id}/{answer}")
def legacy_confirm(event_id: str, answer: str, token: str):
    """Old SMS links. They now open the review page instead of changing anything directly."""
    answer = "fire" if answer == "yes" else "not_fire" if answer == "no" else ""
    return RedirectResponse(url=f"/review/{event_id}?token={token}&answer={answer}", status_code=303)


class ReviewBody(BaseModel):
    decision: str = Field(pattern="^(fire|not_fire)$")
    label: str = Field(default="other", max_length=40)
    note: str = Field(default="", max_length=500)


@router.post("/api/admin/events/{event_id}/review")
def admin_review(event_id: str, body: ReviewBody, authorization: str | None = Header(default=None)):
    """Review from the admin side, e.g. when the alert was missed or the link expired."""
    require_admin(authorization)
    with get_connection() as conn:
        status = _apply_review(conn, event_id, body.decision, body.label, body.note, reviewer="admin")
    invalidate_cache()
    return {"ok": True, "event_id": event_id, "status": status}


# --- Read APIs ----------------------------------------------------------------------------------

PRIVATE_FIELDS = ("review_note", "reviewed_by", "dedupe_key", "suppressed_count")


@router.get("/api/device-events")
def device_events(status: str | None = None, limit: int = 100, viewer: Viewer = Depends(get_viewer)):
    """Everyone sees confirmed real fires; camera logins also see their own cameras' events;
    admins (session or Bearer token) see everything, including test fires."""
    limit = max(1, min(limit, 500))
    where, params = _visible_events_sql(viewer)
    if status:
        where += " AND status=?"
        params.append(status)
    with get_connection() as conn:
        rows = conn.execute(f"SELECT * FROM device_events WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                            (*params, limit)).fetchall()
        pending_where, pending_params = _visible_events_sql(viewer, include_tests=False)
        pending = conn.execute(f"SELECT COUNT(*) FROM device_events WHERE {pending_where} AND status='unconfirmed'",
                               pending_params).fetchone()[0]
        marks = ",".join("?" * len(PUBLIC_STATUSES))
        confirmed = conn.execute(f"SELECT COUNT(*) FROM device_events WHERE is_test=0 AND status IN ({marks})",
                                 PUBLIC_STATUSES).fetchone()[0]
    events = [_event_for_display(r) for r in rows]
    if not viewer.is_admin:
        for event in events:
            for field in PRIVATE_FIELDS:
                event.pop(field, None)
    return {"events": events, "counts": {"pending_review": pending, "confirmed": confirmed}}


def _visible_events_sql(viewer, include_tests=True):
    """WHERE clause (and params) for the device events this viewer may see."""
    if viewer.is_admin:
        return ("1=1", []) if include_tests else ("is_test=0", [])
    marks = ",".join("?" * len(PUBLIC_STATUSES))
    cameras = sorted(viewer.camera_ids)
    own = f" OR camera_id IN ({','.join('?' * len(cameras))})" if cameras else ""
    return f"is_test=0 AND (status IN ({marks}){own})", [*PUBLIC_STATUSES, *cameras]


@router.get("/api/cameras/status")
def camera_status(viewer: Viewer = Depends(get_viewer)):
    """Camera locations and health: own cameras for camera logins, all for admins, none for visitors."""
    if viewer.is_anonymous:
        return {"cameras": []}
    now = utc_now()
    with get_connection() as conn:
        rows = conn.execute("""SELECT camera_id,name,latitude,longitude,enabled,status,last_seen_at,last_heartbeat_at,
                               software_version FROM cameras WHERE camera_id<>? ORDER BY camera_id""",
                            (TEST_CAMERA_ID,)).fetchall()
    result = []
    for r in rows:
        if not viewer.can_see_camera(r["camera_id"]):
            continue
        item = dict(r)
        last = item.get("last_seen_at")
        if not item["enabled"]:
            item["status"] = "disabled"
        elif not last:
            item["status"] = "offline"
        else:
            age = (now - datetime.fromisoformat(last)).total_seconds()
            item["status"] = "online" if age <= HEARTBEAT_TIMEOUT_SECONDS else "stale" if age <= 300 else "offline"
            item["seconds_since_seen"] = round(age, 1)
        result.append(item)
    return {"cameras": result}
