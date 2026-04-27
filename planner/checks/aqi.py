"""AQI summary using AirNow."""

from __future__ import annotations

import os
import requests


def get_aqi_summary(lat: float, lng: float) -> dict:
    api_key = os.environ.get("AIRNOW_API_KEY")
    if not api_key:
        return {"error": "AIRNOW_API_KEY not set", "observations": []}

    try:
        url = (
            "https://www.airnowapi.org/aq/observation/latLong/current/"
            f"?format=application/json&latitude={lat}&longitude={lng}"
            f"&distance=25&API_KEY={api_key}"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        return {
            "observations": [
                {
                    "parameter": o.get("ParameterName"),
                    "aqi": o.get("AQI"),
                    "category": (o.get("Category") or {}).get("Name"),
                }
                for o in data
            ]
        }
    except Exception as e:
        return {"error": str(e), "observations": []}
