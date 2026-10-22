import os
import secrets
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from auth import generate_password, hash_password, revoke_camera_sessions
from database import TEST_CAMERA_ID, get_connection, hash_camera_key, utc_now

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


# --- Camera helpers shared by the admin API, the admin panel and manage.py ---------------------------
# Each takes an open connection. Unknown cameras (and the hidden test camera) raise KeyError;
# invalid input raises ValueError with a message fit to show an admin.

def _require_camera(conn, camera_id):
    if camera_id == TEST_CAMERA_ID or not conn.execute("SELECT 1 FROM cameras WHERE camera_id=?", (camera_id,)).fetchone():
        raise KeyError(camera_id)


def add_camera(conn, camera_id, latitude, longitude, name="", camera_key=None):
    """Create a camera. Returns (pi_key, login_password); both are shown once and stored hashed."""
    camera_id = (camera_id or "").strip()
    if not camera_id or len(camera_id) > 64 or camera_id == TEST_CAMERA_ID:
        raise ValueError("Camera ID must be 1-64 characters.")
    if not (-90 <= float(latitude) <= 90 and -180 <= float(longitude) <= 180):
        raise ValueError("Latitude must be -90..90 and longitude -180..180.")
    if conn.execute("SELECT 1 FROM cameras WHERE camera_id=?", (camera_id,)).fetchone():
        raise ValueError(f"Camera {camera_id} already exists.")
    pi_key = camera_key or secrets.token_urlsafe(32)
    login_password = generate_password()
    conn.execute(
        """INSERT INTO cameras(camera_id,camera_key,latitude,longitude,name,created_at,login_password_hash)
           VALUES (?,?,?,?,?,?,?)""",
        (camera_id, hash_camera_key(pi_key), float(latitude), float(longitude), (name or "").strip()[:120] or camera_id,
         utc_now(), hash_password(login_password)),
    )
    return pi_key, login_password


def reset_login_password(conn, camera_id):
    """New login password for a camera; everyone signed in to it is signed out."""
    _require_camera(conn, camera_id)
    password = generate_password()
    conn.execute("UPDATE cameras SET login_password_hash=? WHERE camera_id=?", (hash_password(password), camera_id))
    revoke_camera_sessions(conn, camera_id)
    return password


def reset_pi_key(conn, camera_id):
    """New device key (must be copied to the Pi). Camera logins are not affected."""
    _require_camera(conn, camera_id)
    key = secrets.token_urlsafe(32)
    conn.execute("UPDATE cameras SET camera_key=? WHERE camera_id=?", (hash_camera_key(key), camera_id))
    return key


def set_camera_enabled(conn, camera_id, enabled):
    _require_camera(conn, camera_id)
    if enabled:
        conn.execute("UPDATE cameras SET enabled=1 WHERE camera_id=?", (camera_id,))
    else:
        conn.execute("UPDATE cameras SET enabled=0,status='disabled' WHERE camera_id=?", (camera_id,))
        revoke_camera_sessions(conn, camera_id)


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
        rows = conn.execute("SELECT camera_id,name,latitude,longitude,enabled,status,last_seen_at,software_version FROM cameras WHERE camera_id<>? ORDER BY camera_id", (TEST_CAMERA_ID,)).fetchall()
    return {"cameras": [dict(r) for r in rows]}


@router.post("/cameras")
def create_camera(body: CameraCreate, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with get_connection() as conn:
        try:
            key, login_password = add_camera(conn, body.camera_id, body.latitude, body.longitude, body.name, body.camera_key)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"Could not create camera: {exc}")
    return {"ok": True, "camera_id": body.camera_id.strip(), "camera_key": key, "login_password": login_password}


@router.post("/cameras/{camera_id}/login-password")
def new_login_password(camera_id: str, authorization: str | None = Header(default=None)):
    """Generate a new camera login password (returned once) and sign that camera out everywhere."""
    _auth(authorization)
    with get_connection() as conn:
        try:
            password = reset_login_password(conn, camera_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Camera not found.")
    return {"ok": True, "camera_id": camera_id, "login_password": password}


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
        cur = conn.execute(f"UPDATE cameras SET {','.join(fields)} WHERE camera_id=? AND camera_id<>'{TEST_CAMERA_ID}'", values)
        if cur.rowcount and body.enabled is False:
            revoke_camera_sessions(conn, camera_id)
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Camera not found.")
    return {"ok": True, "camera_id": camera_id}


@router.delete("/cameras/{camera_id}")
def disable_camera(camera_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with get_connection() as conn:
        try:
            set_camera_enabled(conn, camera_id, False)
        except KeyError:
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
