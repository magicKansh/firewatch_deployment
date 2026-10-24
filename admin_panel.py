"""Admin control panel: camera logins and keys, and (admin-only) test fires."""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import Viewer, get_viewer
from camera_api import add_camera, reset_login_password, reset_pi_key, set_camera_enabled
from database import TEST_CAMERA_ID, get_connection
from device_api import camera_status
import test_fires

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
ADMIN_VIEWER = Viewer(is_admin=True)


def _guard(viewer):
    """Redirect to the admin sign-in page unless this is an admin."""
    return None if viewer.is_admin else RedirectResponse("/admin/login", status_code=303)


def _cameras():
    with get_connection() as conn:
        has_login = {r["camera_id"]: r["login_password_hash"] is not None for r in conn.execute(
            "SELECT camera_id, login_password_hash FROM cameras WHERE camera_id<>?", (TEST_CAMERA_ID,))}
    cameras = camera_status(ADMIN_VIEWER)["cameras"]
    for camera in cameras:
        camera["has_login"] = has_login.get(camera["camera_id"], False)
    return cameras


def _page(request, message="", error="", status_code=200, **extra):
    return templates.TemplateResponse(request=request, name="admin.html", status_code=status_code, context={
        "cameras": _cameras(), "test_fires": test_fires.list_test_fires(), "severities": list(test_fires.SEVERITY_INDEX),
        "message": message, "error": error, "carto_key": os.getenv("CARTO_API_KEY", "").strip(), **extra,
    })


def _secret_page(request, title, secrets, note=""):
    """Show freshly generated secrets once. Never cached by the browser."""
    response = templates.TemplateResponse(request=request, name="admin_secret.html", context={
        "title": title, "secrets": secrets, "note": note,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


PI_KEY_HELP = "Put this in camera_config.json on the Pi (camera_key)."
LOGIN_HELP = "Give this to the people who look after this camera. They sign in at /login with the camera ID."


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, viewer: Viewer = Depends(get_viewer)):
    return _guard(viewer) or _page(request)


@router.post("/admin/cameras", response_class=HTMLResponse)
def admin_add_camera(request: Request, camera_id: str = Form(""), name: str = Form(""),
                     latitude: str = Form(""), longitude: str = Form(""), viewer: Viewer = Depends(get_viewer)):
    if (redirect := _guard(viewer)):
        return redirect
    try:
        with get_connection() as conn:
            pi_key, password = add_camera(conn, camera_id, float(latitude), float(longitude), name)
    except ValueError as exc:
        message = str(exc) if "could not convert" not in str(exc) else "Latitude and longitude must be numbers."
        return _page(request, error=message, status_code=400)
    return _secret_page(request, f"Camera {camera_id.strip()} added", [
        ("pi_key", "Pi key", pi_key, PI_KEY_HELP),
        ("login", "Login password", password, LOGIN_HELP),
    ])


def _camera_action(request, viewer, camera_id, action):
    if (redirect := _guard(viewer)):
        return redirect
    try:
        with get_connection() as conn:
            return action(conn)
    except KeyError:
        return _page(request, error=f"Camera {camera_id} not found.", status_code=404)


@router.post("/admin/cameras/{camera_id}/login-password", response_class=HTMLResponse)
def admin_reset_login(request: Request, camera_id: str, viewer: Viewer = Depends(get_viewer)):
    def action(conn):
        password = reset_login_password(conn, camera_id)
        return _secret_page(request, f"New login password for {camera_id}", [("login", "Login password", password, LOGIN_HELP)],
                            note="Everyone signed in with the old password has been signed out.")
    return _camera_action(request, viewer, camera_id, action)


@router.post("/admin/cameras/{camera_id}/pi-key", response_class=HTMLResponse)
def admin_reset_pi_key(request: Request, camera_id: str, viewer: Viewer = Depends(get_viewer)):
    def action(conn):
        key = reset_pi_key(conn, camera_id)
        return _secret_page(request, f"New Pi key for {camera_id}", [("pi_key", "Pi key", key, PI_KEY_HELP)],
                            note="The Pi stops reporting until its camera_config.json has the new key.")
    return _camera_action(request, viewer, camera_id, action)


@router.post("/admin/cameras/{camera_id}/enabled")
def admin_set_enabled(request: Request, camera_id: str, enabled: str = Form("1"), viewer: Viewer = Depends(get_viewer)):
    def action(conn):
        set_camera_enabled(conn, camera_id, enabled == "1")
        return RedirectResponse("/admin", status_code=303)
    return _camera_action(request, viewer, camera_id, action)


# --- Test fires (admin only; never shown to the public or camera logins) -------------------------

def _number(value, label, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")


@router.post("/admin/test-fires/simulate", response_class=HTMLResponse)
def admin_simulate(request: Request, camera_id: str = Form(""), max_temperature_c: str = Form("350"),
                   hot_pixels: str = Form("12"), send_alerts: str = Form(""), viewer: Viewer = Depends(get_viewer)):
    if (redirect := _guard(viewer)):
        return redirect
    try:
        result = test_fires.simulate_detection(
            camera_id.strip(), _number(max_temperature_c, "Max temperature"),
            _number(hot_pixels, "Hot spot size", int), send_alerts=bool(send_alerts))
    except ValueError as exc:
        return _page(request, error=str(exc), status_code=400)
    sent = result["notifications"]
    alerts = (f"{sent.get('sms_sent', 0)} SMS and {sent.get('email_sent', 0)} email alerts sent with [TEST]."
              if sent else "No alerts sent.")
    link = f"/review/{result['event_id']}?token={result['token']}"
    return _page(request, message=f'Test detection created for {camera_id.strip()}. {alerts} '
                                  f'<a href="{link}">Open review page</a>')


@router.post("/admin/test-fires/drop", response_class=HTMLResponse)
def admin_drop(request: Request, latitude: str = Form(""), longitude: str = Form(""), severity: str = Form("Moderate"),
               viewer: Viewer = Depends(get_viewer)):
    if (redirect := _guard(viewer)):
        return redirect
    try:
        lat, lon = _number(latitude, "Latitude"), _number(longitude, "Longitude")
        test_fires.drop_test_fire(lat, lon, severity)
    except ValueError as exc:
        return _page(request, error=str(exc), status_code=400)
    return _page(request, message=f"Confirmed {severity} test fire placed at {lat:.5f}, {lon:.5f}. "
                                  f'It shows on <a href="/">your map</a> and in your route planning only.')


@router.post("/admin/test-fires/delete-all", response_class=HTMLResponse)
def admin_delete_all(request: Request, viewer: Viewer = Depends(get_viewer)):
    if (redirect := _guard(viewer)):
        return redirect
    count = test_fires.delete_test_fires()
    return _page(request, message=f"Deleted {count} test fire{'s' if count != 1 else ''}.")


@router.post("/admin/test-fires/{event_id}/delete", response_class=HTMLResponse)
def admin_delete_one(request: Request, event_id: str, viewer: Viewer = Depends(get_viewer)):
    if (redirect := _guard(viewer)):
        return redirect
    if not test_fires.delete_test_fires([event_id]):
        return _page(request, error="No test fire with that ID.", status_code=404)
    return _page(request, message="Test fire deleted.")
