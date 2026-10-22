"""Shared base class for server tests that need cameras, logins and a fresh database."""
import importlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

ADMIN = {"Authorization": "Bearer test-admin-token"}
ADMIN_TOKEN = "test-admin-token"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
# Every module that caches database paths or settings at import time; reloaded per test.
APP_MODULES = ["database", "ratelimit", "fire_data", "notifications", "camera_api", "auth", "device_api",
               "test_fires", "admin_panel", "app", "routes", "map"]


def reload_app():
    for name in APP_MODULES:
        sys.modules.pop(name, None)
    return importlib.import_module("map")


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        env = {
            "FIREWATCH_DB": os.path.join(self.tmp.name, "fw.db"),
            "FIREWATCH_IMAGE_DIR": os.path.join(self.tmp.name, "images"),
            "FIREWATCH_ADMIN_TOKEN": ADMIN_TOKEN,
            "PUBLIC_BASE_URL": "https://firewatch.example",
            "TWILIO_ACCOUNT_SID": "AC123", "TWILIO_AUTH_TOKEN": "x", "TWILIO_FROM_NUMBER": "+15550000000",
            "FIRMS_API_KEY": "",
        }
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.map = reload_app()
        self.notifications = sys.modules["notifications"]
        self.sent = []
        fake = mock.MagicMock()
        fake.return_value.messages.create.side_effect = lambda **kw: (self.sent.append(kw), mock.Mock(sid="SM1"))[1]
        twilio = mock.patch.object(self.notifications, "Client", fake)
        twilio.start()
        self.addCleanup(twilio.stop)
        self.client = self.new_client()

        self.keys = {}
        for camera_id, name, lat, lon in [("CAM-A", "Alpha Ridge", 41.1, -73.2), ("CAM-B", "Bravo Hill", 42.0, -74.0)]:
            r = self.client.post("/api/admin/cameras", headers=ADMIN, json={
                "camera_id": camera_id, "latitude": lat, "longitude": lon, "name": name})
            self.assertEqual(r.status_code, 200, r.text)
            self.keys[camera_id] = r.json()["camera_key"]
        r = self.client.post("/api/admin/subscribers", headers=ADMIN, json={"camera_id": "*", "phone": "+15555550123"})
        self.assertEqual(r.status_code, 200, r.text)

    def new_client(self):
        # https so the Secure session cookie is sent back.
        return TestClient(self.map.app, base_url="https://testserver")

    def db(self):
        return sys.modules["database"].get_connection()

    def set_login_password(self, camera_id):
        auth = sys.modules["auth"]
        password = auth.generate_password()
        with self.db() as conn:
            conn.execute("UPDATE cameras SET login_password_hash=? WHERE camera_id=?",
                         (auth.hash_password(password), camera_id))
        return password

    def login_camera(self, client, camera_id, password):
        return client.post("/login", data={"camera_id": camera_id, "password": password}, follow_redirects=False)

    def login_admin(self, client):
        return client.post("/admin/login", data={"password": ADMIN_TOKEN}, follow_redirects=False)

    def upload_event(self, camera_id, temp=150.0, hotspot=(10.0, 8.0)):
        files = [("image", (f"event_{camera_id}_heat.jpg", io.BytesIO(JPEG), "image/jpeg"))]
        data = {"camera_id": camera_id, "camera_key": self.keys[camera_id], "latitude": 0, "longitude": 0,
                "fire_size_percent": 3.2, "max_temperature_c": temp, "duration_seconds": 4,
                "hotspot_x": hotspot[0], "hotspot_y": hotspot[1]}
        r = self.client.post("/api/camera-event", data=data, files=files)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def token_from_last_sms(self):
        return re.search(r"token=([\w-]+)", self.sent[-1]["body"]).group(1)

    def make_event(self, camera_id="CAM-A", *, status="unconfirmed", is_test=False, location=None):
        """Insert an event directly (bypasses dedupe). Returns (row, review token or None)."""
        api = sys.modules["device_api"]
        with self.db() as conn:
            camera = dict(conn.execute("SELECT * FROM cameras WHERE camera_id=?", (camera_id,)).fetchone())
            return api.create_event(conn, api.new_event_id(), camera, fire_size_percent=2.0,
                                    max_temperature_c=300.0, duration_seconds=5, status=status,
                                    is_test=is_test, location=location)

    def make_visibility_fixture(self):
        """Clients for each kind of viewer, and one event of each kind they should or shouldn't see."""
        self.anon = self.new_client()
        self.cam_a = self.new_client()
        self.login_camera(self.cam_a, "CAM-A", self.set_login_password("CAM-A"))
        self.admin = self.new_client()
        self.login_admin(self.admin)
        self.a_unconfirmed = self.upload_event("CAM-A")["event_id"]
        self.b_unconfirmed = self.upload_event("CAM-B")["event_id"]
        self.b_confirmed = self.make_event("CAM-B", status="confirmed")[0]["event_id"]
        self.test_confirmed = self.make_event("CAM-A", status="confirmed", is_test=True, location=(45.0, -70.0))[0]["event_id"]
        self.test_unconfirmed = self.make_event("CAM-A", is_test=True)[0]["event_id"]
        sys.modules["fire_data"].invalidate_cache()
