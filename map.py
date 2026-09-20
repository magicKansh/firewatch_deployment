from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import os
from fire_data import load_fires

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def firewatch(request: Request):
    data = load_fires()

    data['mag'] = pd.to_numeric(data['mag'], errors='coerce')
    data['longitude'] = data['longitude'].astype(str).str.replace('−', '-').astype(float)
    data = data.dropna(subset=['mag'])
    data = data[data.mag >= 0]

    center_lat = data['latitude'].mean()
    center_lon = data['longitude'].mean()

    fig = go.Figure()

    reported = data[data['source'] == 'reported']
    if not reported.empty:
        fig.add_trace(go.Scattermap(
            lat=reported['latitude'], lon=reported['longitude'],
            mode = 'markers',
            marker=dict(size=reported['mag'] * 7, color='rgba(255, 69, 0, 0.25)'),
            hoverinfo='skip', showlegend=False
        ))
        fig.add_trace(go.Scattermap(
            lat=reported['latitude'], lon=reported['longitude'],
            mode='markers',
            marker=dict(size=9, color = reported['mag'], colorscale = 'reds', cmin=1, cmax=5, symbol='circle'),
            text=reported['place'],
            hovertemplate='Reported: %{text}<extra></extra>',
            name='Reported fires',
            showlegend = True
        ))
    satellite = data[data['source'] == 'satellite']
    if not satellite.empty:
        fig.add_trace(go.Scattermap(
            lat=satellite['latitude'], lon=satellite['longitude'],
            mode='markers',
            marker=dict(size=10, color = '#ffd166', symbol = 'triangle'),
            text=satellite['place'],
            hovertemplate='Satellite detection<extra></extra>',
            name='Satellite fires',
            showlegend = True


        ))

    fig.update_layout(
        map=dict(
            center=dict(lat=center_lat, lon=center_lon),
            zoom=6,
            style="open-street-map"
        ),
        autosize=True,
        margin=dict(l=0, r=0, t=0, b=0),
        legend=dict(
            bgcolor="rgba(0,0,0,0.6)",
            bordercolor="rgba(255,255,255,0.2)",
            borderwidth=1,
            font=dict(color="white", size=12),
            x=0.01,
            y=0.01,
            xanchor="left",
            yanchor="bottom"
        )
    )
    graph_html = pio.to_html(fig, full_html=False, include_plotlyjs='cdn')
    return templates.TemplateResponse(request, "index.html", {"graph_html": graph_html})

import routes
