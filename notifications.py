import os
import smtplib
from email.message import EmailMessage

from database import IMAGE_DIR, get_connection, get_subscribers, utc_now

try:
    from twilio.rest import Client
except ImportError:
    Client = None

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER or "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000"
PUBLIC_BASE_URL = PUBLIC_BASE_URL.rstrip("/")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
# MMS lets the heat image show up inside the text message. Twilio only fetches media from a
# public HTTPS URL, and MMS is not available for every country; we fall back to plain SMS.
TWILIO_SEND_MMS = os.getenv("TWILIO_SEND_MMS", "1").strip().lower() in {"1", "true", "yes", "on"}


def review_url(event):
    return f"{PUBLIC_BASE_URL}/review/{event['event_id']}?token={event['confirm_token']}"


def _test_prefix(event):
    return "[TEST] " if event.get("is_test") else ""


def _alert_text(event):
    return (f"{_test_prefix(event)}FIREWATCH: possible fire seen by {event.get('place') or event['camera_id']}. "
            f"Max {event['max_temperature_c']:.0f} C, hot area {event['fire_size_percent']:.1f}% "
            f"({event['magnitude']}). Is it a fire? Look at the heat image and answer here:\n"
            f"{review_url(event)}")


def _image_file(event):
    relative = event.get("image") or ""
    if not relative.startswith("images/"):
        return None
    path = IMAGE_DIR / relative[len("images/"):]
    return path if path.is_file() else None


def _record(event_id, subscriber_id, channel, destination, status, provider_id=None, error=None):
    with get_connection() as conn:
        conn.execute("""INSERT INTO notification_attempts
        (event_id,subscriber_id,channel,destination,attempt_number,status,provider_message_id,error,created_at)
        VALUES (?,?,?,?,1,?,?,?,?)""", (event_id, subscriber_id, channel, destination, status, provider_id, error, utc_now()))


def send_fire_confirmation(event):
    subscribers = get_subscribers(event["camera_id"])
    body = _alert_text(event)
    sms_sent = email_sent = 0
    errors = []
    if not subscribers:
        errors.append("No enabled notification recipients are configured for this camera or '*'.")

    media = []
    if TWILIO_SEND_MMS and PUBLIC_BASE_URL.startswith("https://") and _image_file(event):
        media = [f"{PUBLIC_BASE_URL}/{event['image']}"]

    if Client and all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        for s in subscribers:
            if not s.get("phone") or not int(s.get("sms_enabled", 1)):
                continue
            phone = s["phone"].strip()
            try:
                try:
                    msg = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=phone,
                                                 **({"media_url": media} if media else {}))
                    channel = "mms" if media else "sms"
                except Exception:
                    if not media:
                        raise
                    msg = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=phone)
                    channel = "sms"
                sms_sent += 1
                _record(event["event_id"], s["id"], channel, phone, "sent", msg.sid)
            except Exception as exc:
                errors.append(f"SMS {phone}: {exc}")
                _record(event["event_id"], s["id"], "sms", phone, "failed", error=str(exc))
    else:
        errors.append("Twilio SMS is not configured.")

    emails = [s for s in subscribers if s.get("email") and int(s.get("email_enabled", 1))]
    if emails and all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, FROM_EMAIL]):
        image = _image_file(event)
        for s in emails:
            try:
                msg = EmailMessage()
                msg["Subject"] = f"{_test_prefix(event)}Firewatch: possible fire at {event.get('place') or event['camera_id']} - please review"
                msg["From"] = FROM_EMAIL
                msg["To"] = s["email"].strip()
                msg.set_content(body + "\n\nThe heat-signature image is attached. Nothing is published "
                                       "to the community map until you answer.")
                if image:
                    msg.add_attachment(image.read_bytes(), maintype="image",
                                       subtype="png" if image.suffix == ".png" else "jpeg",
                                       filename=f"firewatch_{event['event_id']}{image.suffix}")
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                    smtp.starttls(); smtp.login(SMTP_USER, SMTP_PASSWORD); smtp.send_message(msg)
                email_sent += 1
                _record(event["event_id"], s["id"], "email", s["email"].strip(), "sent")
            except Exception as exc:
                errors.append(f"Email {s['email']}: {exc}")
                _record(event["event_id"], s["id"], "email", s["email"].strip(), "failed", error=str(exc))
    elif emails:
        errors.append("SMTP email is not configured.")

    return {"sms_sent": sms_sent, "email_sent": email_sent, "errors": errors}
