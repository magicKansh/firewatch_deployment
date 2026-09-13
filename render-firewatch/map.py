from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import pandas as pd
import plotly.express as px
import plotly.io as pio

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def firewatch():
    data = pd.read_csv('data/fires.csv')

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
    custom_css = """
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        html, body {
            width: 100vw;
            height: 100vh;
            overflow: hidden;
            background-color: #000;
        }
        .plotly-graph-div {
            width: 100vw !important;
            height: 100vh !important;
        }
    </style>
    """
    home_screen_html ="""
    <div id="home-overlay">
        <div id="home-content">
            <h1 id = "app-title"> Firewatch </h1>
            <p id="app-subtitle">A tool for locating and routing around fires.</p>
            <button id="view-map-button" onclick="
                document.getElementById('home-overlay').style.display = 'none';
                document.getElementById('gps-panel').style.display = 'block';
                document.querySelectorAll('g.colorbar').forEach(cb => cb.style.display = 'block');
            ">
                Start Exploring
            </button>
        </div>
    </div>
    <div id="gps-panel">
        <h3>Navigation</h3>
        <label for ="start">Your Location:</label>
        <input type="text" id="start" placeholder="Enter current location">

        <label for="end">Destination:</label>
        <input type="text" id="end" placeholder="Enter destination">
        <button id="gps-submit">Get Route</button>
    </div>
    <div id="colorbar-container"></div>

    <style>
        @import url('https://fonts.googleapis.com/css2?family=Merriweather:wght@400;700&display=swap');
        
        #home-overlay {
            position: fixed;
            inset: 0;
            background-color: rgba(0, 0, 0, 0.55);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 9999;
        }
        #home-content {
            text-align: center;
            color: white;
        }
        #gps-panel{
            position: fixed;
            top: 20px;
            right: 20px;
            background-color: rgba(0, 0, 0, 0.55);
            padding: 15px 18px;
            border-radius: 10px;
            color: white;
            font-family: 'Open Sans', sans-serif;
            width: 240px;
            display: none;
            z-index: 9998;
        }
        #gps-panel h3 {
            margin-bottom: 10px;
            font-size: 1.2rem;
            font-family: 'Merriweather', serif;
        }
        #gps-panel label {
            display: block;
            margin-top: 8px;
            font-size: 0.9rem;
        }
        #gps-panel input {
            width: 100%;
            padding: 6px 8px;
            margin-top: 4px;
            border-radius: 6px;
            border: none;
            outline: none;
            font-size: 0.9rem;
        }
        #gps-submit {
            width: 100%;
            padding: 10px;
            margin-top: 12px;
            background-color: #ff4500;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1rem;
            transition: background-color 0.3s ease;
        }
        #gps-submit:hover {
            background-color: #ff6347;
        }
        #app-title {
            font-family: 'Merriweather', serif;
            font-size: 3rem;
            margin-bottom: 10px;
            font-weight: 700;
            letter-spacing: 2px;
        }
        #app-subtitle {
            font-family: 'Open Sans', sans-serif;
            font-size: 1.1rem;
            margin-bottom: 25px;
            font-weight: 400;
        }
        #view-map-button {
            font-family: 'Open Sans', sans-serif;
            padding: 12px 28px;
            font-size: 1.2rem;
            cursor: pointer;
            background-color: #ff4500;
            border: none;
            border-radius: 8px;
            color: white;
            transition: background-color 0.3s ease;
        }
        #view-map-button:hover {
            background-color: #ff6347;
            transform: scale(1.05);
        }
        g.colorbar {
            display: none;
        }
        g.colorbar rect {
        rx: 12px; /* rounded corners */
        ry: 12px;
        }

        g.colorbar {
            transform: translateY(-20px); /* move it slightly higher */
        }

        #colorbar-container {
        position: fixed;
        bottom: 90px; 
        right: 20px;
        display: none; 
        z-index: 9998;
        border-radius: 12px; 
        overflow: hidden; 
        }

        #plotly-colorbar {
        border-radius: 12px;
        box-shadow: 0 0 10px rgba(0,0,0,0.4);
        }

    </style>
    """

    final_html = f"""
    <html>
    <head>
        {custom_css}
    </head>
    <body>
        {home_screen_html}
        {graph_html}
    </body>
    </html>
    """
    return final_html