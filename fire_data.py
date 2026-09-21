import os
import time
from io import StringIO
import pandas as pd
import requests

FIRMS_API_KEY = os.getenv("FIRMS_API_KEY")

_cache = {"data": None, "timestamp": 0}
CACHE_TTL_SECONDS = 900

def _load_reported_fires():
    data_path = os.path.join(os.path.dirname(__file__), 'data', 'fires.csv')
    data = pd.read_csv(data_path)
    data['mag'] = pd.to_numeric(data['mag'], errors='coerce')
    data['source'] = 'reported'
    return data[['latitude', 'longitude', 'mag', 'place', 'source']]

def _load_satellite_fires(reported_df):
    if not FIRMS_API_KEY or reported_df.empty:
        print("FIRMS DEBUG: missing key or empty reported_df, skipping")
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
    print("FIRMS DEBUG: requesting", url.replace(FIRMS_API_KEY, "***KEY***"))

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        print("FIRMS DEBUG: raw response text (first 500 chars):", response.text[:500])
        df = pd.read_csv(StringIO(response.text))
        print("FIRMS DEBUG: parsed columns:", list(df.columns))
        print("FIRMS DEBUG: row count:", len(df))
    except Exception as e:
        print("FIRMS DEBUG: exception occurred:", repr(e))
        return pd.DataFrame(columns=['latitude', 'longitude', 'mag', 'place', 'source'])

    if df.empty or 'latitude' not in df.columns:
        print("FIRMS DEBUG: empty or missing latitude column")
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
    _cache["timestamp"] = now
    return combined
