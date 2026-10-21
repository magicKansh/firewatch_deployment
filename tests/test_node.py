"""Raspberry Pi node tests. No sensor or camera needed: hardware modules are faked.

Run with:  python -m unittest discover -s tests   (or: pytest)
"""
import email
import importlib
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import thermal  # noqa: E402
from node_storage import Outbox, PermanentUploadError, SnapshotRing  # noqa: E402

REAL_SLEEP = time.sleep


def scene(hot=False, seed=0):
    rng = np.random.default_rng(seed)
    temps = 18 + rng.normal(0, 0.5, (24, 32))
    if hot:
        temps[8:12, 5:9] = 160      # 16-pixel hotspot centred at x=6.5, y=9.5
    return temps.astype(np.float32)


class ThermalTests(unittest.TestCase):
    def test_hotspot_found(self):
        stats = thermal.analyze(scene(hot=True), 80, 25, 6)
        self.assertEqual(stats.cluster, 16)
        self.assertAlmostEqual(stats.hotspot_x, 6.5)
        self.assertAlmostEqual(stats.hotspot_y, 9.5)
        self.assertEqual(stats.bbox, (5, 8, 8, 11))
        self.assertGreater(stats.hot_percent, 0)

    def test_cold_scene_has_no_hotspot(self):
        stats = thermal.analyze(scene(), 80, 25, 6)
        self.assertEqual(stats.cluster, 0)
        self.assertEqual(stats.hot_percent, 0)
        self.assertIsNone(stats.bbox)

    def test_small_cluster_does_not_count(self):
        temps = scene()
        temps[0, 0] = temps[0, 1] = 200
        self.assertEqual(thermal.analyze(temps, 80, 25, 6).hot_percent, 0)

    def test_largest_cluster_wins(self):
        temps = scene()
        temps[0:2, 0:2] = 150        # 4 px
        temps[20:23, 25:29] = 150    # 12 px
        stats = thermal.analyze(temps, 80, 25, 6)
        self.assertEqual(stats.cluster, 12)
        self.assertGreater(stats.hotspot_x, 24)

    def test_render_size_and_flip(self):
        img = thermal.render(scene(hot=True), thermal.analyze(scene(hot=True), 80, 25, 6), title="T")
        self.assertEqual(img.size, (640, 480 + 54))
        flipped = thermal.to_grid(scene(hot=True).ravel(), flip_h=True)
        self.assertEqual(thermal.analyze(flipped, 80, 25, 6).bbox, (23, 8, 26, 11))

    def test_render_handles_nan(self):
        temps = scene()
        temps[3, 3] = np.nan
        thermal.render(temps)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ring_keeps_newest_and_clears_stale(self):
        (self.dir / "snap").mkdir()
        (self.dir / "snap" / "snap_19990101T000000Z.jpg").write_bytes(b"old")
        ring = SnapshotRing(self.dir / "snap", keep=3)
        self.assertEqual(ring.recent(5), [])          # stale file from a previous run removed
        img = Image.new("RGB", (4, 4))
        for i in range(6):
            ring.add(img, stamp=f"20260101T00000{i}Z")
        names = [p.name for p in ring.recent(10)]
        self.assertEqual(names, [f"snap_20260101T00000{i}Z_{i + 1:06d}.jpg" for i in (3, 4, 5)])
        self.assertEqual([p.name for p in ring.recent(2)], names[1:])

    def test_outbox_retry_and_drop(self):
        box = Outbox(self.dir / "out", max_items=2)
        for i in range(3):
            f = self.dir / f"img{i}.jpg"
            f.write_bytes(b"x")
            box.put({"n": i}, [("image", f, "image/jpeg")])
            self.assertFalse(f.exists())              # moved into the outbox
        self.assertEqual(len(box.items()), 2)         # oldest dropped at the cap

        def offline(form, files):
            raise ConnectionError("down")
        self.assertEqual(box.flush(offline), (0, 0))
        self.assertEqual(len(box.items()), 2)

        seen = []

        def online(form, files):
            if form["n"] == 1:
                raise PermanentUploadError("401")
            seen.append((form["n"], [p.read_bytes() for _, p, _ in files]))
        self.assertEqual(box.flush(online), (1, 1))
        self.assertEqual(seen, [(2, [b"x"])])
        self.assertEqual(box.items(), [])


# --- End-to-end: fake MLX90640 + fake Firewatch server --------------------------------------

class FakeServer:
    def __init__(self):
        self.status = 200
        self.events = []
        self.heartbeats = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/heartbeat":
                    outer.heartbeats += 1
                    code, payload = 200, {"ok": True}
                else:
                    code = outer.status
                    if code == 200:
                        outer.events.append(outer.parse(self.headers["Content-Type"], body))
                    payload = {"ok": code == 200, "event_id": "abc"}
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def parse(content_type, body):
        msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
        form, files = {}, []
        for part in msg.get_payload():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                files.append((name, part.get_filename(), part.get_payload(decode=True)))
            else:
                form[name] = part.get_payload(decode=True).decode()
        return {"form": form, "files": files}

    def close(self):
        self.httpd.shutdown()


class FakeMLX:
    """Feeds a scripted list of scenes, then stops the node with KeyboardInterrupt."""

    def __init__(self, scenes, between=None):
        self.scenes = list(scenes)
        self.between = between or {}
        self.count = 0

    def getFrame(self, buffer):
        if self.count in self.between:
            self.between[self.count]()
        if not self.scenes:
            raise KeyboardInterrupt
        buffer[:] = self.scenes.pop(0).ravel().tolist()
        self.count += 1
        REAL_SLEEP(0.004)


class NodeEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.server = FakeServer()
        config = self.dir / "camera_config.json"
        config.write_text(json.dumps({"camera_id": "CAM-T", "camera_key": "k" * 20,
                                      "latitude": 41.0, "longitude": -73.0}))
        env = {"FIREWATCH_CAMERA_CONFIG": str(config), "FIREWATCH_SERVER_URL": self.server.url,
               "FIREWATCH_SNAPSHOT_DIR": str(self.dir / "snaps"),
               "FIREWATCH_OUTBOX_DIR": str(self.dir / "outbox"), "FIREWATCH_PICAMERA": "off"}
        with mock.patch.dict("os.environ", env):
            sys.modules.pop("sensor_monitor", None)
            self.node = importlib.import_module("sensor_monitor")
        self.node.SNAPSHOT_SECONDS = 0.02
        self.node.SNAPSHOT_KEEP = 3
        self.node.OUTBOX_RETRY_SECONDS = 0
        self.node.QUIET_FRAMES_TO_RESET = 5

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def run_node(self, mlx):
        with mock.patch.object(self.node, "create_thermal", return_value=mlx), \
             mock.patch.object(self.node.time, "sleep", lambda s: None):
            self.node.main()

    def test_detection_uploads_heat_image_with_context(self):
        scenes = [scene()] * 15 + [scene(hot=True)] * 8 + [scene()] * 10
        self.run_node(FakeMLX(scenes))
        self.assertGreaterEqual(self.server.heartbeats, 1)
        self.assertEqual(len(self.server.events), 1)
        event = self.server.events[0]
        form = event["form"]
        self.assertEqual(form["camera_id"], "CAM-T")
        self.assertEqual(form["detection_method"], "thermal")
        self.assertAlmostEqual(float(form["hotspot_x"]), 6.5)
        self.assertGreater(float(form["max_temperature_c"]), 150)
        fields = [name for name, _, _ in event["files"]]
        self.assertEqual(fields.count("image"), 1)
        self.assertEqual(fields.count("context_images"), 2)
        self.assertTrue(event["files"][0][2].startswith(b"\xff\xd8"))   # JPEG
        # Temp files cleaned up: only the rolling snapshots remain.
        self.assertEqual(list((self.dir / "snaps" / "events").iterdir()), [])
        self.assertLessEqual(len(list((self.dir / "snaps").glob("snap_*.jpg"))), 3)

    def test_offline_event_is_queued_then_delivered(self):
        def server_back():
            self.server.status = 200
        self.server.status = 503
        scenes = [scene()] * 5 + [scene(hot=True)] * 8 + [scene()] * 30
        self.run_node(FakeMLX(scenes, between={20: server_back}))
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(self.server.events[0]["form"]["camera_id"], "CAM-T")
        self.assertEqual(list((self.dir / "outbox").iterdir()), [])

    def test_rejected_event_is_not_queued(self):
        self.server.status = 401
        self.run_node(FakeMLX([scene(hot=True)] * 8))
        self.assertEqual(self.server.events, [])
        self.assertEqual(list((self.dir / "outbox").iterdir()), [])

    def test_dead_sensor_exits_for_restart(self):
        class DeadMLX:
            def getFrame(self, buffer):
                raise ValueError("Too many retries")
        self.node.MAX_READ_ERRORS = 3
        with self.assertRaises(SystemExit):
            self.run_node(DeadMLX())

    def test_one_event_per_heat_episode(self):
        scenes = ([scene(hot=True)] * 10 + [scene()] * 8) * 2
        self.run_node(FakeMLX(scenes))
        self.assertEqual(len(self.server.events), 2)


if __name__ == "__main__":
    unittest.main()
