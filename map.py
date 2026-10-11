"""Firewatch map and web entry point."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import app
from database import get_connection
from fire_data import get_last_updated, get_satellite_status, load_fires

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

STATUS_STYLE = {
    "online": ("#35c759", "Online"),
    "stale": ("#ff9f0a", "Stale"),
    "offline": ("#a1a1aa", "Offline"),
    "disabled": ("#52525b", "Disabled"),
    "unknown": ("#a1a1aa", "Unknown"),
}
FIRE_STYLE = {
    "unconfirmed": ("#facc15", "Unconfirmed detections"),
    "confirmed": ("#ef4444", "Confirmed fires"),
    "false_alarm": ("#71717a", "False alarms"),
    "active": ("#f97316", "Active events"),
    "resolved": ("#60a5fa", "Resolved events"),
}


def _camera_rows():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT camera_id,name,latitude,longitude,enabled,status,last_seen_at,last_heartbeat_at,software_version "
            "FROM cameras ORDER BY camera_id"
        ).fetchall()
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        item = dict(row)
        if not item["enabled"]:
            item["display_status"] = "disabled"
        elif not item.get("last_seen_at"):
            item["display_status"] = "offline"
        else:
            try:
                stamp = datetime.fromisoformat(item["last_seen_at"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age = (now - stamp).total_seconds()
                item["display_status"] = "online" if age <= 90 else "stale" if age <= 300 else "offline"
                item["seconds_since_seen"] = max(0, round(age))
            except (TypeError, ValueError):
                item["display_status"] = "unknown"
        result.append(item)
    return result


def build_map_figure():
    raw = load_fires()
    if raw is None or raw.empty:
        data = pd.DataFrame(columns=["latitude", "longitude", "mag", "place", "source", "status"])
    else:
        data = raw.copy()
        for column in ("latitude", "longitude", "mag"):
            if column in data:
                data[column] = pd.to_numeric(
                    data[column].astype(str).str.replace("−", "-", regex=False), errors="coerce"
                )
        data = data.dropna(subset=["latitude", "longitude"])
        data = data[data["latitude"].between(-90, 90) & data["longitude"].between(-180, 180)]
        if "mag" not in data:
            data["mag"] = 1.0
        data["mag"] = pd.to_numeric(data["mag"], errors="coerce").fillna(1.0).clip(0.5, 5.0)

    cameras = _camera_rows()
    fire_points = data[["latitude", "longitude"]].values.tolist() if not data.empty else []
    camera_points = [[float(c["latitude"]), float(c["longitude"])] for c in cameras]
    # Prefer the registered camera deployment footprint when no fire has been reported.
    points = camera_points or fire_points
    if points:
        center_lat = sum(p[0] for p in points) / len(points)
        center_lon = sum(p[1] for p in points) / len(points)
        zoom = 10 if camera_points and len(camera_points) <= 3 else 6
    else:
        # Default overview until the operator registers the first fixed-location camera.
        center_lat, center_lon, zoom = 39.5, -98.35, 3.3

    fig = go.Figure()

    # Critical: create the map subplot even before the first fire/camera is registered.
    # Without a Scattermap trace Plotly renders a blank Cartesian chart with numeric axes.
    fig.add_trace(go.Scattermap(
        lat=[center_lat], lon=[center_lon], mode="markers",
        marker=dict(size=1, opacity=0), hoverinfo="skip", showlegend=False,
        name="Map initializer",
    ))

    if not data.empty:
        reported = data[data.get("source", pd.Series(index=data.index, dtype=str)) == "reported"]
        if not reported.empty:
            fig.add_trace(go.Scattermap(
                lat=reported["latitude"], lon=reported["longitude"], mode="markers",
                marker=dict(size=(reported["mag"] * 3 + 7), color="#fb923c", opacity=0.9),
                text=reported.get("place", "Reported fire"),
                hovertemplate="Reported fire: %{text}<extra></extra>", name="Reported fires",
            ))
        satellite = data[data.get("source", pd.Series(index=data.index, dtype=str)) == "satellite"]
        if not satellite.empty:
            fig.add_trace(go.Scattermap(
                lat=satellite["latitude"], lon=satellite["longitude"], mode="markers",
                marker=dict(size=11, color="#fbbf24", symbol="triangle"),
                text=satellite.get("place", "Satellite detection"),
                hovertemplate="Satellite detection: %{text}<extra></extra>", name="Satellite fires",
            ))
        device = data[data.get("source", pd.Series(index=data.index, dtype=str)) == "device"]
        for status, (color, label) in FIRE_STYLE.items():
            part = device[device.get("status", pd.Series(index=device.index, dtype=str)).fillna("unconfirmed") == status]
            if part.empty:
                continue
            camera_ids = part.get("camera_id", pd.Series(["Camera"] * len(part), index=part.index)).fillna("Camera")
            magnitudes = part.get("magnitude", pd.Series(["Unknown"] * len(part), index=part.index)).fillna("Unknown")
            areas = pd.to_numeric(part.get("fire_size_percent", 0), errors="coerce")
            temps = pd.to_numeric(part.get("max_temperature_c", 0), errors="coerce")
            texts = [
                f"{status.replace('_', ' ').upper()}<br>Camera: {cid}<br>Level: {mag}<br>Hot thermal pixels: {float(area):.1f}%<br>Max temperature: {float(temp):.1f} °C"
                for cid, mag, area, temp in zip(camera_ids, magnitudes, areas, temps)
            ]
            fig.add_trace(go.Scattermap(
                lat=part["latitude"], lon=part["longitude"], mode="markers",
                marker=dict(size=(part["mag"] * 3 + 10), color=color, opacity=0.95),
                text=texts, hovertemplate="%{text}<extra></extra>", name=label,
            ))

    if cameras:
        for status in ("online", "stale", "offline", "disabled", "unknown"):
            part = [c for c in cameras if c["display_status"] == status]
            if not part:
                continue
            color, label = STATUS_STYLE[status]
            texts = [
                f"CAMERA {c['camera_id']}<br>{c.get('name') or 'Fixed-location camera'}<br>State: {label}<br>Coordinates: {float(c['latitude']):.5f}, {float(c['longitude']):.5f}"
                for c in part
            ]
            fig.add_trace(go.Scattermap(
                lat=[float(c["latitude"]) for c in part],
                lon=[float(c["longitude"]) for c in part], mode="markers",
                marker=dict(size=12, color=color, symbol="circle", opacity=0.95),
                text=texts, hovertemplate="%{text}<extra></extra>", name=f"Cameras · {label}",
            ))

    last_updated = get_last_updated()
    footer = f"Data refreshed {last_updated}" if last_updated else "Live Firewatch map · awaiting detections"
    if not cameras:
        footer += " · Register a camera to center the map on your site"
    fig.update_layout(
        map=dict(center=dict(lat=center_lat, lon=center_lon), zoom=zoom, style="open-street-map"),
        autosize=True, margin=dict(l=0, r=0, t=0, b=0), uirevision="firewatch-live-map",
        paper_bgcolor="#0b1120", plot_bgcolor="#0b1120",
        font=dict(color="#e5e7eb", family="Inter, Arial, sans-serif"),
        legend=dict(bgcolor="rgba(10,16,30,0.88)", bordercolor="rgba(255,255,255,0.13)",
                    borderwidth=1, font=dict(color="#e5e7eb", size=11), x=0.015, y=0.02,
                    xanchor="left", yanchor="bottom"),
        annotations=[dict(text=footer, xref="paper", yref="paper", x=.99, y=.99,
                          xanchor="right", yanchor="top", showarrow=False,
                          font=dict(color="#e5e7eb", size=11), bgcolor="rgba(10,16,30,.85)",
                          bordercolor="rgba(255,255,255,.15)", borderwidth=1, borderpad=6)],
    )
    return fig, cameras, data


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/map-figure")
def map_figure():
    fig, _, _ = build_map_figure()
    return JSONResponse(content=json.loads(pio.to_json(fig, validate=True)))


@app.get("/api/satellite-status")
def satellite_status():
    """Expose whether NASA FIRMS is configured and whether it returned detections."""
    return JSONResponse(content=get_satellite_status())


@app.get("/", response_class=HTMLResponse)
def firewatch(request: Request):
    fig, cameras, data = build_map_figure()
    graph_html = pio.to_html(
        fig, full_html=False, include_plotlyjs="cdn",
        config={"responsive": True, "scrollZoom": True, "displaylogo": False},
    )
    stats = {
        "cameras": len(cameras),
        "online": sum(c["display_status"] == "online" for c in cameras),
        "unconfirmed": int(((data["source"] == "device") & (data.get("status", "") == "unconfirmed")).sum()) if not data.empty else 0,
        "confirmed": int(((data["source"] == "device") & (data.get("status", "") == "confirmed")).sum()) if not data.empty else 0,
    }
    return templates.TemplateResponse(request, "index.html", {
        "graph_html": graph_html, "stats": stats, "cameras": cameras,
    })


# Register route planner endpoints after the shared FastAPI app is constructed.
import routes  # noqa: E402,F401
