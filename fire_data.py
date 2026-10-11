
import os
import time
from io import StringIO

import pandas as pd
import requests
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

from database import get_connection, init_db


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

FIRMS_API_KEY = os.getenv("FIRMS_API_KEY")
FIRMS_BBOX = os.getenv("FIRMS_BBOX", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

_cache = {
    "data": None,
    "avoid_geojson": None,
    "timestamp": 0,
}

CACHE_TTL_SECONDS = 15


# ---------------------------------------------------------
# Cache management
# ---------------------------------------------------------

def invalidate_cache():
    """Force fire data to reload on the next request."""
    _cache["data"] = None
    _cache["avoid_geojson"] = None
    _cache["timestamp"] = 0


# ---------------------------------------------------------
# Empty data structure
# ---------------------------------------------------------

def _empty():
    """Return an empty dataframe with the expected columns."""
    return pd.DataFrame(
        columns=[
            "latitude",
            "longitude",
            "mag",
            "place",
            "source",
            "event_id",
            "camera_id",
            "status",
            "magnitude",
            "fire_size_percent",
            "duration_seconds",
            "max_temperature_c",
            "timestamp",
            "image",
            "video",
            "visual_fire_score",
            "detection_method",
            "severity_index",
        ]
    )


# ---------------------------------------------------------
# Reported fire data
# ---------------------------------------------------------

def _load_reported_fires():
    """Load manually reported fires from data/fires.csv."""
    file_path = os.path.join(DATA_DIR, "fires.csv")

    try:
        data = pd.read_csv(file_path)

        for column in ("latitude", "longitude", "mag"):
            if column in data.columns:
                data[column] = pd.to_numeric(
                    data[column]
                    .astype(str)
                    .str.replace("−", "-", regex=False),
                    errors="coerce",
                )

        data = data.dropna(subset=["latitude", "longitude"])

        data = data[
            data["latitude"].between(-90, 90)
            & data["longitude"].between(-180, 180)
        ]

        if "mag" not in data.columns:
            data["mag"] = 1.0

        if "place" not in data.columns:
            data["place"] = "Reported fire"

        data["source"] = "reported"

        return data[
            ["latitude", "longitude", "mag", "place", "source"]
        ]

    except (OSError, ValueError, KeyError, pd.errors.ParserError):
        return _empty()[
            ["latitude", "longitude", "mag", "place", "source"]
        ]


# ---------------------------------------------------------
# Raspberry Pi camera events
# ---------------------------------------------------------

def _load_device_fires():
    """Load fire events reported by registered Raspberry Pi cameras."""
    init_db()

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM device_events ORDER BY timestamp DESC"
        ).fetchall()

    if not rows:
        return _empty()

    data = pd.DataFrame([dict(row) for row in rows])

    numeric_columns = [
        "latitude",
        "longitude",
        "fire_size_percent",
        "duration_seconds",
        "max_temperature_c",
        "severity_index",
    ]

    for column in numeric_columns:
        if column in data.columns:
            data[column] = pd.to_numeric(
                data[column], errors="coerce"
            )

    data = data.dropna(subset=["latitude", "longitude"])

    data = data[
        data["latitude"].between(-90, 90)
        & data["longitude"].between(-180, 180)
    ]

    if "severity_index" not in data.columns:
        data["severity_index"] = 1.0

    data["mag"] = data["severity_index"].clip(
        lower=0.5, upper=5
    )

    if "place" not in data.columns:
        data["place"] = "Raspberry Pi camera"
    else:
        data["place"] = data["place"].fillna(
            "Raspberry Pi camera"
        )

    data["source"] = "device"

    # Preserve optional fields when reading older database schemas.
    optional_defaults = {
        "event_id": "",
        "camera_id": "",
        "status": "unconfirmed",
        "magnitude": "Unknown",
        "fire_size_percent": 0.0,
        "duration_seconds": 0.0,
        "max_temperature_c": 0.0,
        "timestamp": "",
        "image": "",
        "video": "",
        "visual_fire_score": 0.0,
        "detection_method": "thermal",
        "severity_index": 1.0,
    }

    for column, default in optional_defaults.items():
        if column not in data.columns:
            data[column] = default

    columns = [
        "latitude",
        "longitude",
        "mag",
        "place",
        "source",
        "event_id",
        "camera_id",
        "status",
        "magnitude",
        "fire_size_percent",
        "duration_seconds",
        "max_temperature_c",
        "timestamp",
        "image",
        "video",
        "visual_fire_score",
        "detection_method",
        "severity_index",
    ]

    return data[columns]


# ---------------------------------------------------------
# NASA FIRMS satellite fire detections
# ---------------------------------------------------------

def _load_satellite_fires(reported_df):
    """Load recent fire detections from NASA FIRMS."""
    if not FIRMS_API_KEY:
        return _empty()[
            ["latitude", "longitude", "mag", "place", "source"]
        ]

    # Prefer the registered camera locations as the monitoring area.
    try:
        with get_connection() as conn:
            camera_rows = conn.execute(
                """
                SELECT latitude, longitude
                FROM cameras
                WHERE enabled = 1
                """
            ).fetchall()

        base = pd.DataFrame(
            [dict(row) for row in camera_rows]
        )

    except Exception:
        base = pd.DataFrame()

    # Use reported fire coordinates as a fallback.
    if base.empty and not reported_df.empty:
        base = reported_df

    # An optional fixed monitoring bounding box can be supplied
    # through the FIRMS_BBOX environment variable:
    # west,south,east,north
    if FIRMS_BBOX:
        try:
            west, south, east, north = [
                float(value.strip())
                for value in FIRMS_BBOX.split(",")
            ]

            if not (
                -180 <= west < east <= 180
                and -90 <= south < north <= 90
            ):
                raise ValueError(
                    "FIRMS_BBOX coordinates are out of range."
                )

            min_lon = west
            min_lat = south
            max_lon = east
            max_lat = north

        except (ValueError, TypeError):
            return _empty()[
                ["latitude", "longitude", "mag", "place", "source"]
            ]

    else:
        if base.empty:
            return _empty()[
                ["latitude", "longitude", "mag", "place", "source"]
            ]

        # Monitoring area around registered cameras.
        pad = 2.0

        min_lon = max(
            -180.0, float(base["longitude"].min()) - pad
        )
        min_lat = max(
            -90.0, float(base["latitude"].min()) - pad
        )
        max_lon = min(
            180.0, float(base["longitude"].max()) + pad
        )
        max_lat = min(
            90.0, float(base["latitude"].max()) + pad
        )

    area = f"{min_lon},{min_lat},{max_lon},{max_lat}"

    url = (
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        f"{FIRMS_API_KEY}/VIIRS_SNPP_NRT/{area}/1"
    )

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()

        data = pd.read_csv(StringIO(response.text))

    except (requests.RequestException, ValueError, pd.errors.ParserError):
        return _empty()[
            ["latitude", "longitude", "mag", "place", "source"]
        ]

    if data.empty or "latitude" not in data.columns:
        return _empty()[
            ["latitude", "longitude", "mag", "place", "source"]
        ]

    if "longitude" not in data.columns:
        return _empty()[
            ["latitude", "longitude", "mag", "place", "source"]
        ]

    data["latitude"] = pd.to_numeric(
        data["latitude"], errors="coerce"
    )
    data["longitude"] = pd.to_numeric(
        data["longitude"], errors="coerce"
    )

    data = data.dropna(subset=["latitude", "longitude"])

    data = data[
        data["latitude"].between(-90, 90)
        & data["longitude"].between(-180, 180)
    ]

    if data.empty:
        return _empty()[
            ["latitude", "longitude", "mag", "place", "source"]
        ]

    # Convert Fire Radiative Power (FRP) to a relative map marker
    # scale. This is a display scale, not measured fire size.
    if "frp" in data.columns:
        frp = pd.to_numeric(data["frp"], errors="coerce").fillna(1)
    else:
        frp = pd.Series(1.0, index=data.index)

    scale = max(float(frp.max()), 1.0)

    data["mag"] = (frp / scale * 5).clip(
        lower=0.5, upper=5
    )

    data["place"] = "Satellite detection"
    data["source"] = "satellite"

    return data[
        ["latitude", "longitude", "mag", "place", "source"]
    ]


# ---------------------------------------------------------
# Public fire-data loader
# ---------------------------------------------------------

def load_fires(use_cache=True):
    """Load and combine reported, satellite, and camera detections."""
    now = time.time()

    if (
        use_cache
        and _cache["data"] is not None
        and now - _cache["timestamp"] < CACHE_TTL_SECONDS
    ):
        return _cache["data"]

    reported = _load_reported_fires()
    satellite = _load_satellite_fires(reported)
    device = _load_device_fires()

    frames = [
        frame
        for frame in (reported, satellite, device)
        if frame is not None and not frame.empty
    ]

    if frames:
        combined = pd.concat(
            frames, ignore_index=True, sort=False
        )
    else:
        combined = _empty()

    combined["latitude"] = pd.to_numeric(
        combined["latitude"], errors="coerce"
    )
    combined["longitude"] = pd.to_numeric(
        combined["longitude"], errors="coerce"
    )
    combined["mag"] = pd.to_numeric(
        combined["mag"], errors="coerce"
    )

    combined = combined.dropna(
        subset=["latitude", "longitude"]
    )

    combined = combined[
        combined["latitude"].between(-90, 90)
        & combined["longitude"].between(-180, 180)
    ]

    _cache["data"] = combined
    _cache["avoid_geojson"] = None
    _cache["timestamp"] = now

    return combined


# ---------------------------------------------------------
# Fire avoidance zones for route planning
# ---------------------------------------------------------

def get_avoid_geojson():
    """
    Return geographic avoidance polygons.

    Reported and satellite detections are included.
    Raspberry Pi detections are included automatically only
    after a human confirms them.
    """
    data = load_fires()

    if _cache["avoid_geojson"] is not None:
        return _cache["avoid_geojson"]

    if data.empty:
        _cache["avoid_geojson"] = None
        return None

    sources = data.get(
        "source",
        pd.Series(index=data.index, dtype="object"),
    )

    # Confirmed camera events only.
    if "status" in data.columns:
        confirmed_device = data[
            (sources == "device")
            & (data["status"] == "confirmed")
        ]
    else:
        confirmed_device = data.iloc[0:0]

    # Public reports and satellite detections.
    public_hazards = data[
        sources.isin(["reported", "satellite"])
    ]

    hazards = pd.concat(
        [public_hazards, confirmed_device],
        ignore_index=True,
    )

    polygons = []

    for _, row in hazards.iterrows():
        try:
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])

            if not (
                -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                continue

            # Approximate local buffer around the detection.
            polygons.append(
                Point(longitude, latitude).buffer(0.01)
            )

        except (ValueError, TypeError, KeyError):
            continue

    if not polygons:
        _cache["avoid_geojson"] = None
        return None

    combined_polygon = unary_union(polygons)

    _cache["avoid_geojson"] = mapping(combined_polygon)

    return _cache["avoid_geojson"]


# ---------------------------------------------------------
# Dashboard helper functions
# ---------------------------------------------------------

def get_last_updated():
    """Return when fire data was last refreshed."""
    timestamp = _cache.get("timestamp", 0)

    if not timestamp:
        return None

    return time.strftime(
        "%I:%M %p",
        time.localtime(timestamp),
    )


def get_satellite_status():
    """Return the NASA FIRMS layer configuration and detection count."""
    configured = bool(
        os.getenv("FIRMS_API_KEY") or FIRMS_API_KEY
    )

    if not configured:
        return {
            "configured": False,
            "count": 0,
            "updated_at": get_last_updated(),
        }

    try:
        data = load_fires()

        if data.empty:
            count = 0
        else:
            sources = data.get(
                "source",
                pd.Series(index=data.index, dtype="object"),
            )

            count = int((sources == "satellite").sum())

    except Exception:
        return {
            "configured": True,
            "count": 0,
            "updated_at": get_last_updated(),
            "error": "Satellite data could not be loaded.",
        }

    return {
        "configured": True,
        "count": count,
        "updated_at": get_last_updated(),
    }
