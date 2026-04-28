"""Fire summary using NIFC perimeters and incidents (ArcGIS)."""

from __future__ import annotations

import math
import os
import requests
from datetime import datetime, timezone

from .cache import TTLCache, env_ttl_seconds


NIFC_PERIMETERS = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Interagency_Perimeters/FeatureServer/0/query"
FIRE_FEED_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("FIRE_CACHE_TTL_SECONDS", 21600))

# How far back to include fire perimeters. Default 365 days; set FIRE_HISTORY_DAYS=0 for no limit.
FIRE_HISTORY_DAYS = int(os.environ.get("FIRE_HISTORY_DAYS", "3650"))


def _distance_miles(lat1, lng1, lat2, lng2) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _days_ago(epoch_ms: int) -> int:
    if not epoch_ms:
        return -1
    ts = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days


def get_fire_summary(route: dict, radius_miles: float = 50.0) -> dict:
    lat = lng = None
    route_midpoint = route.get("midpoint")
    if route_midpoint:
        lat, lng = route_midpoint[0], route_midpoint[1]

    try:
        geo = FIRE_FEED_CACHE.get("wfigs-perimeters")
        if geo is None:
            params = {
                "where": "1=1",
                "outFields": "*",
                "f": "geojson",
                "resultRecordCount": 2000,
                "returnGeometry": "true",
                "outSR": 4326,
            }
            r = requests.get(NIFC_PERIMETERS, params=params, timeout=12)
            r.raise_for_status()
            geo = r.json()
            FIRE_FEED_CACHE.set("wfigs-perimeters", geo)

        filtered_features = []
        for feat in geo.get("features", []):
            props = feat.get("properties") or {}
            geom = feat.get("geometry")

            if lat and lng and geom:
                geom_type = geom.get("type")
                coords = geom.get("coordinates")
                feat_lat = feat_lng = None
                if geom_type == "Polygon" and coords and coords[0]:
                    pt = coords[0][0]
                    if len(pt) >= 2:
                        feat_lng, feat_lat = pt[0], pt[1]
                elif geom_type == "MultiPolygon" and coords and coords[0] and coords[0][0]:
                    pt = coords[0][0][0]
                    if len(pt) >= 2:
                        feat_lng, feat_lat = pt[0], pt[1]

                if feat_lat is not None:
                    dist = _distance_miles(lat, lng, feat_lat, feat_lng)
                    if dist > radius_miles:
                        continue

            days = _days_ago(props.get("Irwin_ModifiedOnDateTime") or props.get("poly_DateCurrent"))
            if days >= 0:
                if FIRE_HISTORY_DAYS > 0 and days > FIRE_HISTORY_DAYS:
                    continue
                if days <= 7:
                    tag = "active"
                elif days <= 30:
                    tag = "recent"
                else:
                    tag = "older"
                props["recency_tag"] = tag
                props["days_since_update"] = days
                feat["properties"] = props
                filtered_features.append(feat)

        filtered_geo = {
            "type": "FeatureCollection",
            "features": filtered_features,
        }

        return {"perimeters": filtered_geo, "count": len(filtered_features)}
    except Exception as e:
        return {"error": str(e), "perimeters": None}
