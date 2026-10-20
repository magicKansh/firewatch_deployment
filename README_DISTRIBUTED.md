# Firewatch Distributed Camera System

Firewatch is a prototype distributed wildfire-monitoring and route-avoidance system. Each Raspberry Pi uses an MLX90640 thermal sensor, plus an optional Pi Camera. About once a minute the Pi takes a thermal snapshot; when a hot spot is confirmed by a short burst of extra frames, it uploads a heat-map image (and a camera photo, if a camera is attached) as an unconfirmed event. A person reviews the image, and only confirmed fires reach the public map.

## Architecture

```text
Pi #1 / Pi #2 / Pi #N
 MLX90640 (+ optional Pi Camera)
        | heat-map + photo over HTTPS
        v
 Firewatch Server
  SQLite + APIs
   |     |     |
   |     |     +--> Map / route avoidance
   |     +--------> Twilio SMS / email
   +--------------> Event history / camera health
                    |
                    v
               Review page: Fire / Not a fire
```

## What is implemented

The dashboard is styled as a clean enterprise monitoring console with a Carto light basemap, header search, health badge, floating camera/fire metrics, and a route-planning panel for driving, biking, and walking. The route panel uses the existing `/routes` and `/autocomplete` endpoints; the satellite toggle controls map-layer visibility and does not disable route hazard avoidance.


- SQLite replaces live CSV storage.
- Camera keys are stored hashed for new cameras; legacy plaintext keys remain readable for migration.
- Fixed server-side latitude/longitude is authoritative; no GPS is required on the Pi.
- Camera heartbeat and online/stale/offline status.
- Thermal detection uses an adaptive ambient threshold plus connected-component filtering, persistence, and configurable thresholds.
- Each event carries a 640x480 heat-map with the hot cluster boxed, plus a Pi Camera photo when one is attached.
- The Pi keeps events in a local `pending/` queue until the server accepts them, so outages and reboots do not lose detections.
- The browser map initializes correctly even with zero events, shows registered fixed-location cameras, and refreshes event/map data automatically every 30 seconds.
- Heat-maps and photos of confirmed events are linked in the recent-events panel.
- Event deduplication prevents repeated alerts from becoming dozens of separate events.
- Event observations preserve repeated measurements.
- Confirmation tokens are hashed, expire, and are single-use.
- SMS through Twilio and optional email notification.
- Notification attempts are recorded in SQLite.
- Admin camera API protected by `FIREWATCH_ADMIN_TOKEN`.
- Camera status and recent-event panel on the map.
- Only **confirmed** device detections appear on the public map, in `/api/device-events`, and in route-avoidance geometry.
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

On Raspberry Pi OS, install the system packages first (`python3-picamera2` is only needed if a Pi Camera is attached; without one the node runs thermal-only):

```bash
sudo apt update
sudo apt install -y python3-numpy python3-pil python3-venv python3-picamera2
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements-pi.txt
cp camera_config.json.example camera_config.json
```

Edit `camera_config.json` with the generated `camera_key` and the camera's fixed registered coordinates. The server's registered coordinates are authoritative; the Pi does not need a GPS receiver.

Put the server address (and any tuning values from `.env.example`) in a `.env` file next to the script:

```bash
echo 'FIREWATCH_SERVER_URL=https://your-service.onrender.com' > .env
```

Test it once in the foreground (Ctrl+C to stop):

```bash
set -a; . ./.env; set +a
python sensor_monitor.py
```

Then install it as a service, so it starts at boot and restarts by itself if it crashes or loses the sensor:

```bash
./install-pi-service.sh
```

The script fills in your username and folder, installs `firewatch-node.service` into systemd and starts it. Useful commands afterwards:

```bash
systemctl status firewatch-node        # is it running?
journalctl -u firewatch-node -f        # live log (one line per sample)
sudo systemctl restart firewatch-node  # after editing .env or pulling new code
sudo systemctl disable --now firewatch-node  # stop it and stop starting at boot
```

Enable I2C (`sudo raspi-config` -> Interface Options -> I2C) before the first run.

How the node works:

1. Every `SAMPLE_SECONDS` (default 60) it reads one thermal frame and saves a heat-map snapshot to `FIREWATCH_TEMP_DIR` (default `/tmp/firewatch`). Cool snapshots are deleted straight away.
2. If the frame looks hot, it reads `CONFIRM_FRAMES` more frames (default 5) `CONFIRM_INTERVAL_SECONDS` apart. If most are hot, the hottest one becomes the event's heat-map, and a photo is taken if a camera is attached.
3. The event is written to `FIREWATCH_PENDING_DIR` (default `pending/` next to the script) and uploaded. Files are deleted only after the server accepts them; failed uploads retry every heartbeat. At most `MAX_PENDING_EVENTS` (default 20) are kept.
4. While the same fire keeps burning it does not alert again, until `EVENT_RESET_SAMPLES` (default 3) cool samples in a row.

Every sample is logged (`max`, `background`, `cluster`), so watch the output for a few days on site to calibrate. The thermal detector uses:

- absolute temperature threshold (`HOT_TEMP_C`, default 80)
- temperature above the scene background (`AMBIENT_DELTA_C`, default 25)
- minimum connected hot cluster (`MIN_CLUSTER_PIXELS`, default 6)
- optional minimum hot area as % of the frame (`MIN_HOT_PERCENT`, default 0 = off)

The MLX90640 has 32x24 pixels over 55 degrees, so each pixel covers roughly 1.7 degrees (about 3 m wide at 100 m) and reports that patch's average temperature. A small or distant fire reads far cooler than its flames and covers few pixels; lower `HOT_TEMP_C`/`MIN_CLUSTER_PIXELS` for longer viewing distances and test with a known heat source.

## Confirmation flow

1. Pi confirms a hot spot over a short burst of frames.
2. Pi uploads the heat-map (and photo, if any) to `/api/camera-event`.
3. Server authenticates the camera and uses its registered coordinates.
4. Server creates an `unconfirmed` event. It is not shown publicly.
5. SMS/email is sent with a link to `/review/{event_id}?token=...`.
6. The reviewer opens the page, looks at the heat-map and photo, and presses **Fire** or **Not a fire**. Opening the link changes nothing (link previews and email scanners cannot answer for you); only pressing a button does.
7. The token is consumed and the event becomes `confirmed` (public map + route avoidance) or `false_alarm` (stays private).

Links from older alerts (`/confirm/...`) now open the review page instead of acting directly.

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

Use HTTPS, firewall the server, keep secrets out of Git, back up SQLite and uploaded images, and consider a real background queue for notifications if this becomes a larger deployment. Render stores these under the persistent disk mount configured in `render.yaml`. The route avoid-zone is a prototype buffer around reported/confirmed points, not a surveyed evacuation boundary. For stronger device authentication, a future version can move from shared camera keys to signed HMAC request bodies with timestamp/replay protection.

This is a prototype detection/decision-support system, **not a certified life-safety wildfire warning system**. MLX90640 readings can be affected by distance, emissivity, sunlight, reflections, hot vehicles/equipment, and other heat sources. Confirmation and independent verification should be used before treating an event as a real wildfire.

The severity index is a relative 0–100 indicator based on hot area, persistence, and maximum temperature. It is not a scientific measurement of physical fire energy or true burned area.

## Render deployment

See `README_RENDER.md` for the Render setup. The important distinction is that Render uses `requirements.txt` while Raspberry Pi nodes use `requirements-pi.txt`.


## Dashboard navigation and satellite monitoring

The top-right panel supports driving, biking, and walking routes with turn-by-turn instructions. Route avoidance uses available reported/satellite detections and confirmed camera fires; unconfirmed camera events are only visible on their review page until someone confirms them. The panel also exposes the NASA FIRMS active-fire layer. Set `FIRMS_API_KEY` to enable active-fire satellite detections, and optionally `FIRMS_BBOX=west,south,east,north` to monitor a fixed bounding box instead of the camera footprint.

## Tests

The detection, Pi queue and review-flow logic is covered by tests that run on any machine (no Pi hardware needed):

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
```
