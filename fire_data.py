import os
import time
from io import StringIO
import pandas as pd
import requests
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

FIRMS_API_KEY = os.getenv("FIRMS_API_KEY")

_cache = {"data": None, "avoid_geojson": None, "timestamp": 0}
CACHE_TTL_SECONDS = 900

def _load_reported_fires():
    try:
        data_path = os.path.join(os.path.dirname(__file__), 'data', 'fires.csv')
        data = pd.read_csv(data_path)
        data['mag'] = pd.to_numeric(data['mag'], errors='coerce')
        data['source'] = 'reported'
        return data[['latitude', 'longitude', 'mag', 'place', 'source']]
    except Exception:
        return pd.DataFrame(columns=['latitude', 'longitude', 'mag', 'place', 'source'])

def _load_satellite_fires(reported_df):
    if not FIRMS_API_KEY or reported_df.empty:
        return pd.DataFrame(columns=['latitude', 'longitude', 'mag', 'place', 'source'])

    pad = 2.0
    west = reported_df['longitude'].min() - pad
    east = reported_df['longitude'].max() + pad
    south = reported_df['latitude'].min() - pad
    north = reported_df['latitude'].max() + pad
    area = f"{west},{south},{east},{north}"

    url = (
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        f"{FIRMS_API_KEY}/VIIRS_SNPP_NRT/{area}/1"
    )

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text))
    except Exception:
        return pd.DataFrame(columns=['latitude', 'longitude', 'mag', 'place', 'source'])

    if df.empty or 'latitude' not in df.columns:
        return pd.DataFrame(columns=['latitude', 'longitude', 'mag', 'place', 'source'])

    df['mag'] = (df.get('frp', 1) / df.get('frp', 1).max() * 5).clip(lower=0.5, upper=5)
    df['place'] = 'Satellite detection'
    df['source'] = 'satellite'

    return df[['latitude', 'longitude', 'mag', 'place', 'source']]

def load_fires(use_cache=True):
    now = time.time()
    if use_cache and _cache["data"] is not None and (now - _cache["timestamp"] < CACHE_TTL_SECONDS):
        return _cache["data"]

    reported = _load_reported_fires()
    satellite = _load_satellite_fires(reported)

    combined = pd.concat([reported, satellite], ignore_index=True)
    combined['mag'] = pd.to_numeric(combined['mag'], errors='coerce')
    combined = combined.dropna(subset=['latitude', 'longitude'])

    _cache["data"] = combined
    _cache["avoid_geojson"] = None  # invalidate stale polygon cache when fire data refreshes
    _cache["timestamp"] = now
    return combined

def get_avoid_geojson():
    data = load_fires()

    if _cache["avoid_geojson"] is not None:
        return _cache["avoid_geojson"]

    fire_polygons = [
        Point(row['longitude'], row['latitude']).buffer(0.01)
        for _, row in data.iterrows()
    ]

    if not fire_polygons:
        _cache["avoid_geojson"] = None
        return None

    avoid_area = unary_union(fire_polygons)
    geojson = mapping(avoid_area)
    _cache["avoid_geojson"] = geojson
    return geojson

def get_last_updated():
    if _cache["timestamp"] == 0:
        return None
    return time.strftime("%I:%M %p", time.localtime(_cache["timestamp"]))
