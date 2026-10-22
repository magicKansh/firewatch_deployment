# Firewatch Distributed Camera System

Firewatch is a prototype distributed wildfire-monitoring and route-avoidance system. Each Raspberry Pi uses an MLX90640 thermal sensor, with an optional Pi Camera. Persistent thermal or warm/flickering video clues create an unconfirmed event; the Pi uploads a snapshot and a short MP4 clip when recording succeeds. The video rule is a lightweight heuristic, not a trained fire-classification model, so human confirmation remains important.

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

Give the people who look after the camera a login (prints a password once):

```bash
python manage.py set-camera-password CAM-001
```

Or do all of this from the admin control panel at `/admin` (see below).

## Raspberry Pi setup

Hardware: Raspberry Pi 5 + Adafruit MLX90640 (55°) on I2C (3V3, GND, SDA = GPIO2, SCL = GPIO3). A Pi Camera is **optional**: without one the node runs thermal-only, and with one it also attaches a visible photo, a short clip and a colour heuristic.

On the Pi, from a clone of this repository:

```bash
sudo ./pi/install.sh
```

The script installs the packages, enables I2C at 1 MHz, creates a `firewatch` service user, copies the node to `/opt/firewatch`, and installs the `firewatch-node` systemd service. The service starts on boot and restarts if it crashes. Then:

1. Put the camera ID and key from `manage.py add-camera` (or the admin API) into `/etc/firewatch/camera_config.json`.
2. Set `FIREWATCH_SERVER_URL` in `/etc/firewatch/node.env`. Detection tuning also lives there.
3. Reboot if the script asks you to, then check that the sensor is found with `i2cdetect -y 1` (it shows `33`).
4. Watch it run: `journalctl -u firewatch-node -f`.

To update later, run `git pull && sudo ./pi/install.sh`.

### What the node does

- **Watches continuously.** It reads the thermal frame 4 times a second. A pixel is hot when it is at least `HOT_TEMP_C` and at least `AMBIENT_DELTA_C` above the scene background. A detection needs a connected hot cluster of `MIN_CLUSTER_PIXELS` that persists for `REQUIRED_FRAMES` frames.
- **Takes a snapshot every minute.** Each `SNAPSHOT_SECONDS` it saves a heat-map snapshot to RAM (`/dev/shm/firewatch`). Only the newest `SNAPSHOT_KEEP` are kept; older ones are deleted, and nothing is written to the SD card.
- **Builds the alert image on detection.** It renders a heat-signature image with the hotspot boxed. It attaches the last `CONTEXT_SNAPSHOTS` minute snapshots as "before" images, plus the photo and clip when a Pi Camera is present. Then it uploads everything.
- **Queues events when offline.** If the server can't be reached, the event goes to `/var/lib/firewatch/outbox` and is retried every minute. Events the server rejects (for example, a wrong camera key) are logged and dropped rather than retried forever.
- **Updates ongoing events.** While the heat persists, it sends an updated image every `EVENT_UPDATE_SECONDS`. The server adds these to the same event, so no new alert goes out.
- **Restarts on sensor failure.** If the sensor stops answering, the node exits and systemd restarts it. If the sensor is really gone, heartbeats stop and the dashboard shows the camera as offline.

**Tuning tip:** the MLX90640 has 32×24 pixels across 55°. At 30 m one pixel covers about 0.9 m, so a small, distant fire is only a few pixels. Tune `MIN_CLUSTER_PIXELS` and `HOT_TEMP_C` for your view, and use `THERMAL_FLIP_H` / `THERMAL_FLIP_V` if the image is mirrored.

## Logins, who sees what, and the admin panel

| | Visitor | Signed in to a camera | Admin |
|---|---|---|---|
| Confirmed real fires (map, list, route avoidance) | yes | yes | yes |
| Camera markers, status, camera list | no | own cameras | all |
| Unreviewed and false-alarm detections, pending count | no | own cameras | all |
| Review a detection from the dashboard | no | own cameras | all |
| Test fires (labelled TEST) | no | no | yes |

**Camera logins.** Each camera has a login (camera ID + password) for the people who look after it, separate from the Pi's device key. They sign in at `/login`; someone with several cameras can sign in to each one in the same browser ("Add another camera"). Signed-in users see their cameras on the map and in the sidebar, and get a **Review this detection** link next to each unreviewed alert, without needing the SMS link. Sessions last 30 days and are stored server-side; resetting a camera's password, or disabling the camera, signs everyone out of it immediately. Login attempts are limited to 10 per minute per IP.

**Admin panel.** Sign in at `/admin/login` with `FIREWATCH_ADMIN_TOKEN` as the password. At `/admin` you can:

- add a camera (shows its Pi key and login password once),
- create or reset a camera's login password, reset its Pi key, enable or disable it,
- **simulate a camera detection**: the server draws a fake heat-map with the same code as the Pi, creates an unconfirmed event and sends the real alerts prefixed with `[TEST]`, so you can walk through the whole review flow,
- **drop a confirmed fire on the map** (click the map to place it) to test map display and route avoidance,
- list and delete test fires, or delete them all (their images are removed too).

Test fires are only visible to admins. The public and camera logins never see them, and only admins' route planning avoids them. Test events never merge with real ones from the same camera.

## Review flow and the community dashboard

1. The Pi detects persistent heat and uploads the heat-signature image.
2. The server creates an `unconfirmed` event. This event is **not public**.
3. Subscribers get an SMS (sent as MMS with the image when Twilio allows it) and/or an email with the image attached. Each message links to `/review/{event_id}?token=…`.
4. The review page shows the heat image, the earlier snapshots and the readings. The reviewer picks **"Yes, it's a fire"** or **"No, it's something else"**. For "something else", they also say what it was (sun or reflection, vehicle, chimney/grill, controlled burn, person/animal, other) and can add a private note.
5. **Yes** marks the event `confirmed`. It then appears on the community map and in the sidebar, and route planning avoids it. **No** marks it `false_alarm` and keeps it private.
6. **No repeat alerts for a dismissed spot.** For `FALSE_ALARM_COOLDOWN_SECONDS` after a dismissed spot was last seen, new detections within `FALSE_ALARM_RADIUS_PX` of it are logged quietly instead of re-alerting. This stops repeat alerts from a chimney or sun glare. A different spot, or the same spot getting `FALSE_ALARM_TEMP_MARGIN_C` hotter, still alerts.

**Links are safe to open.** Opening the review link never changes anything; only pressing a button does. Email security scanners open links automatically, so this matters. Old `/confirm/...` links now open the review page. Each link works once and expires after `CONFIRMATION_TOKEN_TTL_HOURS`. Admins can review any event with `POST /api/admin/events/{event_id}/review`, for example when an alert was missed.

The public `GET /api/device-events` returns only confirmed real fires. Signed-in camera users also get their own cameras' other events and pending count; admins (session or bearer token) get everything, including test fires. `GET /api/cameras/status` returns nothing to visitors, own cameras to camera logins, and all cameras to admins.

## Admin API

Set `FIREWATCH_ADMIN_TOKEN` and send:

```text
Authorization: Bearer YOUR_TOKEN
```

Endpoints:

- `GET /api/admin/cameras`
- `POST /api/admin/cameras` (returns the Pi key and a login password, each shown once)
- `POST /api/admin/cameras/{camera_id}/login-password` (new login password; signs that camera out everywhere)
- `PUT /api/admin/cameras/{camera_id}`
- `DELETE /api/admin/cameras/{camera_id}` (disables it and signs its viewers out)
- `GET /api/admin/subscribers`
- `POST /api/admin/subscribers`
- `GET /api/cameras/status`
- `GET /api/device-events` (everything with the admin token, including test fires; public callers only see confirmed real fires)
- `POST /api/admin/events/{event_id}/review` with `{"decision": "fire" | "not_fire", "label": "sun_glare", "note": ""}`

## Important production notes

Use HTTPS, firewall the server, keep secrets out of Git, back up SQLite and uploaded clips, and consider a real background queue for notifications if this becomes a larger deployment. Render stores these under the persistent disk mount configured in `render.yaml`. The route avoid-zone is a prototype buffer around reported/confirmed points, not a surveyed evacuation boundary. For stronger device authentication, a future version can move from shared camera keys to signed HMAC request bodies with timestamp/replay protection.

This is a prototype detection/decision-support system, **not a certified life-safety wildfire warning system**. MLX90640 readings can be affected by distance, emissivity, sunlight, reflections, hot vehicles/equipment, and other heat sources. Confirmation and independent verification should be used before treating an event as a real wildfire.

The severity index is a relative 0–100 indicator based on hot area, persistence, and maximum temperature. It is not a scientific measurement of physical fire energy or true burned area.

## Render deployment

See `README_RENDER.md` for the Render setup. The important distinction is that Render uses `requirements.txt` while Raspberry Pi nodes use `requirements-pi.txt`.


## Dashboard navigation and satellite monitoring

The top-right panel supports driving, biking, and walking routes with turn-by-turn instructions. Route avoidance uses available reported/satellite detections and confirmed camera fires; unconfirmed camera events stay visible for review and are not automatically treated as confirmed device hazards. The panel also exposes the NASA FIRMS active-fire layer. Set `FIRMS_API_KEY` to enable active-fire satellite detections, and optionally `FIRMS_BBOX=west,south,east,north` to monitor a fixed bounding box instead of the camera footprint.

## Tests

```bash
pip install -r requirements.txt pytest httpx pillow numpy
pytest
```

`tests/test_node.py` exercises the Pi node with a simulated sensor and server. `tests/test_server.py` covers the full detection → review → dashboard flow. Both run on every push through GitHub Actions.
