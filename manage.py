import argparse
import secrets

from database import get_connection, init_db


def add_camera(args):
    init_db()
    key = args.camera_key or secrets.token_urlsafe(32)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO cameras(camera_id,camera_key,latitude,longitude,name)
               VALUES (?,?,?,?,?)""",
            (args.camera_id, key, args.latitude, args.longitude, args.name or args.camera_id),
        )
    print(f"Camera added: {args.camera_id}")
    print(f"Camera key: {key}")
    print("Save this key in that Raspberry Pi's camera_config.json.")


def add_subscriber(args):
    init_db()
    if not args.email and not args.phone:
        raise SystemExit("Provide --email and/or --phone")
    with get_connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO subscribers(camera_id,email,phone,sms_enabled,email_enabled)
               VALUES (?,?,?,?,?)""",
            (args.camera_id, args.email, args.phone, int(args.sms), int(args.email_enabled)),
        )
    print("Subscriber added.")


def list_cameras(_args):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT camera_id,name,latitude,longitude,enabled FROM cameras ORDER BY camera_id"
        ).fetchall()
    for r in rows:
        print(dict(r))


def list_subscribers(_args):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id,camera_id,email,phone,sms_enabled,email_enabled FROM subscribers ORDER BY id"
        ).fetchall()
    for r in rows:
        print(dict(r))


parser = argparse.ArgumentParser(description="Firewatch database administration")
sub = parser.add_subparsers(dest="command", required=True)

p = sub.add_parser("add-camera")
p.add_argument("camera_id")
p.add_argument("latitude", type=float)
p.add_argument("longitude", type=float)
p.add_argument("--name")
p.add_argument("--camera-key")
p.set_defaults(func=add_camera)

p = sub.add_parser("add-subscriber")
p.add_argument("camera_id", help="Camera ID or * for every camera")
p.add_argument("--phone")
p.add_argument("--email")
p.add_argument("--sms", action=argparse.BooleanOptionalAction, default=True)
p.add_argument("--email-enabled", action=argparse.BooleanOptionalAction, default=True)
p.set_defaults(func=add_subscriber)

p = sub.add_parser("list-cameras")
p.set_defaults(func=list_cameras)

p = sub.add_parser("list-subscribers")
p.set_defaults(func=list_subscribers)

args = parser.parse_args()
args.func(args)
