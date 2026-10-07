import os
import smtplib
from email.message import EmailMessage

from database import get_subscribers

try:
    from twilio.rest import Client
except ImportError:  # SMS is optional until the package is installed.
    Client = None

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER or "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")


def _confirmation_text(event):
    event_id = event["event_id"]
    yes_url = f"{PUBLIC_BASE_URL}/confirm/{event_id}/yes?token={event['confirm_token']}"
    no_url = f"{PUBLIC_BASE_URL}/confirm/{event_id}/no?token={event['confirm_token']}"
    body = (
        f"FIREWATCH ALERT: Possible fire at {event['camera_id']}. "
        f"Hot area {event['fire_size_percent']:.1f}%, max {event['max_temperature_c']:.1f} C, "
        f"severity {event['severity_index']:.1f} ({event['magnitude']}).\n\n"
        f"CONFIRM FIRE: {yes_url}\n"
        f"FALSE ALARM: {no_url}"
    )
    return body, yes_url, no_url


def _send_sms(numbers, body):
    if not numbers:
        return 0, "No phone subscribers configured."
    if Client is None:
        return 0, "Twilio package is not installed."
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        return 0, "Twilio environment variables are not configured."

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    sent = 0
    errors = []
    for number in numbers:
        try:
            client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=number)
            sent += 1
        except Exception as exc:
            errors.append(f"{number}: {exc}")
    return sent, "; ".join(errors) if errors else "ok"


def _send_email(addresses, event, body):
    if not addresses:
        return 0, "No email subscribers configured."
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, FROM_EMAIL]):
        return 0, "SMTP environment variables are not configured."

    msg = EmailMessage()
    msg["Subject"] = f"Firewatch: Possible fire detected by {event['camera_id']}"
    msg["From"] = FROM_EMAIL
    msg["To"] = ", ".join(addresses)
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)
    return len(addresses), "ok"


def send_fire_confirmation(event):
    subscribers = get_subscribers(event["camera_id"])
    phones = list(dict.fromkeys(
        s["phone"].strip() for s in subscribers
        if s.get("phone") and int(s.get("sms_enabled", 1))
    ))
    emails = list(dict.fromkeys(
        s["email"].strip() for s in subscribers
        if s.get("email") and int(s.get("email_enabled", 1))
    ))

    body, _, _ = _confirmation_text(event)
    sms_sent, sms_status = _send_sms(phones, body)
    email_sent, email_status = _send_email(emails, event, body)
    return {
        "sms_sent": sms_sent,
        "sms_status": sms_status,
        "email_sent": email_sent,
        "email_status": email_status,
    }
