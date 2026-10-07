from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from app import app, limiter
from fire_data import load_fires, get_last_updated

BASE_DIR = __import__('pathlib').Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def firewatch(request: Request):
    data = load_fires()
    if data.empty:
        center_lat, center_lon = 39.5, -98.35
    else:
        data["mag"] = pd.to_numeric(data["mag"], errors="coerce")
        data["longitude"] = data["longitude"].astype(str).str.replace("−", "-").astype(float)
        data = data.dropna(subset=["latitude", "longitude"])
        center_lat, center_lon = data["latitude"].mean(), data["longitude"].mean()

    fig = go.Figure()
    reported = data[data["source"] == "reported"] if not data.empty else pd.DataFrame()
    if not reported.empty:
        fig.add_trace(go.Scattermap(lat=reported["latitude"], lon=reported["longitude"], mode="markers",
                                    marker=dict(size=12, color="rgba(255,69,0,0.35)"), name="Reported fires",
                                    text=reported["place"], hovertemplate="Reported: %{text}<extra></extra>"))
    satellite = data[data["source"] == "satellite"] if not data.empty else pd.DataFrame()
    if not satellite.empty:
        fig.add_trace(go.Scattermap(lat=satellite["latitude"], lon=satellite["longitude"], mode="markers",
                                    marker=dict(size=10, color="#ffd166", symbol="triangle"), name="Satellite fires",
                                    text=satellite["place"], hovertemplate="Satellite detection<extra></extra>"))

    device = data[data["source"] == "device"] if not data.empty else pd.DataFrame()
    status_styles = {
        "unconfirmed": ("#ffcc00", "Unconfirmed camera detections"),
        "confirmed": ("#e60000", "Confirmed camera fires"),
        "false_alarm": ("#777777", "False alarms"),
        "active": ("#ff6600", "Active camera fires"),
        "resolved": ("#66aaff", "Resolved camera events"),
    }
    for status, (color, label) in status_styles.items():
        part = device[device["status"] == status] if not device.empty else pd.DataFrame()
        if part.empty:
            continue
        texts = [
            f"{status.upper()}<br>Camera: {c}<br>Magnitude: {m}<br>Hot area: {a:.1f}%<br>Max: {t:.1f}°C"
            for c, m, a, t in zip(part["camera_id"], part["magnitude"], part["fire_size_percent"], part["max_temperature_c"])
        ]
        fig.add_trace(go.Scattermap(lat=part["latitude"], lon=part["longitude"], mode="markers",
                                    marker=dict(size=part["mag"] * 5 + 10, color=color), text=texts,
                                    hovertemplate="%{text}<extra></extra>", name=label))

    last_updated = get_last_updated()
    footer = f"Data as of {last_updated}" if last_updated else "Data loading..."
    fig.update_layout(map=dict(center=dict(lat=center_lat, lon=center_lon), zoom=6, style="open-street-map"),
                      autosize=True, margin=dict(l=0,r=0,t=0,b=0),
                      legend=dict(bgcolor="rgba(0,0,0,0.65)", font=dict(color="white"), x=0.01, y=0.01),
                      annotations=[dict(text=footer, xref="paper", yref="paper", x=.99, y=.99,
                                        xanchor="right", yanchor="top", showarrow=False,
                                        font=dict(color="white", size=12), bgcolor="rgba(0,0,0,.65)")])
    graph_html = pio.to_html(fig, full_html=False, include_plotlyjs="cdn")
    return templates.TemplateResponse(request, "index.html", {"graph_html": graph_html})


# Keep the original `uvicorn map:app` command working.
import routes  # noqa: E402,F401
