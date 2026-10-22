"""Camera logins, admin login and server-side sessions.

Every request is turned into a Viewer: anonymous, signed in to one or more cameras, or admin.
Read paths ask the viewer what they may show; sessions live in SQLite so they can be revoked.
"""
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request

from database import TEST_CAMERA_ID, get_connection

SESSION_COOKIE = "fw_session"
SESSION_DAYS = 30
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, salt_hex, digest_hex = (stored or "").split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt((password or "").encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def generate_password():
    return secrets.token_urlsafe(12)


@dataclass(frozen=True)
class Viewer:
    is_admin: bool = False
    camera_ids: frozenset = frozenset()

    def can_see_camera(self, camera_id):
        return self.is_admin or camera_id in self.camera_ids

    @property
    def sees_tests(self):
        return self.is_admin

    @property
    def is_anonymous(self):
        return not self.is_admin and not self.camera_ids


ANONYMOUS = Viewer()


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def create_session(conn, *, is_admin=False, camera_ids=()):
    """Start a session and return its raw token (only the hash is stored)."""
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token_hash,is_admin,created_at,expires_at) VALUES (?,?,?,?)",
        (_hash_token(token), int(is_admin), now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()),
    )
    for camera_id in camera_ids:
        add_camera_to_session(conn, token, camera_id)
    return token


def add_camera_to_session(conn, token, camera_id):
    conn.execute("INSERT OR IGNORE INTO session_cameras(token_hash,camera_id) VALUES (?,?)",
                 (_hash_token(token), camera_id))


def load_viewer(conn, token):
    if not token:
        return ANONYMOUS
    token_hash = _hash_token(token)
    row = conn.execute("SELECT is_admin, expires_at FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) < _now():
        return ANONYMOUS
    cameras = conn.execute(
        """SELECT sc.camera_id FROM session_cameras sc JOIN cameras c ON c.camera_id = sc.camera_id
           WHERE sc.token_hash=? AND c.enabled=1 AND c.camera_id <> ?""",
        (token_hash, TEST_CAMERA_ID),
    ).fetchall()
    return Viewer(is_admin=bool(row["is_admin"]), camera_ids=frozenset(r["camera_id"] for r in cameras))


def delete_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def revoke_camera_sessions(conn, camera_id):
    """Sign everyone out of one camera; sessions holding other cameras keep those."""
    conn.execute("DELETE FROM session_cameras WHERE camera_id=?", (camera_id,))


def get_viewer(request: Request) -> Viewer:
    """FastAPI dependency: who is looking at this request."""
    from camera_api import is_admin  # imported here: camera_api also imports this module

    if is_admin(request.headers.get("authorization")):
        return Viewer(is_admin=True)
    with get_connection() as conn:
        return load_viewer(conn, request.cookies.get(SESSION_COOKIE))
