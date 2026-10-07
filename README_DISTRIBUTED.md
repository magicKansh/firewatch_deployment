# Firewatch Distributed Camera System

This version uses **SQLite instead of CSV for live application data** and supports **phone SMS alerts through Twilio**, while keeping email as an optional backup.

## Architecture

```text
Raspberry Pi cameras
  MLX90640 + Pi camera
          |
          | HTTPS multipart POST
          v
     Firewatch server
       |       |       |
       v       v       v
    SQLite   SMS/email  Map
               |
               v
       Confirm fire / false alarm
```

Each Pi still has a fixed `camera_id`, secret key, latitude, and longitude. The server's registered latitude/longitude is authoritative.

## 1. Install server dependencies

```bash
pip install -r requirements.txt
```

The `twilio` Python package is used for SMS delivery. Twilio's current documentation recommends environment variables for credentials in deployed applications. See the official SMS guide: https://www.twilio.com/docs/messaging/tutorials/how-to-send-sms-messages

## 2. SQLite

The server automatically creates:

```text
data/firewatch.db
```

The first startup also migrates the old `cameras.csv`, `subscribers.csv`, and `device_fires.csv` data if those tables are empty. After verifying the migration, those CSV files are no longer used by the application.

SQLite stores:

- cameras
- subscribers
- device fire events
- confirmation token hashes

## 3. Configure SMS

Create a Twilio account/number and set these environment variables:

```bash
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...
PUBLIC_BASE_URL=https://your-public-firewatch-domain.example
```

Phone numbers should be stored in international E.164 format, such as `+15555550123`. Twilio documents E.164 formatting and SMS sending in its Python quickstart.

For U.S./Canadian SMS traffic, Twilio may require applicable sender registration before production messaging. Check Twilio's current requirements for your account and number.

## 4. Add cameras and phone subscribers

Use the included management CLI instead of editing CSV files.

Add a camera:

```bash
python manage.py add-camera CAM-001 41.123456 -73.123456 --name "North Ridge"
```

The command generates a random camera key. Put that key into the Pi's `camera_config.json`.

Add a phone subscriber for one camera:

```bash
python manage.py add-subscriber CAM-001 --phone +15555550123
```

Or subscribe to every camera:

```bash
python manage.py add-subscriber '*' --phone +15555550123
```

Email can also be added with `--email`. Disable either channel with `--no-sms` or `--no-email-enabled`.

List configured cameras/subscribers with:

```bash
python manage.py list-cameras
python manage.py list-subscribers
```

## 5. Start the server

```bash
uvicorn map:app --host 0.0.0.0 --port 8000
```

The Pi continues to run:

```bash
python3 sensor_monitor.py
```

## Alert flow

1. Thermal sensor detects a persistent hot region.
2. Pi captures an image.
3. Pi sends the event to `/api/camera-event`.
4. Server validates the camera key.
5. Server uses the registered camera coordinates.
6. Event is stored in SQLite as `unconfirmed`.
7. SMS and/or email is sent.
8. Recipient taps **CONFIRM FIRE** or **FALSE ALARM**.
9. SQLite status changes to `confirmed` or `false_alarm`.
10. Confirmed device fires are included in route-avoidance geometry.

## Security notes

- Never commit `.env` or real Twilio credentials.
- Use HTTPS for the public server.
- Use long random camera keys.
- Use a production database backup strategy.
- Confirmation tokens are stored as SHA-256 hashes rather than plaintext.
- For a real emergency-warning deployment, add authentication, retry queues, audit logs, redundant notification paths, and independent fire verification. Do not rely on this system as the sole life-safety warning mechanism.

## Important limitation

The displayed `severity_index` is a relative 0-100 indicator based on hot-area percentage, persistence, and maximum temperature. It is **not** a scientific measurement of the fire's physical energy output or true wildfire area.
