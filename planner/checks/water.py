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


def _sample_route_points(route_points: list[dict], max_samples: int = 8) -> list[tuple[float, float]]:
    n = len(route_points)
    if n == 0:
        return []
    if n <= max_samples:
        return [(float(p["lat"]), float(p["lng"])) for p in route_points]
    indices = [int(i * (n - 1) / (max_samples - 1)) for i in range(max_samples)]
    return [(float(route_points[i]["lat"]), float(route_points[i]["lng"])) for i in indices]


def get_water_summary(
    lat: float,
    lng: float,
    radius_miles: float = 0.5,
    route_points: list[dict] | None = None,
) -> dict:
    radius_m = int(radius_miles * 1609.34)

    if route_points:
        sample_pts = _sample_route_points(route_points)
    else:
        sample_pts = [(lat, lng)]

    cache_key = json.dumps(
        {"pts": [(round(p[0], 3), round(p[1], 3)) for p in sample_pts], "r": radius_miles},
        sort_keys=True,
    )
    cached = WATER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    around_clauses = "\n".join(
        f'  node["natural"="spring"](around:{radius_m},{p[0]},{p[1]});\n'
        f'  way["natural"="water"]["water"~"lake|pond|reservoir"](around:{radius_m},{p[0]},{p[1]});\n'
        f'  way["waterway"~"stream|river"](around:{radius_m},{p[0]},{p[1]});'
        for p in sample_pts
    )
    query = f"[out:json][timeout:30];\n(\n{around_clauses}\n);\nout center;\n"

    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=30,
            headers={"User-Agent": "BackcountryPlanner/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        result = {"error": str(exc), "count": 0, "message": "Water data unavailable", "geojson": None}
        WATER_CACHE.set(cache_key, result)
        return result

    seen_ids: set[str] = set()
    features = []
    center_lat, center_lng = sample_pts[len(sample_pts) // 2]

    for el in data.get("elements", []):
        el_id = f"{el['type']}/{el['id']}"
        if el_id in seen_ids:
            continue
        seen_ids.add(el_id)

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

        # Distance from nearest sample point
        dist = min(_haversine_miles(p[0], p[1], elat, elng) for p in sample_pts)
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
    features = features[:40]

    geojson = {"type": "FeatureCollection", "features": features}

    if not features:
        message = f"No water sources within {radius_miles} mi of trail"
    else:
        nearest = features[0]["properties"]
        message = (
            f"{len(features)} sources within {radius_miles} mi of trail · "
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
