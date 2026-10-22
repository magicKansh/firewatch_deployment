"""Shared base class for server tests that need cameras, logins and a fresh database."""
import importlib
import os
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
