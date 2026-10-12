# Firewatch — Render deployment

## What changed

## Dashboard redesign

The current `templates/index.html` and `map.py` provide the enterprise-style Firewatch dashboard: a top header with global search and service-health status, floating camera/event metrics, a light Carto Positron map, top-right navigation controls for driving/biking/walking, turn-by-turn directions, and a NASA FIRMS visibility switch. No React build step is needed because this repository uses FastAPI, Jinja2, and Plotly directly.


The server and Raspberry Pi dependencies are now separated. Render installs only the server packages, so it will not try to compile `picamera2`, `python-prctl`, or other Raspberry Pi-only libraries.

- `requirements.txt` — central Firewatch server
- `requirements-pi.txt` — Python packages for the Raspberry Pi node (Picamera2/libcamera are installed from Raspberry Pi OS)
- `render.yaml` — Render web-service configuration

## Deploy with Render

1. Put this project in a Git repository and push it.
2. In Render, create a new Blueprint and select the repository.
3. Render will read `render.yaml`.
4. The service uses:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn map:app --host 0.0.0.0 --port $PORT`
   - Health check: `/health`
5. Enter the secret values requested by `render.yaml`.
6. Set `PUBLIC_BASE_URL` to the exact public HTTPS URL of the deployed service, for example `https://your-service.onrender.com`.
7. Deploy.

The Blueprint attaches a 10 GB persistent disk at `/var/data`. The SQLite database is stored at `/var/data/firewatch.db` and uploaded images at `/var/data/images`, so those files survive normal deploys/restarts. Render persistent disks require a paid service plan and have single-instance limitations. For a larger multi-instance deployment, move the database to Render Postgres and images to object storage.

## Required Render secrets

At minimum for the complete system:

- `FIREWATCH_ADMIN_TOKEN`
- `PUBLIC_BASE_URL`
- `ORS_API_KEY` for address search/routing
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`

Optional:

- `FIRMS_API_KEY`
- SMTP variables for email notifications

Generate a strong admin token rather than using the example value.

## After deployment

Open:

- `/` — Firewatch map
- `/health` — health check
- `/docs` — FastAPI API documentation

Create a camera through the protected admin API. Replace the URL and token with your deployed values:

```bash
curl -X POST "https://your-service.onrender.com/api/admin/cameras" \
  -H "Authorization: Bearer YOUR_FIREWATCH_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"camera_id":"CAM-001","latitude":41.123456,"longitude":-73.123456,"name":"North Ridge"}'
```

The response contains the generated camera key. Save it securely; it goes into the Pi's `camera_config.json`.

Add a phone recipient through `/docs` using `POST /api/admin/subscribers`, also with `Authorization: Bearer YOUR_FIREWATCH_ADMIN_TOKEN`:

```json
{
  "camera_id": "*",
  "phone": "+15555550123",
  "sms_enabled": true,
  "email_enabled": false
}
```

Use `camera_id: "*"` to receive alerts from all cameras, or set a specific camera ID. SMS requires the Twilio environment variables to be configured; email requires the SMTP variables.

## Raspberry Pi installation

Do **not** install the server `requirements.txt` on the Pi just to run the detector. On each Raspberry Pi:

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-numpy python3-pil python3-venv ffmpeg
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements-pi.txt
```

Then configure `camera_config.json` with the registered camera ID/key and stored coordinates, and run (replace the example URL):

```bash
export FIREWATCH_SERVER_URL="https://your-service.onrender.com"
python3 sensor_monitor.py
```

The Pi connects to the deployed Firewatch server through `FIREWATCH_SERVER_URL`. On persistent thermal/video evidence, the node uploads an image and attempts a short MP4 clip. The visual check is only a simple bright/warm-color heuristic, not a trained fire model; human confirmation remains required.

## Why the original Render build failed

The old requirements file contained `picamera2`. That package depends on Raspberry Pi/Linux camera components and `python-prctl`; a normal Render build environment does not provide the Raspberry Pi-specific development environment required to compile it. The central server does not need `picamera2`, so it has been removed from the server dependency set.

## Important production limitation

Firewatch is a prototype detection and decision-support system, not a certified life-safety wildfire warning system. Thermal readings can be affected by distance, emissivity, sunlight, reflections, hot vehicles/equipment, and other heat sources. Use independent verification and appropriate emergency procedures before treating an alert as a real wildfire.


### NASA FIRMS satellite fire layer

The top-right dashboard panel includes a toggle for NASA FIRMS active-fire detections. Set `FIRMS_API_KEY` in the Render environment to enable it. By default the server requests detections around registered enabled cameras (or around reported-fire coordinates when there are no registered cameras). You can optionally set `FIRMS_BBOX` to `west,south,east,north` to monitor a fixed area instead, for example `-75.0,39.0,-73.0,42.0`. The toggle controls map visibility; it does not disable confirmed/report/satellite hazard avoidance in routing.

## Navigation UI updates

The dashboard now uses a dark translucent map-overlay style with orange route highlighting, an interactive location autocomplete menu, and a browser geolocation button for the route start. Suggestions rank specific features (venues, addresses, streets, neighbourhoods) before broader areas. The current-location button requires the site to be served over HTTPS (Render does this automatically) and the user to grant browser location permission. If reverse geocoding is unavailable, coordinate input is still accepted by the route API.

For live autocomplete and address routing, configure `ORS_API_KEY` in the Render service environment. For satellite fire detections, configure `FIRMS_API_KEY`.
