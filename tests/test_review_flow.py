import io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import device_api
import fire_data
import map as firewatch_map
import notifications
from database import get_connection, hash_camera_key

KEY = "test-camera-key-123456"


def jpeg(color):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(monkeypatch):
    sent = []
    monkeypatch.setattr(device_api, "send_fire_confirmation", lambda event: sent.append(event) or {})
    client = TestClient(firewatch_map.app)
    client.sent = sent
    return client


@pytest.fixture
def camera_id():
    camera_id = "CAM-" + uuid.uuid4().hex[:6]
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO cameras(camera_id,camera_key,latitude,longitude,name,created_at) VALUES (?,?,?,?,?,?)",
            (camera_id, hash_camera_key(KEY), 41.0, -73.0, "North Ridge", ""),
        )
    return camera_id


def post_event(client, camera_id, photo=True):
    files = {"image": ("heat.jpg", jpeg((255, 0, 0)), "image/jpeg")}
    if photo:
        files["photo"] = ("photo.jpg", jpeg((0, 0, 255)), "image/jpeg")
    response = client.post("/api/camera-event", data={
        "camera_id": camera_id, "camera_key": KEY, "latitude": 41.0, "longitude": -73.0,
        "fire_size_percent": 2.5, "max_temperature_c": 310.0, "duration_seconds": 5,
        "detection_method": "thermal",
    }, files=files)
    assert response.status_code == 200, response.text
    event_id = response.json()["event_id"]
    return event_id, client.sent[-1]["confirm_token"]


def status_of(event_id):
    with get_connection() as conn:
        return conn.execute("SELECT status FROM device_events WHERE event_id=?", (event_id,)).fetchone()["status"]


def public_event_ids(client):
    return {e["event_id"] for e in client.get("/api/device-events").json()["events"]}


def map_event_ids():
    data = fire_data.load_fires(use_cache=False)
    return set(data["event_id"].dropna()) if "event_id" in data.columns else set()


def test_photo_is_stored_alongside_heatmap(client, camera_id):
    event_id, _ = post_event(client, camera_id)
    with get_connection() as conn:
        row = conn.execute("SELECT image, photo FROM device_events WHERE event_id=?", (event_id,)).fetchone()
    assert row["image"] and row["photo"] and row["image"] != row["photo"]
    assert client.get("/" + row["photo"]).content == client.get("/" + row["photo"]).content


def test_unconfirmed_event_is_not_public(client, camera_id):
    event_id, _ = post_event(client, camera_id)
    assert status_of(event_id) == "unconfirmed"
    assert event_id not in public_event_ids(client)
    assert event_id not in map_event_ids()


def test_opening_review_link_changes_nothing(client, camera_id):
    event_id, token = post_event(client, camera_id)
    page = client.get(f"/review/{event_id}", params={"token": token})
    assert page.status_code == 200
    assert "North Ridge" in page.text
    assert "310.0" in page.text
    assert 'method="post"' in page.text
    assert page.text.count("<img") == 2  # heat-map + photo
    assert status_of(event_id) == "unconfirmed"


def test_review_page_without_photo_shows_heatmap_only(client, camera_id):
    event_id, token = post_event(client, camera_id, photo=False)
    page = client.get(f"/review/{event_id}", params={"token": token})
    assert page.text.count("<img") == 1


def test_bad_token_is_rejected(client, camera_id):
    event_id, _ = post_event(client, camera_id)
    assert client.get(f"/review/{event_id}", params={"token": "wrong"}).status_code == 403
    assert client.post(f"/review/{event_id}", data={"token": "wrong", "answer": "fire"}).status_code == 403
    assert status_of(event_id) == "unconfirmed"


def test_confirming_publishes_event(client, camera_id):
    event_id, token = post_event(client, camera_id)
    response = client.post(f"/review/{event_id}", data={"token": token, "answer": "fire"})
    assert response.status_code == 200
    assert status_of(event_id) == "confirmed"
    assert event_id in public_event_ids(client)
    assert event_id in map_event_ids()


def test_token_cannot_be_reused(client, camera_id):
    event_id, token = post_event(client, camera_id)
    client.post(f"/review/{event_id}", data={"token": token, "answer": "fire"})
    second = client.post(f"/review/{event_id}", data={"token": token, "answer": "not_fire"})
    assert second.status_code == 403
    assert status_of(event_id) == "confirmed"
    # Re-opening the link afterwards shows the outcome instead of the buttons.
    page = client.get(f"/review/{event_id}", params={"token": token})
    assert page.status_code == 200 and 'method="post"' not in page.text


def test_false_alarm_stays_private(client, camera_id):
    event_id, token = post_event(client, camera_id)
    client.post(f"/review/{event_id}", data={"token": token, "answer": "not_fire"})
    assert status_of(event_id) == "false_alarm"
    assert event_id not in public_event_ids(client)
    assert event_id not in map_event_ids()


def test_invalid_answer_is_rejected(client, camera_id):
    event_id, token = post_event(client, camera_id)
    assert client.post(f"/review/{event_id}", data={"token": token, "answer": "maybe"}).status_code == 400
    assert status_of(event_id) == "unconfirmed"


def test_legacy_confirm_link_only_redirects(client, camera_id):
    event_id, token = post_event(client, camera_id)
    response = client.get(f"/confirm/{event_id}/yes", params={"token": token}, follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert f"/review/{event_id}?token=" in response.headers["location"]
    assert status_of(event_id) == "unconfirmed"


def test_alert_text_links_to_review_page():
    body, review_url = notifications._confirmation_text({
        "event_id": "abc123", "confirm_token": "tok", "camera_id": "CAM-001", "place": "North Ridge",
        "fire_size_percent": 2.5, "max_temperature_c": 310.0, "severity_index": 12.0, "magnitude": "Low",
    })
    assert review_url.endswith("/review/abc123?token=tok")
    assert review_url in body
    assert "/confirm/" not in body
