import os
import time
from io import StringIO
import pandas as pd
import requests
from shapely.geometry import Point, mapping
from shapely.ops import unary_union
from database import get_connection, init_db

FIRMS_API_KEY = os.getenv("FIRMS_API_KEY")
FIRMS_BBOX = os.getenv("FIRMS_BBOX", "").strip()
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
_cache = {"data": None, "avoid_geojson": None, "timestamp": 0}
CACHE_TTL_SECONDS = 15


def invalidate_cache():
    _cache["data"] = None
    _cache["avoid_geojson"] = None
    _cache["timestamp"] = 0


def _empty():
    return pd.DataFrame(columns=["latitude","longitude","mag","place","source","event_id","camera_id","status","magnitude","fire_size_percent","duration_seconds","max_temperature_c","timestamp","image","video","visual_fire_score","detection_method","severity_index"])


def _load_reported_fires():
    try:
        data = pd.read_csv(os.path.join(DATA_DIR, "fires.csv"))
        for column in ("latitude", "longitude", "mag"):
            data[column] = pd.to_numeric(data[column].astype(str).str.replace("−", "-", regex=False), errors="coerce")
        data = data.dropna(subset=["latitude", "longitude"])
        data = data[data["latitude"].between(-90, 90) & data["longitude"].between(-180, 180)]
        data["source"] = "reported"
        if "place" not in data.columns:
            data["place"] = "Reported fire"
        return data[["latitude","longitude","mag","place","source"]]
    except Exception:
        return _empty()[["latitude","longitude","mag","place","source"]]


def _load_device_fires():
    init_db()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM device_events ORDER BY timestamp DESC").fetchall()
    if not rows:
        return _empty()
    data = pd.DataFrame([dict(r) for r in rows])
    for col in ["latitude","longitude","fire_size_percent","duration_seconds","max_temperature_c","severity_index"]:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data["mag"] = data["severity_index"].clip(lower=0.5, upper=5)
    data["place"] = data["place"].fillna("Raspberry Pi camera")
    data["source"] = "device"
    return data[["latitude","longitude","mag","place","source","event_id","camera_id","status","magnitude","fire_size_percent","duration_seconds","max_temperature_c","timestamp","image","video","visual_fire_score","detection_method","severity_index"]]


def _load_satellite_fires(reported_df):
    if not FIRMS_API_KEY:
        return _empty()[["latitude","longitude","mag","place","source"]]
    # Prefer the registered camera deployment footprint. Reported-fire coordinates are
    # only a fallback; otherwise unrelated reports can pull the NASA query away from
    # the actual installation.
    try:
        with get_connection() as conn:
            camera_rows = conn.execute("SELECT latitude,longitude FROM cameras WHERE enabled=1").fetchall()
        base = pd.DataFrame([dict(r) for r in camera_rows])
    except Exception:
        base = pd.DataFrame()
    if base.empty and not reported_df.empty:
        base = reported_df
    if FIRMS_BBOX:
        try:
            west, south, east, north = [float(value.strip()) for value in FIRMS_BBOX.split(",")]
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("FIRMS_BBOX coordinates are out of range")
            min_lon, min_lat, max_lon, max_lat = west, south, east, north
        except Exception:
            return _empty()[["latitude","longitude","mag","place","source"]]
    else:
        if base.empty:
            return _empty()[["latitude","longitude","mag","place","source"]]
        pad = 2.0
        min_lon = max(-180.0, float(base["longitude"].min()) - pad)
        min_lat = max(-90.0, float(base["latitude"].min()) - pad)
        max_lon = min(180.0, float(base["longitude"].max()) + pad)
        max_lat = min(90.0, float(base["latitude"].max()) + pad)
    area = f"{min_lon},{min_lat},{max_lon},{max_lat}"
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_API_KEY}/VIIRS_SNPP_NRT/{area}/1"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text))
    except Exception:
        return _empty()[["latitude","longitude","mag","place","source"]]
    if df.empty or "latitude" not in df.columns:
        return _empty()[["latitude","longitude","mag","place","source"]]
    frp_source = df["frp"] if "frp" in df.columns else pd.Series(1.0, index=df.index)
    frp = pd.to_numeric(frp_source, errors="coerce").fillna(1)
    scale = max(float(frp.max()), 1.0)
    df["mag"] = (frp / scale * 5).clip(lower=.5, upper=5)
    df["place"] = "Satellite detection"
    df["source"] = "satellite"
    return df[["latitude","longitude","mag","place","source"]]


def load_fires(use_cache=True):
    now = time.time()
    if use_cache and _cache["data"] is not None and now - _cache["timestamp"] < CACHE_TTL_SECONDS:
        return _cache["data"]
    reported = _load_reported_fires()
    satellite = _load_satellite_fires(reported)
    device = _load_device_fires()
    nonempty = [frame for frame in (reported, satellite, device) if frame is not None and not frame.empty]
    combined = pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else _empty()
    combined["mag"] = pd.to_numeric(combined["mag"], errors="coerce")
    combined = combined.dropna(subset=["latitude","longitude"])
    _cache["data"] = combined
    _cache["avoid_geojson"] = None
    _cache["timestamp"] = now
    return combined


def get_avoid_geojson():
    data = load_fires()
    if _cache["avoid_geojson"] is not None:
        return _cache["avoid_geojson"]
    # Only confirmed camera detections are automatically treated as device hazards.
    # The combined frame may contain only satellite or only reported rows, so `status`
    # is not guaranteed to exist until at least one device event has been loaded.
    if data.empty:
        hazards = pd.DataFrame()
    else:
        source = data.get("source", pd.Series(index=data.index, dtype=str))
        device_confirmed = (
            data[(source == "device") & (data["status"] == "confirmed")]
            if "status" in data.columns else data.iloc[0:0]
        )
        reported_and_satellite = data[source.isin(["reported", "satellite"])]
        hazards = pd.concat([reported_and_satellite, device_confirmed], ignore_index=True)
    polygons = [Point(row["longitude"], row["latitude"]).buffer(.01) for _, row in hazards.iterrows()]
    if not polygons:
        _cache["avoid_geojson"] = None
        return None
    _cache["avoid_geojson"] = mapping(unary_union(polygons))
    return _cache["avoid_geojson"]


def get_last_updated():
    return time.strftime("%I:%M %p", time.localtime(_cache["timestamp"])) if _cache["timestamp"] else None


def get_satellite_status():
    """Return metadata for the optional NASA FIRMS fire-detection layer."""
    configured = bool(os.getenv("FIRMS_API_KEY") or FIRMS_API_KEY)
    if not configured:
        return {"configured": False, "count": 0, "updated_at": get_last_updated()}
    data = load_fires()
    count = int((data.get("source", pd.Series(dtype=str)) == "satellite").sum()) if not data.empty else 0
    return {"configured": True, "count": count, "updated_at": get_last_updated()}
