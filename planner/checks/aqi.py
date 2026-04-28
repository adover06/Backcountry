"""AQI summary using AirNow."""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .cache import TTLCache, env_ttl_seconds

# Load .env file
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(env_path)

# Default API key - will use env var if set
AIRNOW_API_KEY = os.environ.get("AIRNOW_API_KEY", "")
AQI_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("AQI_CACHE_TTL_SECONDS", 1800))


def get_aqi_summary(lat: float, lng: float) -> dict:
    cache_key = f"{round(lat, 3)}:{round(lng, 3)}"
    cached = AQI_CACHE.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.environ.get("AIRNOW_API_KEY", AIRNOW_API_KEY)
    if not api_key:
        return {"error": "AQI unavailable", "observations": []}

    try:
        url = (
            "https://www.airnowapi.org/aq/observation/latLong/current/"
            f"?format=application/json&latitude={lat}&longitude={lng}"
            f"&distance=25&API_KEY={api_key}"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        result = {
            "observations": [
                {
                    "parameter": o.get("ParameterName"),
                    "aqi": o.get("AQI"),
                    "category": (o.get("Category") or {}).get("Name"),
                }
                for o in data
            ]
        }
        AQI_CACHE.set(cache_key, result)
        return result
    except Exception as e:
        return {"error": str(e), "observations": []}
