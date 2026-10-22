"""Camera logins, per-viewer visibility and the admin control panel."""
import os
import sys

from fastapi.testclient import TestClient

from tests.support import ServerTestCase, reload_app


class AuthCoreTests(ServerTestCase):
    def test_password_hash_roundtrip(self):
        auth = sys.modules["auth"]
        stored = auth.hash_password("pw-123")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(auth.verify_password("pw-123", stored))
        self.assertFalse(auth.verify_password("pw-124", stored))
        self.assertFalse(auth.verify_password("pw-123", None))
        self.assertNotEqual(stored, auth.hash_password("pw-123"))  # salted

    def test_session_viewer_and_revocation(self):
        auth = sys.modules["auth"]
        with self.db() as conn:
            token = auth.create_session(conn, camera_ids=["CAM-A"])
            auth.add_camera_to_session(conn, token, "CAM-B")
            viewer = auth.load_viewer(conn, token)
            self.assertEqual(viewer.camera_ids, frozenset({"CAM-A", "CAM-B"}))
            self.assertTrue(viewer.can_see_camera("CAM-B") and not viewer.sees_tests)
            auth.revoke_camera_sessions(conn, "CAM-A")
            self.assertEqual(auth.load_viewer(conn, token).camera_ids, frozenset({"CAM-B"}))
            row = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertNotEqual(row["token_hash"], token)  # only the hash is stored

    def test_disabled_camera_drops_out_of_session(self):  # Review Focus 2
        auth = sys.modules["auth"]
        with self.db() as conn:
            token = auth.create_session(conn, camera_ids=["CAM-A", "CAM-B"])
            conn.execute("UPDATE cameras SET enabled=0 WHERE camera_id='CAM-B'")
            self.assertEqual(auth.load_viewer(conn, token).camera_ids, frozenset({"CAM-A"}))

    def test_expired_and_unknown_sessions_are_anonymous(self):
        auth = sys.modules["auth"]
        with self.db() as conn:
            token = auth.create_session(conn, camera_ids=["CAM-A"])
            conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
            self.assertTrue(auth.load_viewer(conn, token).is_anonymous)
            self.assertTrue(auth.load_viewer(conn, "nope").is_anonymous)
            self.assertTrue(auth.load_viewer(conn, None).is_anonymous)

    def test_admin_session_sees_tests(self):
        auth = sys.modules["auth"]
        with self.db() as conn:
            viewer = auth.load_viewer(conn, auth.create_session(conn, is_admin=True))
        self.assertTrue(viewer.is_admin and viewer.sees_tests and viewer.can_see_camera("CAM-B"))


class LoginTests(ServerTestCase):
    def test_camera_login_sets_secure_cookie_and_logout(self):
        pw = self.set_login_password("CAM-A")
        r = self.login_camera(self.client, "CAM-A", pw)
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"].lower()
        for part in ("fw_session=", "httponly", "secure", "samesite=lax"):
            self.assertIn(part, cookie)
        self.assertEqual(self.client.post("/logout", follow_redirects=False).status_code, 303)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_login_page_renders(self):
        self.assertIn('name="camera_id"', self.client.get("/login").text)
        self.assertIn('name="password"', self.client.get("/admin/login").text)

    def test_wrong_id_and_wrong_password_look_identical(self):
        self.set_login_password("CAM-A")
        a = self.login_camera(self.new_client(), "CAM-A", "wrong")
        b = self.login_camera(self.new_client(), "CAM-ZZZ", "wrong")
        self.assertEqual((a.status_code, b.status_code), (401, 401))
        self.assertIn("Camera ID or password is wrong.", a.text)
        self.assertIn("Camera ID or password is wrong.", b.text)

    def test_no_password_or_disabled_camera_cannot_login(self):
        self.assertEqual(self.login_camera(self.client, "CAM-B", "").status_code, 401)  # never set
        pw = self.set_login_password("CAM-B")
        with self.db() as conn:
            conn.execute("UPDATE cameras SET enabled=0 WHERE camera_id='CAM-B'")
        self.assertEqual(self.login_camera(self.client, "CAM-B", pw).status_code, 401)

    def test_login_trims_camera_id(self):  # Review Focus 1
        pw = self.set_login_password("CAM-A")
        self.assertEqual(self.login_camera(self.client, "  CAM-A ", pw).status_code, 303)
        self.assertEqual(self.login_camera(self.new_client(), "cam-a", pw).status_code, 401)

    def test_two_cameras_in_one_session(self):
        pa, pb = self.set_login_password("CAM-A"), self.set_login_password("CAM-B")
        self.login_camera(self.client, "CAM-A", pa)
        self.login_camera(self.client, "CAM-B", pb)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cameras").fetchone()[0], 2)

    def test_admin_login(self):
        self.assertEqual(self.client.post("/admin/login", data={"password": "nope"}).status_code, 401)
        r = self.login_admin(self.client)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/admin"))

    def test_admin_login_disabled_without_token(self):  # Review Focus 3
        os.environ["FIREWATCH_ADMIN_TOKEN"] = ""
        client = TestClient(reload_app().app, base_url="https://testserver")
        r = client.post("/admin/login", data={"password": ""}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("set-cookie", r.headers)

    def test_login_rate_limited(self):
        codes = [self.login_camera(self.client, "CAM-A", "x").status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)


class TestEventIsolationTests(ServerTestCase):
    def test_real_upload_does_not_merge_into_open_test_event(self):
        test_row, _ = self.make_event(is_test=True)
        real = self.upload_event("CAM-A")
        self.assertNotIn("deduplicated", real)
        self.assertNotEqual(real["event_id"], test_row["event_id"])

    def test_test_event_does_not_merge_into_open_real_event(self):
        real = self.upload_event("CAM-A")
        test_row, _ = self.make_event(is_test=True)
        self.assertNotEqual(real["event_id"], test_row["event_id"])
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT is_test FROM device_events WHERE event_id=?",
                                          (real["event_id"],)).fetchone()[0], 0)

    def test_real_false_alarm_does_not_quiet_test_detections(self):
        api = sys.modules["device_api"]
        real, _ = self.make_event(status="false_alarm")
        with self.db() as conn:
            conn.execute("UPDATE device_events SET hotspot_x=10, hotspot_y=8, last_seen_at=? WHERE event_id=?",
                         (api.utc_now().isoformat(), real["event_id"]))
            self.assertIsNone(api._find_dismissed_match(conn, "CAM-A", api.utc_now(), 10, 8, 100, is_test=True))
            self.assertIsNotNone(api._find_dismissed_match(conn, "CAM-A", api.utc_now(), 10, 8, 100))

    def test_test_alert_is_prefixed(self):
        row, token = self.make_event(is_test=True)
        sys.modules["device_api"].notify_new_event(row, token)
        self.assertTrue(self.sent[-1]["body"].startswith("[TEST] "))

    def test_real_alert_is_not_prefixed(self):
        self.upload_event("CAM-A")
        self.assertFalse(self.sent[-1]["body"].startswith("[TEST]"))

    def test_confirmed_events_get_no_review_token(self):
        row, token = self.make_event(status="confirmed", location=(45.0, -70.0))
        self.assertIsNone(token)
        self.assertEqual((row["latitude"], row["longitude"]), (45.0, -70.0))

    def test_upload_still_works_through_create_event(self):
        first = self.upload_event("CAM-A")
        self.assertEqual(first["status"], "unconfirmed")
        self.assertTrue(self.upload_event("CAM-A")["deduplicated"])


class VisibilityApiTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.make_visibility_fixture()

    def ids(self, client, **params):
        return {e["event_id"] for e in client.get("/api/device-events", params=params).json()["events"]}

    def test_event_visibility_matrix(self):
        self.assertEqual(self.ids(self.anon), {self.b_confirmed})
        self.assertEqual(self.ids(self.cam_a), {self.b_confirmed, self.a_unconfirmed})
        self.assertEqual(self.ids(self.admin), {self.b_confirmed, self.a_unconfirmed, self.b_unconfirmed,
                                                self.test_confirmed, self.test_unconfirmed})

    def test_status_filter_cannot_widen_visibility(self):
        self.assertEqual(self.ids(self.anon, status="unconfirmed"), set())
        self.assertEqual(self.ids(self.cam_a, status="unconfirmed"), {self.a_unconfirmed})

    def test_pending_counts_are_scoped(self):
        count = lambda c: c.get("/api/device-events").json()["counts"]["pending_review"]
        self.assertEqual((count(self.anon), count(self.cam_a), count(self.admin)), (0, 1, 2))

    def test_camera_status_is_scoped(self):
        names = lambda c: [x["camera_id"] for x in c.get("/api/cameras/status").json()["cameras"]]
        self.assertEqual(names(self.anon), [])
        self.assertEqual(names(self.cam_a), ["CAM-A"])
        self.assertEqual(names(self.admin), ["CAM-A", "CAM-B"])

    def test_admin_sees_is_test_flag(self):
        flags = {e["event_id"]: e["is_test"] for e in self.admin.get("/api/device-events").json()["events"]}
        self.assertEqual(flags[self.test_unconfirmed], 1)
        self.assertEqual(flags[self.a_unconfirmed], 0)

    def test_bearer_admin_still_works(self):
        from tests.support import ADMIN
        self.assertEqual(len(self.anon.get("/api/device-events", headers=ADMIN).json()["events"]), 5)
