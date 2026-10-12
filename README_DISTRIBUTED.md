# Firewatch Distributed Camera System

Firewatch is a prototype distributed wildfire-monitoring and route-avoidance system. Each Raspberry Pi uses an MLX90640 thermal sensor plus a Pi Camera video stream. Persistent thermal or warm/flickering video clues create an unconfirmed event; the Pi uploads a snapshot and a short MP4 clip when recording succeeds. The video rule is a lightweight heuristic, not a trained fire-classification model, so human confirmation remains important.

## Architecture

```text
Pi #1 / Pi #2 / Pi #N
 MLX90640 + Pi Camera video
        | snapshot + MP4 over HTTPS
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

The dashboard is styled as a clean enterprise monitoring console with a Carto light basemap, header search, health badge, floating camera/fire metrics, and a route-planning panel for driving, biking, and walking. The route panel uses the existing `/routes` and `/autocomplete` endpoints; the satellite toggle controls map-layer visibility and does not disable route hazard avoidance.


- SQLite replaces live CSV storage.
- Camera keys are stored hashed for new cameras; legacy plaintext keys remain readable for migration.
- Fixed server-side latitude/longitude is authoritative; no GPS is required on the Pi.
- Camera heartbeat and online/stale/offline status.
- Thermal detection uses an adaptive ambient threshold plus connected-component filtering, persistence, and configurable thresholds.
- A video-frame heuristic checks for persistent bright warm-colored regions and a short video clip is attached to an event when recording is available. It can have false positives and is not a certified classifier.
- The browser map initializes correctly even with zero events, shows registered fixed-location cameras, and refreshes event/map data automatically every 30 seconds.
- Event snapshots and MP4 clips are linked in the recent-events panel.
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

On Raspberry Pi OS, install system camera/video packages first so Picamera2 uses the OS-provided libcamera stack:

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-numpy python3-pil python3-venv ffmpeg
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements-pi.txt
cp camera_config.json.example camera_config.json
```

Edit `camera_config.json` with the generated `camera_key` and the camera's fixed registered coordinates. The server's registered coordinates are authoritative; the Pi does not need a GPS receiver.

Then run (replace the address with your deployed Render URL):

```bash
export FIREWATCH_SERVER_URL="https://your-service.onrender.com"
python sensor_monitor.py
```

The Pi samples video frames and the thermal sensor. On a persistent candidate, it records an approximately 6-second MP4 clip and snapshot, then uploads both to `/api/camera-event`. Tune `VISUAL_FIRE_THRESHOLD`, `VISUAL_REQUIRED_FRAMES`, and `FIREWATCH_CLIP_SECONDS` for the installation. Install/configure a real trained fire-detection model and validate it on representative data before relying on video-only decisions in the field.

The Pi sends a heartbeat about every 30 seconds. The thermal detector uses:

- absolute temperature threshold (`HOT_TEMP_C`)
- temperature above local background (`AMBIENT_DELTA_C`)
- minimum hot area (`MIN_HOT_PERCENT`)
- minimum connected hot cluster (`MIN_CLUSTER_PIXELS`)
- persistent detections (`REQUIRED_FRAMES`)

These values are intentionally configurable because installation, weather, distance, optics, and sensor calibration affect the readings.

## Confirmation flow

1. Pi detects persistent thermal heat or a persistent video-frame heuristic signal.
2. Pi captures a snapshot and attempts a short MP4 clip.
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
- `GET /api/admin/subscribers`
- `POST /api/admin/subscribers`
- `GET /api/cameras/status`
- `GET /api/device-events`

## Important production notes

Use HTTPS, firewall the server, keep secrets out of Git, back up SQLite and uploaded clips, and consider a real background queue for notifications if this becomes a larger deployment. Render stores these under the persistent disk mount configured in `render.yaml`. The route avoid-zone is a prototype buffer around reported/confirmed points, not a surveyed evacuation boundary. For stronger device authentication, a future version can move from shared camera keys to signed HMAC request bodies with timestamp/replay protection.

This is a prototype detection/decision-support system, **not a certified life-safety wildfire warning system**. MLX90640 readings can be affected by distance, emissivity, sunlight, reflections, hot vehicles/equipment, and other heat sources. Confirmation and independent verification should be used before treating an event as a real wildfire.

The severity index is a relative 0–100 indicator based on hot area, persistence, and maximum temperature. It is not a scientific measurement of physical fire energy or true burned area.

## Render deployment

See `README_RENDER.md` for the Render setup. The important distinction is that Render uses `requirements.txt` while Raspberry Pi nodes use `requirements-pi.txt`.


## Dashboard navigation and satellite monitoring

The top-right panel supports driving, biking, and walking routes with turn-by-turn instructions. Route avoidance uses available reported/satellite detections and confirmed camera fires; unconfirmed camera events stay visible for review and are not automatically treated as confirmed device hazards. The panel also exposes the NASA FIRMS active-fire layer. Set `FIRMS_API_KEY` to enable active-fire satellite detections, and optionally `FIRMS_BBOX=west,south,east,north` to monitor a fixed bounding box instead of the camera footprint.
