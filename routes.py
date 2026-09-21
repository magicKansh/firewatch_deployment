import os
import requests
from fastapi import Request
from map import app, limiter
from fire_data import load_fires, get_avoid_geojson

ORS_API_KEY = os.getenv("ORS_API_KEY")

PROFILES = {
    "driving-car": "driving-car",
    "cycling-regular": "cycling-regular",
    "foot-walking": "foot-walking",
}

MAX_QUERY_LENGTH = 200

def call_ors_route(start, end, profile):
    route_url = f"https://api.openrouteservice.org/v2/directions/{profile}/geojson"
    payload = {
        "coordinates": [start, end],
        "options": {"avoid_polygons": get_avoid_geojson()},
        "instructions": True
    }
    headers = {"Authorization": ORS_API_KEY, "Content-Type": "application/json"}
    return requests.post(route_url, json=payload, headers=headers, timeout=15).json()

def geocode(query):
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError("Location text is too long")
    url = f"https://api.openrouteservice.org/geocode/search?api_key={ORS_API_KEY}&text={query}"
    r = requests.get(url, timeout=10).json()
    if not r.get("features"):
        raise ValueError(f"Could not find '{query}'")
    return r["features"][0]["geometry"]["coordinates"]

@app.post("/routes")
@limiter.limit("10/minute")
async def calculate_routes(request: Request):
    body = await request.json()
    start_text = body["start"]
    end_text = body["end"]

    if len(start_text) > MAX_QUERY_LENGTH or len(end_text) > MAX_QUERY_LENGTH:
        return {"error": "Location text is too long."}

    try:
        start = geocode(start_text)
        end = geocode(end_text)
    except Exception as e:
        return {"error": str(e)}

    results = {}
    for key, profile in PROFILES.items():
        try:
            route = call_ors_route(start, end, profile)
            if "features" not in route:
                results[key] = {"error": route.get("error", {}).get("message", "Unavailable")}
                continue
            feature = route["features"][0]
            coords = feature["geometry"]["coordinates"]
            summary = feature["properties"]["summary"]

            segments = feature["properties"].get("segments", [])
            steps = []
            if segments:
                for step in segments[0].get("steps", []):
                    steps.append({
                        "instruction": step.get("instruction", ""),
                        "distance": step.get("distance", 0),
                        "duration": step.get("duration", 0),
                    })

            results[key] = {
                "lat": [c[1] for c in coords],
                "lon": [c[0] for c in coords],
                "duration": summary.get("duration"),
                "distance": summary.get("distance"),
                "steps": steps,
                "start": {"lat": start[1], "lon": start[0]},
                "end": {"lat": end[1], "lon": end[0]},
            }
        except Exception as e:
            results[key] = {"error": str(e)}

    return results

@app.get("/reverse")
@limiter.limit("15/minute")
async def reverse_geocode(request: Request, lat: float, lon: float):
    url = (
        f"https://api.openrouteservice.org/geocode/reverse"
        f"?api_key={ORS_API_KEY}&point.lat={lat}&point.lon={lon}&size=1"
    )
    r = requests.get(url, timeout=10).json()
    if not r.get("features"):
        return {"label": f"{lat:.5f}, {lon:.5f}"}
    return {"label": r["features"][0]["properties"]["label"]}

@app.get("/autocomplete")
@limiter.limit("30/minute")
async def autocomplete(request: Request, query: str):
    if not query or len(query) < 3:
        return {"suggestions": []}
    if len(query) > MAX_QUERY_LENGTH:
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
