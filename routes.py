import os
import pandas as pd
import requests
from shapely.geometry import Point, mapping
from shapely.ops import unary_union
from fastapi import Request
from map import app

ORS_API_KEY = os.getenv("ORS_API_KEY")

@app.post("/route")
async def calculate_route(request: Request):
    body = await request.json()
    start_text = body["start"]
    end_text = body["end"]

    def geocode(query):
        url = f"https://api.openrouteservice.org/geocode/search?api_key={ORS_API_KEY}&text={query}"
        r = requests.get(url).json()
        coords = r["features"][0]["geometry"]["coordinates"]
        return coords

    start = geocode(start_text)
    end = geocode(end_text)

    data_path = os.path.join(os.path.dirname(__file__), 'data', 'fires.csv')
    data = pd.read_csv(data_path)

    fire_polygons = []
    for _, row in data.iterrows():
        p = Point(row['longitude'], row['latitude'])
        danger = p.buffer(0.01)
        fire_polygons.append(danger)

    avoid_area = unary_union(fire_polygons)
    avoid_geojson = mapping(avoid_area)

    route_url = "https://api.openrouteservice.org/v2/directions/driving-car"
    payload = {
        "coordinates": [start, end],
        "options": {"avoid_polygons": avoid_geojson}
    }

    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json"
    }

    route = requests.post(route_url, json=payload, headers=headers).json()

    coords = route["features"][0]["geometry"]["coordinates"]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]

    return {"lat": lats, "lon": lons}
