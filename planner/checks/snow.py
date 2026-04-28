"""Snow summary using Open-Meteo forecast sampling."""

from __future__ import annotations

import math
from statistics import mean

import requests


OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
MILES_PER_LAT_DEGREE = 69.0


def _sample_points(lat: float, lng: float, radius_miles: float) -> list[tuple[float, float]]:
    lat_delta = radius_miles / MILES_PER_LAT_DEGREE
    lng_delta = radius_miles / (MILES_PER_LAT_DEGREE * max(math.cos(math.radians(lat)), 0.2))
    points = []
    for lat_offset in (-lat_delta, 0.0, lat_delta):
        for lng_offset in (-lng_delta, 0.0, lng_delta):
            points.append((round(lat + lat_offset, 6), round(lng + lng_offset, 6)))
    return points


def _fetch_point(lat: float, lng: float, start_date: str, end_date: str, elevation_m: float | None = None) -> dict:
    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": "snow_depth",
        "daily": "snowfall_sum",
        "elevation": elevation_m if elevation_m is not None else 0,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
    }
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    r = requests.get(OPEN_METEO_FORECAST, params=params, timeout=8)
    r.raise_for_status()
    data = r.json()

    # Open-Meteo snow_depth is meters and snowfall_sum is centimeters.
    depths_m = [v for v in data.get("hourly", {}).get("snow_depth", []) if isinstance(v, (int, float))]
    snowfall_cm = [v for v in data.get("daily", {}).get("snowfall_sum", []) if isinstance(v, (int, float))]
    max_depth_in = max(depths_m) * 39.3701 if depths_m else 0.0
    total_snowfall_in = sum(snowfall_cm) * 0.393701 if snowfall_cm else 0.0

    return {
        "lat": lat,
        "lng": lng,
        "max_depth_in": round(max_depth_in, 1),
        "total_snowfall_in": round(total_snowfall_in, 1),
    }


def _representative_point(route: dict | None, lat: float, lng: float) -> tuple[float, float, float | None]:
    if not route:
        return lat, lng, None

    points = route.get("points") or []
    if not points:
        return lat, lng, None

    best = max(points, key=lambda p: p.get("ele") or 0)
    return best.get("lat", lat), best.get("lng", lng), best.get("ele")


def get_snow_summary(lat: float, lng: float, start_date: str, end_date: str, radius_miles: float = 5.0, route: dict | None = None) -> dict:
    try:
        center_lat, center_lng, elevation_m = _representative_point(route, lat, lng)
        samples = [_fetch_point(p_lat, p_lng, start_date, end_date, elevation_m=elevation_m) for p_lat, p_lng in _sample_points(center_lat, center_lng, radius_miles)]
        max_depth = max((s["max_depth_in"] for s in samples), default=0.0)
        avg_depth = mean([s["max_depth_in"] for s in samples]) if samples else 0.0
        max_snowfall = max((s["total_snowfall_in"] for s in samples), default=0.0)
        snow_samples = [s for s in samples if s["max_depth_in"] >= 1 or s["total_snowfall_in"] >= 0.1]

        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [s["lng"], s["lat"]]},
                    "properties": {
                        "max_depth_in": s["max_depth_in"],
                        "total_snowfall_in": s["total_snowfall_in"],
                    },
                }
                for s in samples
            ],
        }

        if snow_samples:
            message = f"Snow detected in {len(snow_samples)} of {len(samples)} sampled points within {radius_miles:g} miles."
        else:
            message = f"No meaningful snow detected in the sampled {radius_miles:g}-mile area."

        return {
            "status": "ok",
            "provider": "Open-Meteo",
            "radius_miles": radius_miles,
            "center": {"lat": round(center_lat, 6), "lng": round(center_lng, 6), "elevation_m": elevation_m},
            "message": message,
            "max_depth_in": round(max_depth, 1),
            "avg_depth_in": round(avg_depth, 1),
            "max_snowfall_in": round(max_snowfall, 1),
            "snow_detected_samples": len(snow_samples),
            "samples": samples,
            "geojson": geojson,
        }
    except Exception as e:
        return {
            "status": "unavailable",
            "provider": "Open-Meteo",
            "message": str(e),
            "max_depth_in": None,
            "avg_depth_in": None,
            "max_snowfall_in": None,
            "samples": [],
            "geojson": None,
        }
