
"""Firewatch enterprise dashboard, live geographic map, and map APIs."""
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
from device_api import PUBLIC_STATUSES
from fire_data import get_last_updated, get_satellite_status, load_fires

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

CAMERA_STYLE = {
    "online": ("#16a34a", "Online"),
    "stale": ("#d97706", "Stale"),
    "offline": ("#64748b", "Offline"),
    "disabled": ("#64748b", "Disabled"),
    "unknown": ("#64748b", "Unknown"),
}
FIRE_STYLE = {
    "unconfirmed": ("#ca8a04", "Needs review"),
    "confirmed": ("#dc2626", "Confirmed fires"),
    "false_alarm": ("#64748b", "False alarms"),
    "active": ("#ea580c", "Active events"),
    "resolved": ("#2563eb", "Resolved events"),
}


def _camera_rows():
    """Read fixed camera locations and derive a user-friendly connectivity status."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT camera_id, name, latitude, longitude, enabled, status,
                      last_seen_at, last_heartbeat_at, software_version
               FROM cameras ORDER BY camera_id"""
        ).fetchall()

    now = datetime.now(timezone.utc)
    cameras = []

    for row in rows:
        camera = dict(row)

        if not camera.get("enabled"):
            camera["display_status"] = "disabled"
        else:
            last_seen = camera.get("last_seen_at")

            if not last_seen:
                camera["display_status"] = "offline"
            else:
                try:
                    stamp = datetime.fromisoformat(last_seen)

                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)

                    age = max(0.0, (now - stamp).total_seconds())
                    camera["seconds_since_seen"] = round(age)

                    camera["display_status"] = (
                        "online"
                        if age <= 90
                        else "stale"
                        if age <= 300
                        else "offline"
                    )

                except (TypeError, ValueError):
                    camera["display_status"] = "unknown"

        try:
            camera["latitude"] = float(camera["latitude"])
            camera["longitude"] = float(camera["longitude"])
        except (ValueError, TypeError, KeyError):
            continue

        if (
            -90 <= camera["latitude"] <= 90
            and -180 <= camera["longitude"] <= 180
        ):
            cameras.append(camera)

    return cameras


def _numeric(data, column, default=0.0):
    if column not in data.columns:
        return pd.Series(default, index=data.index, dtype=float)

    return pd.to_numeric(
        data[column], errors="coerce"
    ).fillna(default)


def build_map_figure():
    """Build a Carto light basemap with reported, satellite, and camera fires."""
    raw = load_fires()

    if raw is None or raw.empty:
        data = pd.DataFrame(
            columns=[
                "latitude", "longitude", "mag",
                "place", "source", "status",
            ]
        )
    else:
        data = raw.copy()

        for column in ("latitude", "longitude", "mag"):
            if column in data.columns:
                data[column] = pd.to_numeric(
                    data[column].astype(str).str.replace(
                        "−", "-", regex=False
                    ),
                    errors="coerce",
                )

        if "mag" not in data.columns:
            data["mag"] = 1.0

        data["mag"] = (
            pd.to_numeric(data["mag"], errors="coerce")
            .fillna(1.0)
            .clip(0.5, 5.0)
        )

        if "source" not in data.columns:
            data["source"] = "reported"

        if "place" not in data.columns:
            data["place"] = "Fire detection"

        if "status" not in data.columns:
            data["status"] = "unconfirmed"

        data = data.dropna(subset=["latitude", "longitude"])

        data = data[
            data["latitude"].between(-90, 90)
            & data["longitude"].between(-180, 180)
        ]

    cameras = _camera_rows()

    camera_points = [
        (camera["latitude"], camera["longitude"])
        for camera in cameras
    ]

    fire_points = (
        list(zip(data["latitude"], data["longitude"]))
        if not data.empty
        else []
    )

    points = camera_points or fire_points

    if points:
        center_lat = sum(point[0] for point in points) / len(points)
        center_lon = sum(point[1] for point in points) / len(points)

        zoom = (
            10
            if camera_points and len(camera_points) <= 3
            else 6
        )
    else:
        # Show a geographic map even before registering a camera.
        center_lat, center_lon, zoom = 39.5, -98.35, 3.3

    fig = go.Figure()

    # Ensure an empty fire dataset still renders a geographic map.
    fig.add_trace(
        go.Scattermap(
            lat=[center_lat],
            lon=[center_lon],
            mode="markers",
            marker={"size": 1, "opacity": 0},
            hoverinfo="skip",
            showlegend=False,
            name="Map initializer",
        )
    )

    if not data.empty:
        sources = data["source"].fillna("reported")

        # Reported fire locations.
        reported = data[sources == "reported"]

        if not reported.empty:
            fig.add_trace(
                go.Scattermap(
                    lat=reported["latitude"],
                    lon=reported["longitude"],
                    mode="markers",
                    marker={
                        "size": _numeric(reported, "mag", 1) * 3 + 8,
                        "color": "#f0440a",
                        "opacity": 0.9,
                    },
                    text=reported["place"].fillna("Reported fire"),
                    hovertemplate=(
                        "<b>Reported fire</b><br>%{text}"
                        "<extra></extra>"
                    ),
                    name="Reported fires",
                )
            )

        # NASA FIRMS satellite detections.
        satellite = data[sources == "satellite"]

        if not satellite.empty:
            fig.add_trace(
                go.Scattermap(
                    lat=satellite["latitude"],
                    lon=satellite["longitude"],
                    mode="markers",
                    marker={
                        "size": 11,
                        "color": "#d97706",
                        "symbol": "triangle",
                    },
                    text=satellite["place"].fillna(
                        "NASA FIRMS detection"
                    ),
                    hovertemplate=(
                        "<b>Satellite detection</b><br>%{text}"
                        "<extra></extra>"
                    ),
                    name="Satellite fires",
                )
            )

        # Raspberry Pi fire events. Only ones a reviewer confirmed are public;
        # unreviewed detections and false alarms stay off the community map.
        device = data[sources == "device"]
        if not device.empty and "status" in device.columns:
            device = device[device["status"].isin(PUBLIC_STATUSES)]

        if not device.empty:
            device_status = device.get(
                "status",
                pd.Series("unconfirmed", index=device.index),
            ).fillna("unconfirmed")

            for status, (color, label) in FIRE_STYLE.items():
                part = device[device_status == status]

                if part.empty:
                    continue

                camera_ids = part.get(
                    "camera_id",
                    pd.Series("Camera", index=part.index),
                ).fillna("Camera")

                levels = part.get(
                    "magnitude",
                    pd.Series("Unknown", index=part.index),
                ).fillna("Unknown")

                areas = _numeric(part, "fire_size_percent")
                temperatures = _numeric(part, "max_temperature_c")

                hover_text = [
                    (
                        f"<b>{label}</b><br>"
                        f"Camera: {camera_id}<br>"
                        f"Severity: {level}<br>"
                        f"Hot thermal pixels: {area:.1f}%<br>"
                        f"Maximum temperature: {temp:.1f} C"
                    )
                    for camera_id, level, area, temp in zip(
                        camera_ids, levels, areas, temperatures
                    )
                ]

                fig.add_trace(
                    go.Scattermap(
                        lat=part["latitude"],
                        lon=part["longitude"],
                        mode="markers",
                        marker={
                            "size": _numeric(part, "mag", 1) * 3 + 9,
                            "color": color,
                            "opacity": 0.95,
                        },
                        text=hover_text,
                        hovertemplate="%{text}<extra></extra>",
                        name=label,
                    )
                )

    # Draw fixed-location camera markers above the fire layers.
    for status, (color, label) in CAMERA_STYLE.items():
        subset = [
            camera
            for camera in cameras
            if camera["display_status"] == status
        ]

        if not subset:
            continue

        hover_text = [
            (
                f"<b>Camera {camera['camera_id']}</b><br>"
                f"{camera.get('name') or 'Fixed-location camera'}<br>"
                f"Status: {label}<br>"
                f"Coordinates: {camera['latitude']:.5f}, "
                f"{camera['longitude']:.5f}"
            )
            for camera in subset
        ]

        fig.add_trace(
            go.Scattermap(
                lat=[camera["latitude"] for camera in subset],
                lon=[camera["longitude"] for camera in subset],
                mode="markers",
                marker={
                    "size": 12,
                    "color": color,
                    "opacity": 0.97,
                },
                text=hover_text,
                hovertemplate="%{text}<extra></extra>",
                name=f"Cameras: {label}",
            )
        )

    updated = get_last_updated()

    map_note = (
        f"Fire data updated {updated}"
        if updated
        else "Firewatch map · awaiting detections"
    )

    if not cameras:
        map_note += " · No registered cameras"

    fig.update_layout(
        map={
            "center": {"lat": center_lat, "lon": center_lon},
            "zoom": zoom,
            "style": "carto-positron",
        },
        autosize=True,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        uirevision="firewatch-enterprise-map",
        paper_bgcolor="#d8dedc",
        plot_bgcolor="#d8dedc",
        font={
            "family": "Inter, Arial, sans-serif",
            "color": "#334155",
            "size": 11,
        },
        legend={
            "bgcolor": "rgba(24,25,24,0.84)",
            "bordercolor": "rgba(255,255,255,0.2)",
            "borderwidth": 1,
            "font": {"color": "#ffffff", "size": 10},
            "x": 0.012,
            "y": 0.015,
            "xanchor": "left",
            "yanchor": "bottom",
        },
        annotations=[
            {
                "text": map_note,
                "xref": "paper",
                "yref": "paper",
                "x": 0.99,
                "y": 0.99,
                "xanchor": "right",
                "yanchor": "top",
                "showarrow": False,
                "font": {"color": "#ffffff", "size": 10},
                "bgcolor": "rgba(24,25,24,0.82)",
                "bordercolor": "rgba(255,255,255,0.2)",
                "borderwidth": 1,
                "borderpad": 6,
            }
        ],
    )

    return fig, cameras, data


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/map-figure")
def map_figure():
    fig, _, _ = build_map_figure()

    return JSONResponse(
        content=json.loads(pio.to_json(fig, validate=True))
    )


@app.get("/api/satellite-status")
def satellite_status():
    try:
        return JSONResponse(content=get_satellite_status())
    except Exception:
        return JSONResponse(
            content={
                "configured": False,
                "count": 0,
                "updated_at": get_last_updated(),
                "error": "Satellite status is temporarily unavailable.",
            }
        )


@app.get("/", response_class=HTMLResponse)
def firewatch(request: Request):
    fig, cameras, data = build_map_figure()

    graph_html = pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs="cdn",
        config={
            "responsive": True,
            "scrollZoom": True,
            "displaylogo": False,
        },
    )

    source = data.get("source", pd.Series(dtype=str))
    status = data.get(
        "status",
        pd.Series("unconfirmed", index=data.index),
    )

    device = source == "device"

    stats = {
        "cameras": sum(
            bool(camera.get("enabled")) for camera in cameras
        ),
        "online": sum(
            camera.get("display_status") == "online"
            for camera in cameras
        ),
        "unconfirmed": (
            int((device & (status == "unconfirmed")).sum())
            if not data.empty
            else 0
        ),
        "confirmed": (
            int((device & (status == "confirmed")).sum())
            if not data.empty
            else 0
        ),
    }

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "graph_html": graph_html,
            "stats": stats,
            "cameras": cameras,
        },
    )


# Register route-planning endpoints on the same FastAPI application.
import routes  # noqa: E402,F401
