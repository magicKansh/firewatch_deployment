import argparse
import secrets
from database import get_connection, init_db, hash_camera_key, utc_now


def add_camera(args):
    init_db(); key = args.camera_key or secrets.token_urlsafe(32)
    with get_connection() as conn:
        conn.execute("INSERT INTO cameras(camera_id,camera_key,latitude,longitude,name,created_at) VALUES (?,?,?,?,?,?)",
                     (args.camera_id, hash_camera_key(key), args.latitude, args.longitude, args.name or args.camera_id, utc_now()))
    print(f"Camera added: {args.camera_id}\nCamera key: {key}\nSave this key in camera_config.json on the Pi.")


def add_subscriber(args):
    init_db()
    if not args.email and not args.phone: raise SystemExit("Provide --email and/or --phone")
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO subscribers(camera_id,email,phone,sms_enabled,email_enabled) VALUES (?,?,?,?,?)",
                     (args.camera_id, args.email, args.phone, int(args.sms), int(args.email_enabled)))
    print("Subscriber added.")


def list_cameras(_):
    with get_connection() as conn: rows = conn.execute("SELECT camera_id,name,latitude,longitude,enabled,status,last_seen_at,software_version FROM cameras ORDER BY camera_id").fetchall()
    for r in rows: print(dict(r))


def list_subscribers(_):
    with get_connection() as conn: rows = conn.execute("SELECT id,camera_id,email,phone,sms_enabled,email_enabled,enabled FROM subscribers ORDER BY id").fetchall()
    for r in rows: print(dict(r))

parser = argparse.ArgumentParser(description="Firewatch database administration")
sub = parser.add_subparsers(dest="command", required=True)
p = sub.add_parser("add-camera"); p.add_argument("camera_id"); p.add_argument("latitude", type=float); p.add_argument("longitude", type=float); p.add_argument("--name"); p.add_argument("--camera-key"); p.set_defaults(func=add_camera)
p = sub.add_parser("add-subscriber"); p.add_argument("camera_id"); p.add_argument("--phone"); p.add_argument("--email"); p.add_argument("--sms", action=argparse.BooleanOptionalAction, default=True); p.add_argument("--email-enabled", action=argparse.BooleanOptionalAction, default=True); p.set_defaults(func=add_subscriber)
p = sub.add_parser("list-cameras"); p.set_defaults(func=list_cameras)
p = sub.add_parser("list-subscribers"); p.set_defaults(func=list_subscribers)
args = parser.parse_args(); args.func(args)
