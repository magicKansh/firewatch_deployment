from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import plotly.graph_objects as go
import plotly.io as pio
import pandas as pd

from fire_data import load_fires, get_last_updated

app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/images", StaticFiles(directory="images"), name="images")
templates = Jinja2Templates(directory="templates")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def firewatch(request: Request):
    data = load_fires()

    if not data.empty:
        data["mag"] = pd.to_numeric(data["mag"], errors="coerce")
        data["latitude"] = pd.to_numeric(data["latitude"], errors="coerce")
        data["longitude"] = pd.to_numeric(
            data["longitude"].astype(str).str.replace("−", "-", regex=False),
            errors="coerce",
        )
        data = data.dropna(subset=["latitude", "longitude", "mag"])
    else:
        data = pd.DataFrame()

    if data.empty:
        center_lat, center_lon = 39.5, -98.35
    else:
        center_lat = data["latitude"].mean()
        center_lon = data["longitude"].mean()

    fig = go.Figure()

    reported = data[data["source"] == "reported"] if not data.empty else pd.DataFrame()
    if not reported.empty:
        fig.add_trace(go.Scattermap(
            lat=reported["latitude"], lon=reported["longitude"],
            mode="markers",
            marker=dict(size=reported["mag"] * 7, color="rgba(255,69,0,0.25)"),
            hoverinfo="skip", showlegend=False
        ))
        fig.add_trace(go.Scattermap(
            lat=reported["latitude"], lon=reported["longitude"],
            mode="markers",
            marker=dict(size=9, color=reported["mag"], colorscale="reds",
                        cmin=1, cmax=5, symbol="circle"),
            text=reported["place"],
            hovertemplate="Reported: %{text}<extra></extra>",
            name="Reported fires"
        ))

    satellite = data[data["source"] == "satellite"] if not data.empty else pd.DataFrame()
    if not satellite.empty:
        fig.add_trace(go.Scattermap(
            lat=satellite["latitude"], lon=satellite["longitude"],
            mode="markers",
            marker=dict(size=10, color="#ffd166", symbol="triangle"),
            text=satellite["place"],
            hovertemplate="Satellite detection<extra></extra>",
            name="Satellite fires"
        ))

    device = data[data["source"] == "device"] if not data.empty else pd.DataFrame()
    if not device.empty:
        unconfirmed = device[device["status"] == "unconfirmed"]
        confirmed = device[device["status"] == "confirmed"]
        false_alarm = device[device["status"] == "false_alarm"]

        if not unconfirmed.empty:
            fig.add_trace(go.Scattermap(
                lat=unconfirmed["latitude"], lon=unconfirmed["longitude"],
                mode="markers",
                marker=dict(size=unconfirmed["mag"] * 5 + 10,
                            color="#ffcc00", symbol="circle"),
                text=[
                    f"UNCONFIRMED<br>Camera: {c}<br>Magnitude: {m}<br>"
                    f"Hot area: {a:.1f}%"
                    for c, m, a in zip(
                        unconfirmed["camera_id"],
                        unconfirmed["magnitude"],
                        unconfirmed["fire_size_percent"]
                    )
                ],
                hovertemplate="%{text}<extra></extra>",
                name="Unconfirmed camera detections"
            ))

        if not confirmed.empty:
            fig.add_trace(go.Scattermap(
                lat=confirmed["latitude"], lon=confirmed["longitude"],
                mode="markers",
                marker=dict(size=confirmed["mag"] * 5 + 10,
                            color="#e60000", symbol="circle"),
                text=[
                    f"CONFIRMED FIRE<br>Camera: {c}<br>Magnitude: {m}<br>"
                    f"Hot area: {a:.1f}%"
                    for c, m, a in zip(
                        confirmed["camera_id"],
                        confirmed["magnitude"],
                        confirmed["fire_size_percent"]
                    )
                ],
                hovertemplate="%{text}<extra></extra>",
                name="Confirmed camera fires"
            ))

        if not false_alarm.empty:
            fig.add_trace(go.Scattermap(
                lat=false_alarm["latitude"], lon=false_alarm["longitude"],
                mode="markers",
                marker=dict(size=10, color="#777777", symbol="circle"),
                text=["FALSE ALARM" for _ in range(len(false_alarm))],
                hovertemplate="%{text}<extra></extra>",
                name="False alarms"
            ))

    last_updated = get_last_updated()
    footer = f"Data as of {last_updated}" if last_updated else "Data loading..."

    fig.update_layout(
        map=dict(
            center=dict(lat=center_lat, lon=center_lon),
            zoom=6,
            style="open-street-map"
        ),
        autosize=True,
        margin=dict(l=0, r=0, t=0, b=0),
        legend=dict(
            bgcolor="rgba(0,0,0,0.65)",
            bordercolor="rgba(255,255,255,0.2)",
            borderwidth=1,
            font=dict(color="white", size=12),
            x=0.01, y=0.01
        ),
        annotations=[dict(
            text=footer,
            xref="paper", yref="paper",
            x=0.99, y=0.99,
            xanchor="right", yanchor="top",
            showarrow=False,
            font=dict(color="white", size=12),
            bgcolor="rgba(0,0,0,0.65)",
            bordercolor="rgba(255,255,255,0.2)",
            borderwidth=1, borderpad=6
        )]
    )

    graph_html = pio.to_html(fig, full_html=False, include_plotlyjs="cdn")
    return templates.TemplateResponse(
        request, "index.html", {"graph_html": graph_html}
    )


# Import after app exists because routes.py imports app.
import routes
import device_api
