import os
import smtplib
from email.message import EmailMessage
from database import get_subscribers, get_connection, utc_now

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


def _confirmation_text(event):
    eid = event["event_id"]
    yes = f"{PUBLIC_BASE_URL}/confirm/{eid}/yes?token={event['confirm_token']}"
    no = f"{PUBLIC_BASE_URL}/confirm/{eid}/no?token={event['confirm_token']}"
    return (f"FIREWATCH ALERT: Possible fire at {event['camera_id']}. "
            f"Hot area {event['fire_size_percent']:.1f}%, max {event['max_temperature_c']:.1f} C, "
            f"severity {event['severity_index']:.1f} ({event['magnitude']}).\n"
            f"CONFIRM: {yes}\nFALSE ALARM: {no}"), yes, no


def _record(event_id, subscriber_id, channel, destination, status, provider_id=None, error=None):
    with get_connection() as conn:
        conn.execute("""INSERT INTO notification_attempts
        (event_id,subscriber_id,channel,destination,attempt_number,status,provider_message_id,error,created_at)
        VALUES (?,?,?,?,1,?,?,?,?)""", (event_id, subscriber_id, channel, destination, status, provider_id, error, utc_now()))


def send_fire_confirmation(event):
    subscribers = get_subscribers(event["camera_id"])
    body, _, _ = _confirmation_text(event)
    sms_sent = email_sent = 0
    errors = []
    if Client and all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        for s in subscribers:
            if not s.get("phone") or not int(s.get("sms_enabled", 1)):
                continue
            try:
                msg = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=s["phone"].strip())
                sms_sent += 1
                _record(event["event_id"], s["id"], "sms", s["phone"].strip(), "sent", msg.sid)
            except Exception as exc:
                errors.append(f"SMS {s['phone']}: {exc}")
                _record(event["event_id"], s["id"], "sms", s["phone"].strip(), "failed", error=str(exc))
    else:
        errors.append("Twilio SMS is not configured.")

    emails = []
    for s in subscribers:
        if s.get("email") and int(s.get("email_enabled", 1)):
            emails.append(s)
    if emails and all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, FROM_EMAIL]):
        for s in emails:
            try:
                msg = EmailMessage()
                msg["Subject"] = f"Firewatch: Possible fire detected by {event['camera_id']}"
                msg["From"] = FROM_EMAIL
                msg["To"] = s["email"].strip()
                msg.set_content(body)
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
