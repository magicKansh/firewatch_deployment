from urllib import request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
import plotly.express as px
import plotly.io as pio
import os

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def firewatch(request: Request):
    data_path = os.path.join(os.path.dirname(__file__), 'data', 'fires.csv')
    data = pd.read_csv(data_path)

    data['longitude'] = data['longitude'].astype(str).str.replace('−', '-').astype(float)
    data = data.dropna(subset=['mag'])
    data = data[data.mag >= 0]


    center_lat = data['latitude'].mean()
    center_lon = data['longitude'].mean()


    fig = px.scatter_map(
        data,
        lat='latitude',
        lon='longitude',
        size='mag',
        size_max=35,
        color_discrete_sequence=['rgba(255, 69, 0, 0.25)'],
        center=dict(lat=center_lat, lon=center_lon),
        zoom=6,
        height=None
    )

    fig.update_traces(hoverinfo='skip', hovertemplate=None)


    core_dots = px.scatter_map(
        data,
        lat='latitude',
        lon='longitude',
        color='mag',
        hover_name='place',
        color_continuous_scale='reds',
        range_color=[1, 5],
        size_max=10
    )

    for trace in core_dots.data:
        fig.add_trace(trace)


    fig.update_layout(
        autosize=True,
        margin=dict(l=0, r=0, t=0, b=0),  
        coloraxis=core_dots.layout.coloraxis,
        coloraxis_colorbar=dict(
            title="<b>Magnitude</b><br>",
            title_font=dict(color="white", size=14),
            tickfont=dict(color="white", size=12),
            len=0.4,                   
            thickness=15,              
            x=0.93,                     
            y=0.05,                     
            xanchor="right",
            yanchor="bottom",
            bgcolor="rgba(0, 0, 0, 0.6)", 
            outlinecolor="rgba(255, 255, 255, 0.2)",
            outlinewidth=1,
            title_side="top",
            ticklen=8,
            tickcolor="rgba(255, 255, 255, 0.5)"
        )
    )

    graph_html = pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs='cdn',
    )
    return templates.TemplateResponse(
       "index.html", 
       {"request": request, "graph_html": graph_html}
    )