import os
import secrets
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from database import get_connection, hash_camera_key, utc_now

router = APIRouter(prefix="/api/admin")
ADMIN_TOKEN = os.getenv("FIREWATCH_ADMIN_TOKEN")


def _auth(authorization):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="FIREWATCH_ADMIN_TOKEN is not configured.")
    if not authorization or not authorization.startswith("Bearer ") or not secrets.compare_digest(authorization[7:], ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin credentials.")


def is_admin(authorization):
    """True when a valid admin bearer token is supplied (never raises)."""
    return bool(ADMIN_TOKEN and authorization and authorization.startswith("Bearer ")
                and secrets.compare_digest(authorization[7:], ADMIN_TOKEN))


class CameraCreate(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str = Field(default="", max_length=120)
    camera_key: str | None = Field(default=None, min_length=16, max_length=256)


class CameraUpdate(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    camera_key: str | None = Field(default=None, min_length=16, max_length=256)


class SubscriberCreate(BaseModel):
    camera_id: str = Field(default="*", min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    sms_enabled: bool = True
    email_enabled: bool = True


@router.get("/cameras")
def list_cameras(authorization: str | None = Header(default=None)):
    _auth(authorization)
    with get_connection() as conn:
        rows = conn.execute("SELECT camera_id,name,latitude,longitude,enabled,status,last_seen_at,software_version FROM cameras ORDER BY camera_id").fetchall()
    return {"cameras": [dict(r) for r in rows]}


@router.post("/cameras")
def create_camera(body: CameraCreate, authorization: str | None = Header(default=None)):
    _auth(authorization)
    key = body.camera_key or secrets.token_urlsafe(32)
    with get_connection() as conn:
        try:
            conn.execute("INSERT INTO cameras(camera_id,camera_key,latitude,longitude,name,created_at) VALUES (?,?,?,?,?,?)",
                         (body.camera_id, hash_camera_key(key), body.latitude, body.longitude, body.name or body.camera_id, utc_now()))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not create camera: {exc}")
    return {"ok": True, "camera_id": body.camera_id, "camera_key": key}


@router.put("/cameras/{camera_id}")
def update_camera(camera_id: str, body: CameraUpdate, authorization: str | None = Header(default=None)):
    _auth(authorization)
    fields, values = [], []
    for name in ["latitude", "longitude", "name", "enabled", "camera_key"]:
        value = getattr(body, name)
        if value is not None:
            fields.append(f"{name}=?")
            values.append(hash_camera_key(value) if name == "camera_key" else int(value) if name == "enabled" else value)
    if not fields:
        raise HTTPException(status_code=400, detail="No changes supplied.")
    values.append(camera_id)
    with get_connection() as conn:
        cur = conn.execute(f"UPDATE cameras SET {','.join(fields)} WHERE camera_id=?", values)
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Camera not found.")
    return {"ok": True, "camera_id": camera_id}


@router.delete("/cameras/{camera_id}")
def disable_camera(camera_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with get_connection() as conn:
        cur = conn.execute("UPDATE cameras SET enabled=0,status='disabled' WHERE camera_id=?", (camera_id,))
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Camera not found.")
    return {"ok": True, "camera_id": camera_id, "disabled": True}


@router.get("/subscribers")
def list_subscribers(authorization: str | None = Header(default=None)):
    _auth(authorization)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id,camera_id,email,phone,sms_enabled,email_enabled,enabled FROM subscribers ORDER BY id"
        ).fetchall()
    return {"subscribers": [dict(row) for row in rows]}


@router.post("/subscribers")
def create_subscriber(body: SubscriberCreate, authorization: str | None = Header(default=None)):
    _auth(authorization)
    email = (body.email or "").strip() or None
    phone = (body.phone or "").strip() or None
    if not email and not phone:
        raise HTTPException(status_code=400, detail="Provide an email address, phone number, or both.")
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="Email address does not look valid.")
    if phone and not all(char.isdigit() or char in "+- ()" for char in phone):
        raise HTTPException(status_code=400, detail="Phone number contains unsupported characters.")
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO subscribers(camera_id,email,phone,sms_enabled,email_enabled,enabled)
                   VALUES (?,?,?,?,?,1)""",
                (body.camera_id.strip(), email, phone, int(body.sms_enabled), int(body.email_enabled)),
            )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not add recipient: {exc}")
    return {"ok": True, "subscriber_id": cursor.lastrowid, "camera_id": body.camera_id, "email": email, "phone": phone}
