"""Fire summary using NIFC perimeters and incidents (ArcGIS)."""

from __future__ import annotations

import requests
from datetime import datetime, timezone


NIFC_PERIMETERS = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/USFS_Active_Fire_Perimeters/FeatureServer/0/query"


def _days_ago(epoch_ms: int) -> int:
    if not epoch_ms:
        return -1
    ts = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days


def get_fire_summary(route: dict) -> dict:
    # For MVP, pull perimeters and return as layer (no spatial clipping yet)
    try:
        params = {
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
        }
        r = requests.get(NIFC_PERIMETERS, params=params, timeout=12)
        r.raise_for_status()
        geo = r.json()

        # Add recency tag where possible
        for feat in geo.get("features", []):
            props = feat.get("properties") or {}
            days = _days_ago(props.get("Irwin_ModifiedOnDateTime") or props.get("poly_DateCurrent"))
            if days >= 0:
                if days <= 7:
                    tag = "active"
                elif days <= 30:
                    tag = "recent"
                else:
                    tag = "older"
                props["recency_tag"] = tag
                props["days_since_update"] = days
                feat["properties"] = props

        return {"perimeters": geo}
    except Exception as e:
        return {"error": str(e), "perimeters": None}
