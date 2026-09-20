import os
import pandas as pd
import requests
from shapely.geometry import Point, mapping
from shapely.ops import unary_union
from fastapi import Request
from map import app

ORS_API_KEY = os.getenv("ORS_API_KEY")

PROFILES = {
    "driving-car": "driving-car",
    "cycling-regular": "cycling-regular",
    "foot-walking": "foot-walking",
}

def get_avoid_geojson():
    data_path = os.path.join(os.path.dirname(__file__), 'data', 'fires.csv')
    data = pd.read_csv(data_path)  # FIXED: was "pd.read.csv" (dot instead of underscore) -> AttributeError

    fire_polygons = []
    for _, row in data.iterrows():
        p = Point(row['longitude'], row['latitude'])
        fire_polygons.append(p.buffer(0.01))

    avoid_area = unary_union(fire_polygons)
    return mapping(avoid_area)

def call_ors_route(start, end, profile):  # FIXED: was "call_ors__route" (double underscore) -> didn't match the calls below, would raise NameError
    route_url = f"https://api.openrouteservice.org/v2/directions/{profile}/geojson"
    payload = {
        "coordinates": [start, end],
        "options": {"avoid_polygons": get_avoid_geojson()}
    }
    headers = {"Authorization": ORS_API_KEY, "Content-Type": "application/json"}
    return requests.post(route_url, json=payload, headers=headers, timeout=15).json()


@app.get("/estimates")
async def estimates(start: str, end: str):
    def geocode(query):
        url = f"https://api.openrouteservice.org/geocode/search?api_key={ORS_API_KEY}&text={query}"
        r = requests.get(url, timeout=10).json()
        if not r.get("features"):
            return None
        return r["features"][0]["geometry"]["coordinates"]

    start_coords = geocode(start)
    end_coords = geocode(end)

    if not start_coords or not end_coords:
        return {"error": "Could not find one of those locations."}

    results = {}
    for key, profile in PROFILES.items():
        try:
            route = call_ors_route(start_coords, end_coords, profile)
            if "features" not in route:
                results[key] = {"error": route.get("error", {}).get("message", "Unavailable")}
                continue
            summary = route["features"][0]["properties"]["summary"]
            results[key] = {
                "duration": summary.get("duration"),  # FIXED: was "summary['distance'] and summary.get('duration')" — that weird "and" was pointless/confusing, just return duration directly
                "distance": summary.get("distance")
            }
        except Exception as e:
            results[key] = {"error": str(e)}

    return results  # FIXED: this was indented inside the for-loop, so it returned after only the FIRST profile instead of after checking all three


@app.post("/route")
async def calculate_route(request: Request):
    body = await request.json()
    start_text = body["start"]
    end_text = body["end"]
    mode = body.get("mode", "driving-car")
    profile = PROFILES.get(mode, "driving-car")

    def geocode(query):
        url = f"https://api.openrouteservice.org/geocode/search?api_key={ORS_API_KEY}&text={query}"
        r = requests.get(url, timeout=10).json()  # added timeout for consistency with /estimates
        if not r.get("features"):
            raise ValueError(f"Could not geocode '{query}'")
        return r["features"][0]["geometry"]["coordinates"]  # FIXED: was "r['feature']" (missing 's') -> KeyError

    try:
        start = geocode(start_text)
        end = geocode(end_text)
        route = call_ors_route(start, end, profile)
    except Exception as e:
        return {"error": str(e), "lat": [], "lon": []}

    if "features" not in route:
        return {"error": route.get("error", {}).get("message", "Routing failed"), "lat": [], "lon": []}

    coords = route["features"][0]["geometry"]["coordinates"]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]

    return {"lat": lats, "lon": lons}


@app.get("/autocomplete")
async def autocomplete(query: str):
    if not query or len(query) < 3:
        return {"suggestions": []}

    def search(layers=None):
        url = f"https://api.openrouteservice.org/geocode/autocomplete?api_key={ORS_API_KEY}&text={query}&size=5"
        if layers:
            url += f"&layers={layers}"
        return requests.get(url, timeout=10).json()

    r = search(layers="venue,address,street,locality,neighbourhood")

    if not r.get("features"):
        r = search()

    suggestions = [
        {
            "label": f["properties"]["label"],
            "lon": f["geometry"]["coordinates"][0],
            "lat": f["geometry"]["coordinates"][1]
        }
        for f in r.get("features", [])
    ]
    return {"suggestions": suggestions}
