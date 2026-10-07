# Firewatch — Render deployment

## What changed

The server and Raspberry Pi dependencies are now separated. Render installs only the server packages, so it will not try to compile `picamera2`, `python-prctl`, or other Raspberry Pi-only libraries.

- `requirements.txt` — central Firewatch server
- `requirements-pi.txt` — Raspberry Pi camera/sensor node
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

## Raspberry Pi installation

Do **not** install the server `requirements.txt` on the Pi just to run the detector. On each Raspberry Pi:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-pi.txt
```

Then configure `camera_config.json` and run:

```bash
python3 sensor_monitor.py
```

The Pi connects to the deployed Firewatch server through `FIREWATCH_SERVER_URL`.

## Why the original Render build failed

The old requirements file contained `picamera2`. That package depends on Raspberry Pi/Linux camera components and `python-prctl`; a normal Render build environment does not provide the Raspberry Pi-specific development environment required to compile it. The central server does not need `picamera2`, so it has been removed from the server dependency set.

## Important production limitation

Firewatch is a prototype detection and decision-support system, not a certified life-safety wildfire warning system. Thermal readings can be affected by distance, emissivity, sunlight, reflections, hot vehicles/equipment, and other heat sources. Use independent verification and appropriate emergency procedures before treating an alert as a real wildfire.
