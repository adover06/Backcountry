"""Snow summary using Open-Meteo — single measurement at the highest route point."""

from __future__ import annotations

import json

import requests

from .cache import TTLCache, env_ttl_seconds


OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
SNOW_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("SNOW_CACHE_TTL_SECONDS", 3600))


def _highest_point(route: dict | None, lat: float, lng: float) -> tuple[float, float]:
    points = (route or {}).get("points") or []
    if not points:
        return lat, lng
    best = max(points, key=lambda p: p.get("ele") or 0)
    return best.get("lat", lat), best.get("lng", lng)


def get_snow_summary(lat: float, lng: float, start_date: str, end_date: str, radius_miles: float = 5.0, route: dict | None = None) -> dict:
    try:
        sample_lat, sample_lng = _highest_point(route, lat, lng)
        cache_key = json.dumps(
            {"lat": round(sample_lat, 3), "lng": round(sample_lng, 3), "start": start_date or "", "end": end_date or ""},
            sort_keys=True,
        )
        cached = SNOW_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "latitude": sample_lat,
            "longitude": sample_lng,
            "hourly": "snow_depth",
            "daily": "snowfall_sum",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        r = requests.get(OPEN_METEO_FORECAST, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        # snow_depth is meters; snowfall_sum is centimeters
        depths_m = [v for v in data.get("hourly", {}).get("snow_depth", []) if isinstance(v, (int, float))]
        snowfall_cm = [v for v in data.get("daily", {}).get("snowfall_sum", []) if isinstance(v, (int, float))]
        max_depth_in = round(max(depths_m) * 39.3701, 1) if depths_m else 0.0
        total_snowfall_in = round(sum(snowfall_cm) * 0.393701, 1) if snowfall_cm else 0.0

        if max_depth_in >= 1 or total_snowfall_in >= 0.1:
            message = f"Snow detected at highest route point. Max depth: {max_depth_in} in."
        else:
            message = "No meaningful snow at the highest route point."

        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [sample_lng, sample_lat]},
                    "properties": {"max_depth_in": max_depth_in, "total_snowfall_in": total_snowfall_in},
                }
            ],
        }

        avg_depth_in = (
            round(sum(depths_m) / len(depths_m) * 39.3701, 1) if depths_m else None
        )

        result = {
            "status": "ok",
            "provider": "Open-Meteo",
            "message": message,
            "max_depth_in": max_depth_in,
            # Previously this was set to max_depth_in, reporting a single sample as
            # though it were an average.
            "avg_depth_in": avg_depth_in,
            "max_snowfall_in": total_snowfall_in,
            "sample_point": [sample_lng, sample_lat],
            "sample_count": len(depths_m),
            "geojson": geojson,
        }
        SNOW_CACHE.set(cache_key, result)
        return result
    except Exception as e:
        return {
            "status": "unavailable",
            "provider": "Open-Meteo",
            "message": str(e),
            "max_depth_in": None,
            "avg_depth_in": None,
            "max_snowfall_in": None,
            "geojson": None,
        }
