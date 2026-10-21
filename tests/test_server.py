"""Server tests: detection -> review -> community dashboard.

Needs the server requirements:  pip install -r requirements.txt pytest httpx
Run with:  pytest   (or: python -m unittest discover -s tests)
"""
import importlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import fastapi  # noqa: F401
    import plotly  # noqa: F401
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

ADMIN = {"Authorization": "Bearer test-admin-token"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


@unittest.skipIf(TestClient is None, "server requirements not installed")
class ServerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {
            "FIREWATCH_DB": os.path.join(self.tmp.name, "fw.db"),
            "FIREWATCH_IMAGE_DIR": os.path.join(self.tmp.name, "images"),
            "FIREWATCH_ADMIN_TOKEN": "test-admin-token",
            "PUBLIC_BASE_URL": "https://firewatch.example",
            "TWILIO_ACCOUNT_SID": "AC123", "TWILIO_AUTH_TOKEN": "x", "TWILIO_FROM_NUMBER": "+15550000000",
            "FIRMS_API_KEY": "",
        }
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for name in ["database", "fire_data", "notifications", "camera_api", "device_api", "app", "routes", "map"]:
            sys.modules.pop(name, None)
        self.map = importlib.import_module("map")
        self.notifications = sys.modules["notifications"]
        self.client = TestClient(self.map.app)
        self.sent = []
        fake = mock.MagicMock()
        fake.return_value.messages.create.side_effect = lambda **kw: (self.sent.append(kw), mock.Mock(sid="SM1"))[1]
        self.twilio = mock.patch.object(self.notifications, "Client", fake)
        self.twilio.start()

        r = self.client.post("/api/admin/cameras", headers=ADMIN, json={
            "camera_id": "CAM-1", "latitude": 41.1, "longitude": -73.2, "name": "North Ridge"})
        self.assertEqual(r.status_code, 200, r.text)
        self.key = r.json()["camera_key"]
        r = self.client.post("/api/admin/subscribers", headers=ADMIN, json={"camera_id": "*", "phone": "+15551234567"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        self.twilio.stop()
        self.env.stop()
        self.tmp.cleanup()

    def send_event(self, hotspot=(10.0, 8.0), temp=150.0, context=2):
        files = [("image", ("event_CAM-1_heat.jpg", io.BytesIO(JPEG), "image/jpeg"))]
        files += [("context_images", (f"event_before_{i}.jpg", io.BytesIO(JPEG), "image/jpeg")) for i in range(context)]
        data = {"camera_id": "CAM-1", "camera_key": self.key, "latitude": 41.1, "longitude": -73.2,
                "fire_size_percent": 3.2, "max_temperature_c": temp, "duration_seconds": 4,
                "hotspot_x": hotspot[0], "hotspot_y": hotspot[1], "detected_at": "2026-01-01T00:00:00+00:00"}
        r = self.client.post("/api/camera-event", data=data, files=files)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def token(self):
        body = self.sent[-1]["body"]
        return body.split("token=")[1].split()[0]

    def public_events(self):
        return self.client.get("/api/device-events").json()

    def review(self, event_id, token, **form):
        return self.client.post(f"/review/{event_id}", data={"token": token, **form}, follow_redirects=False)

    # --- tests ---

    def test_detection_is_private_until_reviewed(self):
        result = self.send_event()
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["notifications"]["sms_sent"], 1)
        sms = self.sent[-1]
        self.assertIn(f"https://firewatch.example/review/{result['event_id']}?token=", sms["body"])
        self.assertEqual(sms["media_url"], [f"https://firewatch.example/images/{result['event_id']}.jpg"])

        public = self.public_events()
        self.assertEqual(public["events"], [])
        self.assertEqual(public["counts"], {"pending_review": 1, "confirmed": 0})
        admin = self.client.get("/api/device-events", headers=ADMIN).json()["events"]
        self.assertEqual(len(admin), 1)
        self.assertEqual(len(admin[0]["context_images"]), 2)
        self.assertEqual(admin[0]["first_seen_at"], "2026-01-01T00:00:00+00:00")
        names = [t.get("name") for t in self.client.get("/api/map-figure").json()["data"]]
        self.assertNotIn("Needs review", names)

    def test_opening_links_never_changes_anything(self):
        event_id = self.send_event()["event_id"]
        token = self.token()
        page = self.client.get(f"/review/{event_id}?token={token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Is this a fire?", page.text)
        self.assertIn(f"/images/{event_id}.jpg", page.text)
        legacy = self.client.get(f"/confirm/{event_id}/yes?token={token}", follow_redirects=False)
        self.assertEqual(legacy.status_code, 303)
        self.assertIn(f"/review/{event_id}?token={token}&answer=fire", legacy.headers["location"])
        self.client.get(legacy.headers["location"])
        admin = self.client.get("/api/device-events", headers=ADMIN).json()["events"]
        self.assertEqual(admin[0]["status"], "unconfirmed")

    def test_confirming_publishes_to_community_map(self):
        event_id = self.send_event()["event_id"]
        token = self.token()
        r = self.review(event_id, token, decision="fire")
        self.assertEqual(r.status_code, 303)
        public = self.public_events()
        self.assertEqual([e["event_id"] for e in public["events"]], [event_id])
        self.assertNotIn("review_note", public["events"][0])
        self.assertEqual(public["counts"], {"pending_review": 0, "confirmed": 1})
        names = [t.get("name") for t in self.client.get("/api/map-figure").json()["data"]]
        self.assertIn("Confirmed fires", names)
        # The link is single-use: a second answer doesn't flip it.
        self.review(event_id, token, decision="not_fire", label="sun_glare")
        page = self.client.get(f"/review/{event_id}?token={token}")
        self.assertIn("Confirmed as a fire", page.text)
        self.assertEqual(self.public_events()["counts"]["confirmed"], 1)

    def test_something_else_stays_private_and_quiets_same_spot(self):
        event_id = self.send_event(hotspot=(10, 8), temp=150)["event_id"]
        self.review(event_id, self.token(), decision="not_fire", label="chimney_grill", note="neighbour's grill")
        admin = self.client.get("/api/device-events", headers=ADMIN).json()["events"][0]
        self.assertEqual((admin["status"], admin["review_label"], admin["review_note"]),
                         ("false_alarm", "chimney_grill", "neighbour's grill"))
        self.assertEqual(self.public_events()["events"], [])

        texts = len(self.sent)
        repeat = self.send_event(hotspot=(11, 9), temp=155)
        self.assertTrue(repeat.get("suppressed"))
        self.assertEqual(len(self.sent), texts)                  # no new alert

        elsewhere = self.send_event(hotspot=(25, 3), temp=150)
        self.assertEqual(elsewhere["status"], "unconfirmed")     # different spot alerts
        self.assertEqual(len(self.sent), texts + 1)

    def test_same_spot_much_hotter_still_alerts(self):
        event_id = self.send_event(hotspot=(10, 8), temp=120)["event_id"]
        self.review(event_id, self.token(), decision="not_fire", label="sun_glare")
        self.assertEqual(self.send_event(hotspot=(10, 8), temp=300)["status"], "unconfirmed")

    def test_bad_tokens_and_admin_review(self):
        event_id = self.send_event()["event_id"]
        self.assertEqual(self.client.get(f"/review/{event_id}?token=nope").status_code, 403)
        self.assertEqual(self.review(event_id, "nope", decision="fire").status_code, 403)
        url = f"/api/admin/events/{event_id}/review"
        self.assertEqual(self.client.post(url, json={"decision": "fire"}).status_code, 401)
        r = self.client.post(url, headers=ADMIN, json={"decision": "fire"})
        self.assertEqual(r.json()["status"], "confirmed")
        self.assertEqual(len(self.public_events()["events"]), 1)

    def test_updates_while_burning_go_to_same_event(self):
        first = self.send_event()
        again = self.send_event(temp=200, context=0)
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
