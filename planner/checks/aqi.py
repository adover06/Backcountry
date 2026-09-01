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
        return {
            "status": "unavailable",
            "message": "AIRNOW_API_KEY not configured",
            "observations": [],
        }

    url = (
        "https://www.airnowapi.org/aq/observation/latLong/current/"
        f"?format=application/json&latitude={lat}&longitude={lng}"
        f"&distance=25&API_KEY={api_key}"
    )
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            result = {
                "status": "ok",
                "provider": "AirNow",
                "observations": [
                    {
                        "parameter": o.get("ParameterName"),
                        "aqi": o.get("AQI"),
                        "category": (o.get("Category") or {}).get("Name"),
                        "site": o.get("ReportingArea"),
                    }
                    for o in data
                ],
                # AirNow searches this far for a monitor. In the backcountry the
                # nearest station is often well outside it, which is why an empty
                # observation list means "no data", never "clean air".
                "search_radius_mi": 25,
            }
            AQI_CACHE.set(cache_key, result)
            return result
        except Exception as e:
            last_err = e
    return {"status": "unavailable", "message": str(last_err), "observations": []}
