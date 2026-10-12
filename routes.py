"""OpenRouteService geocoding, routing, and fire-avoidance endpoints."""
import os
import requests
from fastapi import HTTPException, Request

from app import app, limiter
from fire_data import get_avoid_geojson

ORS_API_KEY = os.getenv("ORS_API_KEY")
PROFILES = {
    "driving-car": "driving-car",
    "cycling-regular": "cycling-regular",
    "foot-walking": "foot-walking",
}
MAX_QUERY_LENGTH = 200


def _require_ors_key():
    if not ORS_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Route planning is not configured: set ORS_API_KEY in the server environment.",
        )


def call_ors_route(start, end, profile):
    _require_ors_key()
    url = f"https://api.openrouteservice.org/v2/directions/{profile}/geojson"
    payload = {"coordinates": [start, end], "instructions": True}
    avoid = get_avoid_geojson()
    if avoid:
        payload["options"] = {"avoid_polygons": avoid}
    response = requests.post(
        url, json=payload,
        headers={"Authorization": ORS_API_KEY, "Content-Type": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def geocode(query):
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
        raise ValueError("Enter a location under 200 characters.")
    # Accept coordinates from the browser's current-location button even if reverse geocoding is down.
    pieces = [part.strip() for part in query.split(",")]
    if len(pieces) == 2:
        try:
            latitude, longitude = float(pieces[0]), float(pieces[1])
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return [longitude, latitude]
        except ValueError:
            pass
    _require_ors_key()
    response = requests.get(
        "https://api.openrouteservice.org/geocode/search",
        params={"api_key": ORS_API_KEY, "text": query.strip()}, timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("features"):
        raise ValueError(f"Could not find '{query}'.")
    return payload["features"][0]["geometry"]["coordinates"]


@app.post("/routes")
@limiter.limit("10/minute")
async def calculate_routes(request: Request):
    _require_ors_key()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON containing 'start' and 'end'.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object containing 'start' and 'end'.")
    start_text, end_text = body.get("start"), body.get("end")
    if not isinstance(start_text, str) or not isinstance(end_text, str) or not start_text.strip() or not end_text.strip():
        raise HTTPException(status_code=400, detail="Both 'start' and 'end' are required location strings.")
    if len(start_text) > MAX_QUERY_LENGTH or len(end_text) > MAX_QUERY_LENGTH:
        raise HTTPException(status_code=400, detail="Location text is too long.")
    try:
        start, end = geocode(start_text), geocode(end_text)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Location search service failed: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    avoidance_active = bool(get_avoid_geojson())
    results = {}
    for key, profile in PROFILES.items():
        try:
            route = call_ors_route(start, end, profile)
            features = route.get("features") or []
            if not features:
                results[key] = {"error": route.get("error", {}).get("message", "No route available.")}
                continue
            feature = features[0]
            coords = feature["geometry"]["coordinates"]
            properties = feature.get("properties", {})
            summary = properties.get("summary", {})
            segments = properties.get("segments", [])
            steps = []
            for segment in segments:
                for step in segment.get("steps", []):
                    steps.append({
                        "instruction": step.get("instruction", ""),
                        "distance": step.get("distance", 0),
                        "duration": step.get("duration", 0),
                    })
            results[key] = {
                "lat": [coord[1] for coord in coords],
                "lon": [coord[0] for coord in coords],
                "duration": summary.get("duration"),
                "distance": summary.get("distance"),
                "steps": steps,
                "start": {"lat": start[1], "lon": start[0]},
                "end": {"lat": end[1], "lon": end[0]},
            }
        except requests.RequestException as exc:
            results[key] = {"error": f"Route service failed: {exc}"}
        except (KeyError, IndexError, TypeError) as exc:
            results[key] = {"error": f"Route response could not be interpreted: {exc}"}
    results["_meta"] = {"avoidance_active": avoidance_active}
    return results


@app.get("/reverse")
@limiter.limit("15/minute")
async def reverse_geocode(request: Request, lat: float, lon: float):
    _require_ors_key()
    try:
        response = requests.get(
            "https://api.openrouteservice.org/geocode/reverse",
            params={"api_key": ORS_API_KEY, "point.lat": lat, "point.lon": lon, "size": 1},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Reverse geocoding service failed: {exc}")
    if not payload.get("features"):
        return {"label": f"{lat:.5f}, {lon:.5f}"}
    return {"label": payload["features"][0]["properties"].get("label", f"{lat:.5f}, {lon:.5f}")}


@app.get("/autocomplete")
@limiter.limit("30/minute")
async def autocomplete(request: Request, query: str):
    _require_ors_key()
    query = query.strip()
    if len(query) < 3 or len(query) > MAX_QUERY_LENGTH:
        return {"suggestions": []}

    def search(layers=None):
        params = {"api_key": ORS_API_KEY, "text": query, "size": 5}
        if layers:
            params["layers"] = layers
        response = requests.get("https://api.openrouteservice.org/geocode/autocomplete", params=params, timeout=12)
        response.raise_for_status()
        return response.json()

    try:
        # Ask for precise features first, then broaden the search only if needed.
        payload = search("venue,address,street,neighbourhood,locality")
        if len(payload.get("features", [])) < 3:
            broader = search("venue,address,street,neighbourhood,locality,borough,localadmin,county,region,country")
            seen = {feature.get("properties", {}).get("label", "").casefold() for feature in payload.get("features", [])}
            payload["features"] = payload.get("features", []) + [
                feature for feature in broader.get("features", [])
                if feature.get("properties", {}).get("label", "").casefold() not in seen
            ]
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Autocomplete service failed: {exc}")
    return {
        "suggestions": [
            {
                "label": feature["properties"].get("label", ""),
                "layer": feature["properties"].get("layer", "location"),
                "lon": feature["geometry"]["coordinates"][0],
                "lat": feature["geometry"]["coordinates"][1],
            }
            for feature in payload.get("features", [])
        ]
    }
