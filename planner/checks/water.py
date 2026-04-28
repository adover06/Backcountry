"""Water source check using OpenStreetMap Overpass API."""

from __future__ import annotations

import json
import math

import requests

from .cache import TTLCache, env_ttl_seconds

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WATER_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("WATER_CACHE_TTL_SECONDS", 3600))

_WATERWAY_TYPES = {"stream", "river", "creek"}
_LAKE_TYPES = {"lake", "reservoir", "pond"}


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _classify(tags: dict) -> str | None:
    natural = tags.get("natural", "")
    waterway = tags.get("waterway", "")
    water = tags.get("water", "")
    if natural == "spring":
        return "spring"
    if waterway in _WATERWAY_TYPES:
        return waterway
    if natural == "water":
        return water if water in _LAKE_TYPES else "lake"
    return None


def get_water_summary(lat: float, lng: float, radius_miles: float = 5.0) -> dict:
    radius_m = int(radius_miles * 1609.34)
    cache_key = json.dumps(
        {"lat": round(lat, 3), "lng": round(lng, 3), "r": radius_miles},
        sort_keys=True,
    )
    cached = WATER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    query = (
        f"[out:json][timeout:25];\n"
        f"(\n"
        f'  node["natural"="spring"](around:{radius_m},{lat},{lng});\n'
        f'  way["natural"="water"]["water"~"lake|pond|reservoir"](around:{radius_m},{lat},{lng});\n'
        f'  way["waterway"~"stream|river"](around:{radius_m},{lat},{lng});\n'
        f');\n'
        f"out center;\n"
    )

    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=25,
            headers={"User-Agent": "BackcountryPlanner/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        result = {"error": str(exc), "count": 0, "message": "Water data unavailable", "geojson": None}
        WATER_CACHE.set(cache_key, result)
        return result

    features = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        wtype = _classify(tags)
        if not wtype:
            continue

        if el["type"] == "node":
            elat, elng = el.get("lat"), el.get("lon")
        else:
            center = el.get("center", {})
            elat, elng = center.get("lat"), center.get("lon")

        if elat is None or elng is None:
            continue

        dist = _haversine_miles(lat, lng, elat, elng)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [elng, elat]},
            "properties": {
                "name": tags.get("name") or wtype.capitalize(),
                "water_type": wtype,
                "distance_mi": round(dist, 2),
            },
        })

    features.sort(key=lambda f: f["properties"]["distance_mi"])
    features = features[:30]

    geojson = {"type": "FeatureCollection", "features": features}

    if not features:
        message = f"No water sources found within {radius_miles:.0f} mi"
    else:
        nearest = features[0]["properties"]
        message = (
            f"{len(features)} sources within {radius_miles:.0f} mi · "
            f"nearest: {nearest['name']} ({nearest['distance_mi']} mi)"
        )

    result = {
        "count": len(features),
        "message": message,
        "nearest_mi": features[0]["properties"]["distance_mi"] if features else None,
        "geojson": geojson,
    }
    WATER_CACHE.set(cache_key, result)
    return result
