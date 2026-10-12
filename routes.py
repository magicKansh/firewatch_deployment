
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
    """Fail with a useful message if routing is not configured."""
    if not ORS_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "Route planning is not configured. "
                "Set ORS_API_KEY in the server environment."
            ),
        )


def call_ors_route(start, end, profile):
    """Request a route using the selected travel profile."""
    _require_ors_key()

    url = (
        "https://api.openrouteservice.org/v2/directions/"
        f"{profile}/geojson"
    )

    payload = {
        "coordinates": [start, end],
        "instructions": True,
    }

    avoid = get_avoid_geojson()

    if avoid:
        payload["options"] = {"avoid_polygons": avoid}

    response = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": ORS_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def geocode(query):
    """Convert an address or 'latitude, longitude' pair to ORS coordinates."""
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_LENGTH
    ):
        raise ValueError("Enter a location under 200 characters.")

    # The current-location button can pass coordinates directly, allowing
    # navigation to continue even if reverse geocoding is unavailable.
    pieces = [piece.strip() for piece in query.split(",")]

    if len(pieces) == 2:
        try:
            latitude = float(pieces[0])
            longitude = float(pieces[1])

            if (
                -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                return [longitude, latitude]

        except ValueError:
            pass

    _require_ors_key()

    response = requests.get(
        "https://api.openrouteservice.org/geocode/search",
        params={
            "api_key": ORS_API_KEY,
            "text": query.strip(),
            "size": 5,
        },
        timeout=12,
    )

    response.raise_for_status()
    payload = response.json()

    if not payload.get("features"):
        raise ValueError(f"Could not find '{query}'.")

    return payload["features"][0]["geometry"]["coordinates"]


@app.post("/routes")
@limiter.limit("10/minute")
async def calculate_routes(request: Request):
    """Calculate a route for driving, biking, and walking."""
    _require_ors_key()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Expected JSON containing 'start' and 'end'.",
        )

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail="Expected a JSON object containing 'start' and 'end'.",
        )

    start_text = body.get("start")
    end_text = body.get("end")

    if (
        not isinstance(start_text, str)
        or not isinstance(end_text, str)
        or not start_text.strip()
        or not end_text.strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="Both 'start' and 'end' are required.",
        )

    if (
        len(start_text) > MAX_QUERY_LENGTH
        or len(end_text) > MAX_QUERY_LENGTH
    ):
        raise HTTPException(
            status_code=400,
            detail="Location text is too long.",
        )

    try:
        start = geocode(start_text)
        end = geocode(end_text)

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Location search service failed: {exc}",
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    avoid = get_avoid_geojson()
    avoidance_active = bool(avoid)

    results = {}

    for key, profile in PROFILES.items():
        try:
            route = call_ors_route(start, end, profile)
            features = route.get("features") or []

            if not features:
                error = route.get("error") or {}
                results[key] = {
                    "error": error.get(
                        "message", "No route available."
                    )
                }
                continue

            feature = features[0]
            coordinates = feature["geometry"]["coordinates"]
            properties = feature.get("properties", {})
            summary = properties.get("summary", {})

            steps = []

            for segment in properties.get("segments", []):
                for step in segment.get("steps", []):
                    steps.append(
                        {
                            "instruction": step.get(
                                "instruction", ""
                            ),
                            "distance": step.get("distance", 0),
                            "duration": step.get("duration", 0),
                        }
                    )

            results[key] = {
                "lat": [point[1] for point in coordinates],
                "lon": [point[0] for point in coordinates],
                "duration": summary.get("duration", 0),
                "distance": summary.get("distance", 0),
                "steps": steps,
                "start": {
                    "lat": start[1],
                    "lon": start[0],
                },
                "end": {
                    "lat": end[1],
                    "lon": end[0],
                },
            }

        except requests.RequestException as exc:
            results[key] = {
                "error": f"Route service failed: {exc}"
            }

        except (KeyError, IndexError, TypeError) as exc:
            results[key] = {
                "error": f"Route response could not be interpreted: {exc}"
            }

    results["_meta"] = {
        "avoidance_active": avoidance_active,
    }

    return results


@app.get("/reverse")
@limiter.limit("15/minute")
async def reverse_geocode(
    request: Request,
    lat: float,
    lon: float,
):
    """Convert the browser's current coordinates into an address."""
    _require_ors_key()

    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise HTTPException(
            status_code=400,
            detail="Latitude or longitude is out of range.",
        )

    try:
        response = requests.get(
            "https://api.openrouteservice.org/geocode/reverse",
            params={
                "api_key": ORS_API_KEY,
                "point.lat": lat,
                "point.lon": lon,
                "size": 1,
            },
            timeout=12,
        )

        response.raise_for_status()
        payload = response.json()

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Reverse geocoding service failed: {exc}",
        )

    if not payload.get("features"):
        return {"label": f"{lat:.6f}, {lon:.6f}"}

    return {
        "label": payload["features"][0]["properties"].get(
            "label", f"{lat:.6f}, {lon:.6f}"
        )
    }


@app.get("/autocomplete")
@limiter.limit("30/minute")
async def autocomplete(request: Request, query: str):
    """
    Search location suggestions from specific places to larger regions.

    The frontend also sorts results by layer so a street or address is shown
    ahead of a city or county where the results provide those classifications.
    """
    _require_ors_key()

    query = query.strip()

    if len(query) < 3 or len(query) > MAX_QUERY_LENGTH:
        return {"suggestions": []}

    def search(layers, size=7):
        params = {
            "api_key": ORS_API_KEY,
            "text": query,
            "size": size,
        }

        if layers:
            params["layers"] = layers

        response = requests.get(
            "https://api.openrouteservice.org/geocode/autocomplete",
            params=params,
            timeout=12,
        )

        response.raise_for_status()
        return response.json()

    try:
        # Begin with venues, addresses, streets, and neighbourhoods.
        specific = search(
            "venue,address,street,neighbourhood,locality",
            size=7,
        )

        specific_features = specific.get("features", [])

        # Search more broadly if few specific matches are returned.
        if len(specific_features) < 5:
            broad = search(
                (
                    "venue,address,street,neighbourhood,locality,"
                    "borough,localadmin,county,region,country"
                ),
                size=10,
            )

            seen = {
                feature.get("properties", {})
                .get("label", "")
                .casefold()
                for feature in specific_features
            }

            for feature in broad.get("features", []):
                label = (
                    feature.get("properties", {})
                    .get("label", "")
                )

                if label.casefold() not in seen:
                    specific_features.append(feature)
                    seen.add(label.casefold())

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Autocomplete service failed: {exc}",
        )

    # Order the suggestions before returning them to the browser.
    specificity = {
        "venue": 0,
        "address": 1,
        "street": 2,
        "neighbourhood": 3,
        "locality": 4,
        "borough": 5,
        "localadmin": 6,
        "county": 7,
        "region": 8,
        "country": 9,
    }

    suggestions = []

    for feature in specific_features:
        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        coordinates = geometry.get("coordinates") or []

        if len(coordinates) < 2:
            continue

        label = properties.get("label", "").strip()

        if not label:
            continue

        layer = properties.get("layer", "location")

        suggestions.append(
            {
                "label": label,
                "layer": layer,
                "specificity": specificity.get(
                    str(layer).lower(), 5
                ),
                "lon": coordinates[0],
                "lat": coordinates[1],
            }
        )

    suggestions.sort(
        key=lambda item: (
            item["specificity"],
            len(item["label"]),
        )
    )

    return {"suggestions": suggestions[:7]}
