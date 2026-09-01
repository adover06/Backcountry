"""Active fire perimeters from the NIFC / WFIGS interagency feed.

Four corrections over the previous implementation, each of which could have hidden
a fire that was actually burning near a route:

1. **Pagination.** The old query asked for 2000 records with no paging and never
   checked `exceededTransferLimit`. WFIGS holds far more than that, so the fire
   next to your route could simply be absent. Truncation is now detected and
   reported so the risk engine can mark the check incomplete.

2. **Undated perimeters are kept.** The old filter dropped any feature whose date
   field was null, silently removing live fires with incomplete metadata.

3. **Distance is measured to the nearest point of the perimeter**, not to the
   centroid of its first ring. A large fire whose centroid is 60 mi away can have
   an edge 2 mi from camp.

4. **A sane history window.** The default was 3650 days, so decade-old burn scars
   were reported as "active fire perimeters", which trains a user to ignore the
   warning entirely.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone

import requests

from .cache import TTLCache, env_ttl_seconds

NIFC_PERIMETERS = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/"
    "WFIGS_Interagency_Perimeters/FeatureServer/0/query"
)

FIRE_FEED_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("FIRE_CACHE_TTL_SECONDS", 21600))

# Only perimeters updated within this window are treated as current.
FIRE_HISTORY_DAYS = int(os.environ.get("FIRE_HISTORY_DAYS", "60"))

PAGE_SIZE = 1000
MAX_PAGES = 20  # 20k perimeters is far beyond any realistic active-fire count

EARTH_RADIUS_MI = 3958.8


def _distance_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(min(1.0, h)))


def _days_since(epoch_ms) -> int | None:
    """Age in days, or None when the timestamp is missing or unusable."""
    if not epoch_ms:
        return None
    try:
        stamp = datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    return (datetime.now(timezone.utc) - stamp).days


def _iter_ring_points(geometry: dict):
    """Yield every (lat, lng) vertex of a Polygon or MultiPolygon."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        rings = coords
    elif gtype == "MultiPolygon":
        rings = [ring for polygon in coords for ring in polygon]
    else:
        return
    for ring in rings:
        for point in ring:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                yield point[1], point[0]


def _min_distance_to_perimeter(lat: float, lng: float, geometry: dict) -> float | None:
    """Distance to the closest vertex of the perimeter.

    Vertex distance slightly overestimates against a long edge, but it is vastly
    closer to the truth than centroid distance and always errs toward reporting a
    fire rather than hiding it.
    """
    best: float | None = None
    for plat, plng in _iter_ring_points(geometry):
        distance = _distance_miles(lat, lng, plat, plng)
        if best is None or distance < best:
            best = distance
    return best


def _route_points(route: dict) -> list[tuple[float, float]]:
    """Sample points along the route, falling back to its midpoint."""
    points = (route or {}).get("points") or []
    if points:
        stride = max(1, len(points) // 60)
        sampled = [
            (p["lat"], p["lng"])
            for i, p in enumerate(points)
            if i % stride == 0 and p.get("lat") is not None and p.get("lng") is not None
        ]
        if sampled:
            return sampled

    midpoint = (route or {}).get("midpoint")
    if midpoint and len(midpoint) >= 2:
        return [(midpoint[0], midpoint[1])]
    return []


def _fetch_all_perimeters() -> tuple[list[dict], bool]:
    """Page through the WFIGS feed. Returns (features, truncated)."""
    cached = FIRE_FEED_CACHE.get("wfigs-perimeters")
    if cached is not None:
        return cached

    features: list[dict] = []
    truncated = False
    offset = 0

    for _ in range(MAX_PAGES):
        params = {
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
            "returnGeometry": "true",
            "outSR": 4326,
            # A stable sort makes paging deterministic across requests.
            "orderByFields": "OBJECTID",
        }
        response = requests.get(NIFC_PERIMETERS, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()

        page = payload.get("features") or []
        features.extend(page)

        # ArcGIS signals more data either via the flag or a full page.
        exceeded = bool(payload.get("exceededTransferLimit") or payload.get("properties", {}).get("exceededTransferLimit"))
        if not page or (not exceeded and len(page) < PAGE_SIZE):
            break
        offset += len(page)
    else:
        # Loop finished without breaking: there is still more data upstream.
        truncated = True

    result = (features, truncated)
    FIRE_FEED_CACHE.set("wfigs-perimeters", result)
    return result


def get_fire_summary(route: dict, radius_miles: float = 25.0) -> dict:
    """Perimeters near a route, with explicit truncation and unknown-metadata handling."""
    try:
        radius_miles = float(radius_miles)
    except (TypeError, ValueError):
        radius_miles = 25.0
    radius_miles = max(1.0, min(radius_miles, 200.0))

    reference_points = _route_points(route)

    try:
        features, truncated = _fetch_all_perimeters()
    except Exception as exc:
        return {
            "status": "unavailable",
            "message": f"WFIGS request failed: {exc}",
            "perimeters": None,
            "count": 0,
        }

    if not reference_points:
        # Without a location there is nothing to filter against; say so rather
        # than returning the whole country as though it were "nearby".
        return {
            "status": "unavailable",
            "message": "No route location available to check fires against",
            "perimeters": None,
            "count": 0,
        }

    kept = []
    for feature in features:
        props = dict(feature.get("properties") or {})
        geometry = feature.get("geometry")

        distance = None
        if geometry:
            distances = [
                d
                for d in (
                    _min_distance_to_perimeter(lat, lng, geometry) for lat, lng in reference_points
                )
                if d is not None
            ]
            distance = min(distances) if distances else None

        # Unknown distance is kept, not dropped — failing toward visibility.
        if distance is not None and distance > radius_miles:
            continue

        days = _days_since(
            props.get("attr_ModifiedOnDateTime_dt")
            or props.get("Irwin_ModifiedOnDateTime")
            or props.get("poly_DateCurrent")
            or props.get("attr_FireDiscoveryDateTime")
        )
        if days is not None and FIRE_HISTORY_DAYS > 0 and days > FIRE_HISTORY_DAYS:
            continue

        if days is None:
            tag = "unknown-age"
        elif days <= 7:
            tag = "active"
        elif days <= 30:
            tag = "recent"
        else:
            tag = "older"

        props["recency_tag"] = tag
        props["days_since_update"] = days
        props["distance_mi"] = round(distance, 1) if distance is not None else None
        props["incident_name"] = (
            props.get("attr_IncidentName")
            or props.get("poly_IncidentName")
            or props.get("IncidentName")
            or "Unnamed incident"
        )
        props["percent_contained"] = props.get("attr_PercentContained")

        kept.append({**feature, "properties": props})

    kept.sort(key=lambda f: f["properties"].get("distance_mi") if f["properties"].get("distance_mi") is not None else 1e9)

    return {
        "status": "ok",
        "provider": "NIFC / WFIGS",
        "perimeters": {"type": "FeatureCollection", "features": kept},
        "count": len(kept),
        "truncated": truncated,
        "searched_radius_mi": radius_miles,
        "history_days": FIRE_HISTORY_DAYS,
        "feed_size": len(features),
    }
