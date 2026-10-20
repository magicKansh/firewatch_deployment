import json

import numpy as np
import pytest
from PIL import Image

import sensor_monitor as sm

CFG = {"camera_id": "CAM-001", "camera_key": "k" * 20, "latitude": 41.0, "longitude": -73.0}


def cool():
    return np.full((24, 32), 22.0, dtype=np.float32)


def hot():
    temps = cool()
    temps[10:13, 20:23] = 300.0
    return temps


class FakeSensor:
    def __init__(self, frames):
        self.frames = list(frames)

    def __call__(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class FakeUploader:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, meta, heatmap_path, photo_path):
        if self.fail:
            raise ConnectionError("server down")
        self.calls.append((meta, heatmap_path.read_bytes(), photo_path.read_bytes() if photo_path else None))


def make_node(tmp_path, frames, uploader, photo=False):
    def capture_photo(path):
        Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="JPEG")

    return sm.Node(
        CFG, FakeSensor(frames), uploader,
        capture_photo=capture_photo if photo else None,
        temp_dir=tmp_path / "temp", pending_dir=tmp_path / "pending",
        sleep=lambda seconds: None,
    )


def test_cool_sample_leaves_no_files(tmp_path):
    uploader = FakeUploader()
    node = make_node(tmp_path, [cool()], uploader)
    node.sample()
    assert list((tmp_path / "temp").iterdir()) == []
    assert list((tmp_path / "pending").iterdir()) == []
    assert uploader.calls == []


def test_hot_sample_confirmed_by_burst_uploads_event(tmp_path):
    uploader = FakeUploader()
    node = make_node(tmp_path, [hot()] * 10, uploader)
    node.sample()
    assert len(uploader.calls) == 1
    meta, heatmap, photo = uploader.calls[0]
    assert meta["detection_method"] == "thermal"
    assert meta["max_temperature_c"] == 300.0
    assert meta["fire_size_percent"] > 0
    assert heatmap[:2] == b"\xff\xd8"  # JPEG
    assert photo is None
    assert list((tmp_path / "pending").iterdir()) == []
    assert list((tmp_path / "temp").iterdir()) == []


def test_one_frame_glitch_is_not_an_event(tmp_path):
    uploader = FakeUploader()
    node = make_node(tmp_path, [hot()] + [cool()] * 10, uploader)
    node.sample()
    assert uploader.calls == []
    assert list((tmp_path / "pending").iterdir()) == []


def test_photo_is_attached_when_camera_present(tmp_path):
    uploader = FakeUploader()
    node = make_node(tmp_path, [hot()] * 10, uploader, photo=True)
    node.sample()
    _meta, _heatmap, photo = uploader.calls[0]
    assert photo is not None and photo[:2] == b"\xff\xd8"


def test_failed_photo_still_sends_heatmap(tmp_path):
    uploader = FakeUploader()
    node = make_node(tmp_path, [hot()] * 10, uploader)

    def broken(path):
        raise RuntimeError("camera unplugged")

    node.capture_photo = broken
    node.sample()
    assert len(uploader.calls) == 1
    assert uploader.calls[0][2] is None


def test_failed_upload_keeps_files_and_retries(tmp_path):
    uploader = FakeUploader(fail=True)
    node = make_node(tmp_path, [hot()] * 10, uploader)
    node.sample()
    pending = sorted(p.suffix for p in (tmp_path / "pending").iterdir())
    assert pending == [".jpg", ".json"]
    meta = json.loads(next((tmp_path / "pending").glob("*.json")).read_text())
    assert "camera_key" not in meta  # the secret is added only at upload time

    uploader.fail = False
    assert node.flush() == 1
    assert len(uploader.calls) == 1
    assert list((tmp_path / "pending").iterdir()) == []


def test_ongoing_fire_alerts_once_until_scene_cools(tmp_path):
    uploader = FakeUploader()
    node = make_node(tmp_path, [hot()] * 50, uploader)
    node.sample()
    node.sample()
    node.sample()
    assert len(uploader.calls) == 1

    node.read_frame = FakeSensor([cool()])
    for _ in range(sm.EVENT_RESET_SAMPLES):
        node.sample()
    node.read_frame = FakeSensor([hot()])
    node.sample()
    assert len(uploader.calls) == 2


def test_pending_queue_is_capped(tmp_path):
    uploader = FakeUploader(fail=True)
    node = make_node(tmp_path, [hot()], uploader)
    for _ in range(sm.MAX_PENDING_EVENTS + 5):
        node.event_active = False
        node.sample()
    assert len(list((tmp_path / "pending").glob("*.json"))) == sm.MAX_PENDING_EVENTS


def test_sensor_errors_are_retried(tmp_path):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("MLX90640 frame read failed")
        return cool()

    node = make_node(tmp_path, [cool()], FakeUploader())
    node.read_frame = flaky
    node.sample()
    assert calls["n"] == 3


def test_missing_config_key_is_reported(tmp_path):
    path = tmp_path / "camera_config.json"
    path.write_text(json.dumps({"camera_id": "CAM-001"}))
    with pytest.raises(RuntimeError, match="camera_key"):
        sm.load_config(path)
