# Firewatch Distributed Camera System

Firewatch is a prototype distributed wildfire-detection and route-avoidance system. Each Raspberry Pi uses an MLX90640 thermal sensor to look for persistent hot regions, captures a normal camera image, and sends an event to a central FastAPI server.

## Architecture

```text
Pi #1 / Pi #2 / Pi #N
 MLX90640 + Pi Camera
        | HTTPS
        v
 Firewatch Server
  SQLite + APIs
   |     |     |
   |     |     +--> Map / route avoidance
   |     +--------> Twilio SMS / email
   +--------------> Event history / camera health
                    |
                    v
               YES / NO confirmation
```

## What is implemented

- SQLite replaces live CSV storage.
- Camera keys are stored hashed for new cameras; legacy plaintext keys remain readable for migration.
- Fixed server-side latitude/longitude is authoritative; no GPS is required on the Pi.
- Camera heartbeat and online/stale/offline status.
- Thermal detection uses an adaptive ambient threshold plus connected-component filtering, persistence, and configurable thresholds.
- Event deduplication prevents repeated alerts from becoming dozens of separate events.
- Event observations preserve repeated measurements.
- Confirmation tokens are hashed, expire, and are single-use.
- SMS through Twilio and optional email notification.
- Notification attempts are recorded in SQLite.
- Admin camera API protected by `FIREWATCH_ADMIN_TOKEN`.
- Camera status and recent-event panel on the map.
- Only **confirmed** device detections are automatically added to route-avoidance geometry.
- Existing reported/satellite fire layers remain available.

## Server setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set the variables in `.env.example` in your deployment environment. At minimum, for the complete system you will need `ORS_API_KEY`, `PUBLIC_BASE_URL`, `FIREWATCH_ADMIN_TOKEN`, and Twilio variables for SMS.

Run:

```bash
uvicorn map:app --host 0.0.0.0 --port 8000
```

The old `uvicorn map:app` entry point is intentionally retained for compatibility.

## Add cameras

```bash
python manage.py add-camera CAM-001 41.123456 -73.123456 --name "North Ridge"
```

The command prints a random camera key. Put it in that Pi's `camera_config.json`:

```json
{
  "camera_id": "CAM-001",
  "camera_key": "THE_GENERATED_KEY",
  "latitude": 41.123456,
  "longitude": -73.123456
}
```

Add an SMS recipient:

```bash
python manage.py add-subscriber CAM-001 --phone +15555550123
```

All cameras:

```bash
python manage.py add-subscriber '*' --phone +15555550123
```

Email can be added with `--email`.

## Raspberry Pi setup

On each Pi install the Raspberry Pi dependencies and copy `camera_config.json.example` to `camera_config.json`.

Then run:

```bash
python3 sensor_monitor.py
```

The Pi sends a heartbeat about every 30 seconds. The thermal detector uses:

- absolute temperature threshold (`HOT_TEMP_C`)
- temperature above local background (`AMBIENT_DELTA_C`)
- minimum hot area (`MIN_HOT_PERCENT`)
- minimum connected hot cluster (`MIN_CLUSTER_PIXELS`)
- persistent detections (`REQUIRED_FRAMES`)

These values are intentionally configurable because installation, weather, distance, optics, and sensor calibration affect the readings.

## Confirmation flow

1. Pi detects a persistent hot region.
2. Pi captures a normal image.
3. Server authenticates the camera and uses its registered coordinates.
4. Server creates an `unconfirmed` event.
5. SMS/email is sent with confirmation links.
6. Recipient selects **CONFIRM** or **FALSE ALARM**.
7. The token is consumed and the event becomes `confirmed` or `false_alarm`.
8. Confirmed device events are used for route avoidance.

Repeated reports from the same camera within `EVENT_DEDUPE_SECONDS` update the existing event and do not send another initial alert.

## Admin API

Set `FIREWATCH_ADMIN_TOKEN` and send:

```text
Authorization: Bearer YOUR_TOKEN
```

Endpoints:

- `GET /api/admin/cameras`
- `POST /api/admin/cameras`
- `PUT /api/admin/cameras/{camera_id}`
- `DELETE /api/admin/cameras/{camera_id}` (disables it)
- `GET /api/cameras/status`
- `GET /api/device-events`

## Important production notes

Use HTTPS, firewall the server, keep secrets out of Git, back up SQLite, and consider a real background queue for notifications if this becomes a larger deployment. For stronger device authentication, a future version can move from shared camera keys to signed HMAC request bodies with timestamp/replay protection.

This is a prototype detection/decision-support system, **not a certified life-safety wildfire warning system**. MLX90640 readings can be affected by distance, emissivity, sunlight, reflections, hot vehicles/equipment, and other heat sources. Confirmation and independent verification should be used before treating an event as a real wildfire.

The severity index is a relative 0–100 indicator based on hot area, persistence, and maximum temperature. It is not a scientific measurement of physical fire energy or true burned area.
