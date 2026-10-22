"""Camera logins, per-viewer visibility and the admin control panel."""
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import ADMIN, ROOT, ServerTestCase, reload_app


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


class VisibilityMapTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.make_visibility_fixture()

    def traces(self, client):
        """{trace name: all hover text of that trace}"""
        data = client.get("/api/map-figure").json()["data"]
        return {t.get("name"): " | ".join(str(x) for x in (t.get("text") or [])) for t in data}

    def test_visitor_map_has_no_cameras_or_private_events(self):
        traces = self.traces(self.anon)
        everything = " ".join(traces.values())
        self.assertNotIn("Camera CAM-", everything)      # no camera markers
        self.assertNotIn("CAM-A", everything)            # A only has private/test events
        self.assertNotIn("Needs review", traces)
        self.assertNotIn("TEST fires", traces)
        self.assertIn("CAM-B", traces["Confirmed fires"])

    def test_camera_login_map_shows_only_own_camera(self):
        traces = self.traces(self.cam_a)
        everything = " ".join(traces.values())
        self.assertIn("Camera CAM-A", everything)
        self.assertNotIn("Camera CAM-B", everything)
        self.assertIn("CAM-A", traces["Needs review"])
        self.assertNotIn("CAM-B", traces["Needs review"])
        self.assertNotIn("TEST fires", traces)

    def test_admin_map_shows_test_fires(self):
        traces = self.traces(self.admin)
        self.assertIn("TEST fires", traces)
        self.assertIn("CAM-B", traces["Needs review"])

    def test_avoidance_includes_test_fires_only_for_admin(self):
        fd, auth = sys.modules["fire_data"], sys.modules["auth"]
        public = fd.get_avoid_geojson(auth.ANONYMOUS)
        camera = fd.get_avoid_geojson(auth.Viewer(camera_ids=frozenset({"CAM-A"})))
        admin = fd.get_avoid_geojson(auth.Viewer(is_admin=True))
        self.assertEqual(public, camera)
        self.assertNotEqual(public, admin)  # admin also avoids the confirmed test fire at (45, -70)
        self.assertIsNotNone(public)        # the real confirmed fire is avoided by everyone

    def test_dashboard_header_and_sections(self):
        anon = self.anon.get("/").text
        self.assertIn('href="/login"', anon)
        self.assertNotIn("Camera network", anon)
        self.assertNotIn("Awaiting owner review", anon)
        mine = self.cam_a.get("/").text
        self.assertIn("Sign out", mine)
        self.assertIn("Camera network", mine)
        self.assertIn("CAM-A", mine)
        self.assertNotIn('href="/admin"', mine)
        self.assertIn('href="/admin"', self.admin.get("/").text)


class SessionReviewTests(ServerTestCase):
    def signed_in(self, camera_id):
        client = self.new_client()
        self.login_camera(client, camera_id, self.set_login_password(camera_id))
        return client

    def status_of(self, event_id):
        with self.db() as conn:
            return tuple(conn.execute("SELECT status, reviewed_by FROM device_events WHERE event_id=?",
                                      (event_id,)).fetchone())

    def test_owner_can_review_without_token(self):
        event_id = self.upload_event("CAM-A")["event_id"]
        client = self.signed_in("CAM-A")
        self.assertIn('name="decision"', client.get(f"/review/{event_id}").text)
        r = client.post(f"/review/{event_id}", data={"decision": "fire"}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/review/{event_id}"))
        self.assertEqual(self.status_of(event_id), ("confirmed", "login:CAM-A"))
        self.assertNotIn('name="decision"', client.get(f"/review/{event_id}").text)  # shows the outcome now

    def test_admin_can_review_without_token(self):
        event_id = self.upload_event("CAM-B")["event_id"]
        admin = self.new_client()
        self.login_admin(admin)
        admin.post(f"/review/{event_id}", data={"decision": "not_fire", "label": "vehicle"})
        self.assertEqual(self.status_of(event_id), ("false_alarm", "admin"))

    def test_other_camera_cannot_review(self):
        event_id = self.upload_event("CAM-B")["event_id"]
        client = self.signed_in("CAM-A")
        self.assertEqual(client.get(f"/review/{event_id}").status_code, 403)
        self.assertEqual(client.post(f"/review/{event_id}", data={"decision": "fire"}).status_code, 403)
        self.assertEqual(self.status_of(event_id)[0], "unconfirmed")

    def test_camera_user_cannot_open_test_event(self):
        row, _ = self.make_event("CAM-A", is_test=True)
        client = self.signed_in("CAM-A")
        self.assertEqual(client.get(f"/review/{row['event_id']}").status_code, 403)

    def test_already_reviewed_event_cannot_be_changed_by_session(self):
        row, _ = self.make_event("CAM-A", status="confirmed")
        client = self.signed_in("CAM-A")
        client.post(f"/review/{row['event_id']}", data={"decision": "not_fire"})
        self.assertEqual(self.status_of(row["event_id"])[0], "confirmed")

    def test_token_link_still_works_anonymously(self):
        event_id = self.upload_event("CAM-A")["event_id"]
        token = self.token_from_last_sms()
        self.new_client().post(f"/review/{event_id}", data={"token": token, "decision": "not_fire", "label": "vehicle"})
        self.assertEqual(self.status_of(event_id), ("false_alarm", "review-link"))


class AdminCameraTests(ServerTestCase):
    def admin_client(self):
        client = self.new_client()
        self.login_admin(client)
        return client

    def test_admin_pages_require_admin(self):
        r = self.new_client().get("/admin", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/admin/login"))
        cam = self.new_client()
        self.login_camera(cam, "CAM-A", self.set_login_password("CAM-A"))
        self.assertEqual(cam.get("/admin", follow_redirects=False).status_code, 303)
        r = self.new_client().post("/admin/cameras/CAM-A/login-password", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.admin_client().get("/admin").status_code, 200)

    def test_camera_table_lists_cameras_and_login_state(self):
        self.set_login_password("CAM-A")
        page = self.admin_client().get("/admin").text
        self.assertIn("Alpha Ridge", page)
        self.assertIn("Bravo Hill", page)
        self.assertRegex(page, r'data-camera="CAM-A"[^>]*data-login="yes"')
        self.assertRegex(page, r'data-camera="CAM-B"[^>]*data-login="no"')

    def test_add_camera_shows_both_secrets_once(self):
        admin = self.admin_client()
        page = admin.post("/admin/cameras", data={"camera_id": "CAM-C", "name": "Cedar",
                                                  "latitude": "40", "longitude": "-75"}).text
        with self.db() as conn:
            row = conn.execute("SELECT camera_key, login_password_hash FROM cameras WHERE camera_id='CAM-C'").fetchone()
        pw = re.search(r'data-secret="login">([^<]+)<', page).group(1)
        key = re.search(r'data-secret="pi_key">([^<]+)<', page).group(1)
        self.assertTrue(sys.modules["auth"].verify_password(pw, row["login_password_hash"]))
        self.assertTrue(sys.modules["database"].camera_key_matches(row["camera_key"], key))
        self.assertNotIn(pw, admin.get("/admin").text)

    def test_add_camera_rejects_bad_input(self):
        admin = self.admin_client()
        for data in ({"camera_id": "CAM-A"}, {"latitude": "123"}, {"camera_id": "__test__"}, {"camera_id": " "}):
            form = {"camera_id": "CAM-D", "name": "D", "latitude": "40", "longitude": "-75", **data}
            self.assertEqual(admin.post("/admin/cameras", data=form).status_code, 400, data)

    def test_reset_password_signs_camera_out(self):
        old = self.set_login_password("CAM-A")
        user = self.new_client()
        self.login_camera(user, "CAM-A", old)
        page = self.admin_client().post("/admin/cameras/CAM-A/login-password").text
        new = re.search(r'data-secret="login">([^<]+)<', page).group(1)
        self.assertEqual(user.get("/api/cameras/status").json()["cameras"], [])
        self.assertEqual(self.login_camera(self.new_client(), "CAM-A", old).status_code, 401)
        self.assertEqual(self.login_camera(self.new_client(), "CAM-A", new).status_code, 303)

    def test_reset_pi_key_keeps_logins(self):
        pw = self.set_login_password("CAM-A")
        user = self.new_client()
        self.login_camera(user, "CAM-A", pw)
        page = self.admin_client().post("/admin/cameras/CAM-A/pi-key").text
        key = re.search(r'data-secret="pi_key">([^<]+)<', page).group(1)
        self.assertEqual(len(user.get("/api/cameras/status").json()["cameras"]), 1)
        r = self.client.post("/api/heartbeat", data={"camera_id": "CAM-A", "camera_key": key})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/heartbeat", data={"camera_id": "CAM-A", "camera_key": self.keys["CAM-A"]})
        self.assertEqual(r.status_code, 401)

    def test_disable_and_enable_camera(self):
        user = self.new_client()
        self.login_camera(user, "CAM-A", self.set_login_password("CAM-A"))
        admin = self.admin_client()
        admin.post("/admin/cameras/CAM-A/enabled", data={"enabled": "0"})
        self.assertEqual(user.get("/api/cameras/status").json()["cameras"], [])
        admin.post("/admin/cameras/CAM-A/enabled", data={"enabled": "1"})
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM cameras WHERE camera_id='CAM-A'").fetchone()[0], 1)

    def test_api_login_password_endpoint_and_disable_revokes(self):
        r = self.client.post("/api/admin/cameras/CAM-B/login-password", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        user = self.new_client()
        self.assertEqual(self.login_camera(user, "CAM-B", r.json()["login_password"]).status_code, 303)
        self.client.delete("/api/admin/cameras/CAM-B", headers=ADMIN)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cameras").fetchone()[0], 0)
        self.assertEqual(self.client.post("/api/admin/cameras/__test__/login-password", headers=ADMIN).status_code, 404)

    def test_api_create_returns_login_password(self):
        r = self.client.post("/api/admin/cameras", headers=ADMIN, json={"camera_id": "CAM-E", "latitude": 1, "longitude": 2})
        self.assertEqual(self.login_camera(self.new_client(), "CAM-E", r.json()["login_password"]).status_code, 303)

    def test_manage_set_camera_password(self):
        out = subprocess.run([sys.executable, "manage.py", "set-camera-password", "CAM-A"], cwd=ROOT,
                             capture_output=True, text=True, env=dict(os.environ), check=True).stdout
        pw = re.search(r"Login password for CAM-A: (\S+)", out).group(1)
        self.assertEqual(self.login_camera(self.new_client(), "CAM-A", pw).status_code, 303)


class TestFireTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.new_client()
        self.login_admin(self.admin)
        self.images = Path(os.environ["FIREWATCH_IMAGE_DIR"])

    def simulate(self, **overrides):
        form = {"camera_id": "CAM-A", "max_temperature_c": "350", "hot_pixels": "12", "send_alerts": "1", **overrides}
        return self.admin.post("/admin/test-fires/simulate", data={k: v for k, v in form.items() if v is not None})

    def test_synthetic_frame_matches_requested_size(self):
        tf, thermal = sys.modules["test_fires"], importlib.import_module("thermal")
        for hot_pixels in (6, 12, 40):
            stats = thermal.analyze(tf.synthetic_frame(350.0, hot_pixels), 80.0, 25.0, 6)
            self.assertEqual(stats.cluster, hot_pixels)
            self.assertAlmostEqual(stats.max_temp, 350.0, delta=0.01)

    def test_simulate_runs_full_pipeline_privately(self):
        page = self.simulate()
        self.assertEqual(page.status_code, 200)
        self.assertIn("Open review page", page.text)
        self.assertTrue(self.sent[-1]["body"].startswith("[TEST] "))
        event = self.admin.get("/api/device-events").json()["events"][0]
        self.assertEqual((event["is_test"], event["status"], event["camera_id"]), (1, "unconfirmed", "CAM-A"))
        self.assertTrue((self.images / f"{event['event_id']}.jpg").is_file())
        self.assertEqual(self.new_client().get("/api/device-events").json()["events"], [])
        cam = self.new_client()
        self.login_camera(cam, "CAM-A", self.set_login_password("CAM-A"))
        self.assertEqual(cam.get("/api/device-events").json()["events"], [])

    def test_simulated_review_link_works(self):
        self.simulate()
        link = re.search(r'href="(/review/[^"]+\?token=[^"]+)"', self.simulate(camera_id="CAM-B").text).group(1)
        event_id = link.split("/")[2].split("?")[0]
        token = link.split("token=")[1]
        self.new_client().post(f"/review/{event_id}", data={"token": token, "decision": "fire"})
        statuses = {e["event_id"]: e["status"] for e in self.admin.get("/api/device-events").json()["events"]}
        self.assertEqual(statuses[event_id], "confirmed")
        self.assertEqual(self.new_client().get("/api/device-events").json()["events"], [])  # still admin-only

    def test_simulate_without_alerts_sends_nothing(self):
        before = len(self.sent)
        self.simulate(send_alerts=None)
        self.assertEqual(len(self.sent), before)
        self.assertEqual(len(sys.modules["test_fires"].list_test_fires()), 1)

    def test_simulate_rejects_bad_input(self):  # Review Focus 4
        for bad in ({"hot_pixels": "0"}, {"hot_pixels": "999"}, {"max_temperature_c": "20"},
                    {"max_temperature_c": "abc"}, {"camera_id": "__test__"}, {"camera_id": "NOPE"}):
            self.assertEqual(self.simulate(**bad).status_code, 400, bad)
        with self.db() as conn:
            conn.execute("UPDATE cameras SET enabled=0 WHERE camera_id='CAM-B'")
        self.assertEqual(self.simulate(camera_id="CAM-B").status_code, 400)
        self.assertEqual(sys.modules["test_fires"].list_test_fires(), [])

    def test_drop_fire_is_confirmed_admin_only(self):
        r = self.admin.post("/admin/test-fires/drop", data={"latitude": "45.0", "longitude": "-70.0", "severity": "High"})
        self.assertEqual(r.status_code, 200)
        [fire] = sys.modules["test_fires"].list_test_fires()
        self.assertEqual((fire["status"], fire["camera_id"], fire["magnitude"], fire["severity_index"]),
                         ("confirmed", "__test__", "High", 55))
        self.assertEqual((fire["latitude"], fire["longitude"]), (45.0, -70.0))
        self.assertNotIn("TEST fires", self.new_client().get("/api/map-figure").text)
        self.assertIn("TEST fires", self.admin.get("/api/map-figure").text)
        self.assertNotIn('data-camera="__test__"', self.admin.get("/admin").text)
        self.assertEqual([c["camera_id"] for c in self.admin.get("/api/cameras/status").json()["cameras"]],
                         ["CAM-A", "CAM-B"])

    def test_drop_fire_rejects_bad_input(self):
        for bad in ({"latitude": "95"}, {"longitude": "x"}, {"severity": "Huge"}):
            form = {"latitude": "45", "longitude": "-70", "severity": "Low", **bad}
            self.assertEqual(self.admin.post("/admin/test-fires/drop", data=form).status_code, 400, bad)

    def test_test_camera_cannot_log_in(self):
        tf = sys.modules["test_fires"]
        tf.drop_test_fire(45.0, -70.0, "Low")
        self.assertEqual(self.login_camera(self.new_client(), "__test__", "").status_code, 401)

    def test_panel_lists_test_fires(self):
        tf = sys.modules["test_fires"]
        event_id = tf.drop_test_fire(45.0, -70.0, "Low")
        self.assertIn(event_id, self.admin.get("/admin").text)

    def test_delete_all_tolerates_missing_files(self):  # Review Focus 5
        tf = sys.modules["test_fires"]
        event_id = tf.simulate_detection("CAM-A", send_alerts=False)["event_id"]
        tf.drop_test_fire(45.0, -70.0, "Low")
        (self.images / f"{event_id}.jpg").unlink()
        real = self.upload_event("CAM-B")["event_id"]
        self.admin.post("/admin/test-fires/delete-all")
        self.assertEqual(tf.list_test_fires(), [])
        self.assertIn(real, {e["event_id"] for e in self.admin.get("/api/device-events").json()["events"]})

    def test_delete_one_removes_its_image(self):
        tf = sys.modules["test_fires"]
        event_id = tf.simulate_detection("CAM-A", send_alerts=False)["event_id"]
        self.admin.post(f"/admin/test-fires/{event_id}/delete")
        self.assertFalse((self.images / f"{event_id}.jpg").exists())
        self.assertEqual(tf.list_test_fires(), [])

    def test_delete_refuses_real_events(self):
        real = self.upload_event("CAM-A")["event_id"]
        self.assertEqual(self.admin.post(f"/admin/test-fires/{real}/delete").status_code, 404)
        self.assertIn(real, {e["event_id"] for e in self.admin.get("/api/device-events").json()["events"]})

    def test_test_fire_actions_require_admin(self):
        for path in ("/admin/test-fires/simulate", "/admin/test-fires/drop", "/admin/test-fires/delete-all"):
            self.assertEqual(self.new_client().post(path, follow_redirects=False).status_code, 303)
