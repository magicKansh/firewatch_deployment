"""Camera logins, per-viewer visibility and the admin control panel."""
import sys

from tests.support import ServerTestCase


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
