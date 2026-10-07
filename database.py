import csv
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("FIREWATCH_DB", DATA_DIR / "firewatch.db"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cameras (
                camera_id TEXT PRIMARY KEY,
                camera_key TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_seen_at TEXT,
                last_heartbeat_at TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                software_version TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL DEFAULT '*',
                email TEXT,
                phone TEXT,
                sms_enabled INTEGER NOT NULL DEFAULT 1,
                email_enabled INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(camera_id, email, phone)
            );

            CREATE TABLE IF NOT EXISTS device_events (
                event_id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT,
                false_alarm_at TEXT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                fire_size_percent REAL NOT NULL,
                max_temperature_c REAL NOT NULL,
                duration_seconds REAL NOT NULL,
                severity_index REAL NOT NULL,
                magnitude TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unconfirmed',
                image TEXT NOT NULL DEFAULT '',
                place TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'device',
                dedupe_key TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
            );

            CREATE TABLE IF NOT EXISTS event_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                fire_size_percent REAL NOT NULL,
                max_temperature_c REAL NOT NULL,
                duration_seconds REAL NOT NULL,
                image TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(event_id) REFERENCES device_events(event_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS confirmation_tokens (
                event_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                FOREIGN KEY(event_id) REFERENCES device_events(event_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notification_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                subscriber_id INTEGER,
                channel TEXT NOT NULL,
                destination TEXT NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                provider_message_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES device_events(event_id) ON DELETE CASCADE,
                FOREIGN KEY(subscriber_id) REFERENCES subscribers(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_timestamp ON device_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_status ON device_events(status);
            CREATE INDEX IF NOT EXISTS idx_events_camera_status ON device_events(camera_id,status,last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_events_dedupe ON device_events(camera_id,dedupe_key,last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_observations_event ON event_observations(event_id,timestamp);
            CREATE INDEX IF NOT EXISTS idx_subscribers_camera ON subscribers(camera_id,enabled);
            CREATE INDEX IF NOT EXISTS idx_notifications_event ON notification_attempts(event_id,created_at);
            """
        )
        # Lightweight migration for databases created by the previous build.
        migrations = {
            "cameras": {
                "status": "TEXT NOT NULL DEFAULT 'unknown'",
                "last_seen_at": "TEXT",
                "last_heartbeat_at": "TEXT",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "software_version": "TEXT NOT NULL DEFAULT ''",
            },
            "subscribers": {"enabled": "INTEGER NOT NULL DEFAULT 1"},
            "device_events": {
                "first_seen_at": "TEXT NOT NULL DEFAULT ''",
                "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "confirmed_at": "TEXT",
                "false_alarm_at": "TEXT",
                "dedupe_key": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in migrations.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _table_empty(conn, table):
    return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None


def migrate_legacy_csvs():
    init_db()
    with get_connection() as conn:
        cameras_csv = DATA_DIR / "cameras.csv"
        if cameras_csv.exists() and _table_empty(conn, "cameras"):
            with cameras_csv.open(newline="") as f:
                for r in csv.DictReader(f):
                    if not r.get("camera_id") or not r.get("camera_key"):
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO cameras
                        (camera_id,camera_key,latitude,longitude,name,created_at)
                        VALUES (?,?,?,?,?,?)""",
                        (r["camera_id"], r["camera_key"], float(r["latitude"]),
                         float(r["longitude"]), r.get("name", r["camera_id"]), utc_now()),
                    )

        subs_csv = DATA_DIR / "subscribers.csv"
        if subs_csv.exists() and _table_empty(conn, "subscribers"):
            with subs_csv.open(newline="") as f:
                for r in csv.DictReader(f):
                    email = (r.get("email") or "").strip() or None
                    phone = (r.get("phone") or "").strip() or None
                    if email or phone:
                        conn.execute(
                            """INSERT OR IGNORE INTO subscribers
                            (camera_id,email,phone) VALUES (?,?,?)""",
                            (r.get("camera_id") or "*", email, phone),
                        )

        events_csv = DATA_DIR / "device_fires.csv"
        if events_csv.exists() and _table_empty(conn, "device_events"):
            with events_csv.open(newline="") as f:
                for r in csv.DictReader(f):
                    if not r.get("event_id") or not r.get("camera_id"):
                        continue
                    timestamp = r.get("timestamp") or utc_now()
                    conn.execute(
                        """INSERT OR IGNORE INTO device_events
                        (event_id,camera_id,timestamp,first_seen_at,last_seen_at,latitude,longitude,
                         fire_size_percent,max_temperature_c,duration_seconds,severity_index,magnitude,
                         status,image,place,source,dedupe_key)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["event_id"], r["camera_id"], timestamp, timestamp, timestamp,
                         float(r["latitude"]), float(r["longitude"]), float(r["fire_size_percent"]),
                         float(r["max_temperature_c"]), float(r["duration_seconds"]),
                         float(r["severity_index"]), r["magnitude"], r.get("status", "unconfirmed"),
                         r.get("image", ""), r.get("place", ""), r.get("source", "device"), ""),
                    )


def hash_camera_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def camera_key_matches(stored, supplied):
    # Supports legacy plaintext keys while allowing new deployments to store hashes.
    supplied_hash = hash_camera_key(supplied)
    return hmac.compare_digest(str(stored), str(supplied)) or hmac.compare_digest(str(stored), supplied_hash)


def get_camera(camera_id, include_disabled=False):
    with get_connection() as conn:
        sql = "SELECT * FROM cameras WHERE camera_id=?"
        params = [camera_id]
        if not include_disabled:
            sql += " AND enabled=1"
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def get_subscribers(camera_id):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM subscribers
               WHERE enabled=1 AND camera_id IN ('*', ?)
                 AND (email IS NOT NULL OR phone IS NOT NULL)""",
            (camera_id,),
        ).fetchall()
        return [dict(r) for r in rows]


init_db()
