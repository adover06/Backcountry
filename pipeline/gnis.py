"""Named natural features from GNIS (USGS Geographic Names Information System).

This is the scenery source that unblocked the enrichment stage. The OSM route via
Overpass kept timing out and rate-limiting; GNIS is a plain ArcGIS service with no
key, no throttling, and an exact `state_alpha` filter, so California can be pulled
precisely rather than by a bounding box that leaks into Nevada and Oregon.

It is also better than OSM for the features hikers actually search on — summits,
passes, and named lakes are the Board on Geographic Names' core competency.

Public domain (US federal work).

Layers used:
    5  Landforms                  summits, gaps/passes, ridges, basins, cliffs
    7  Other Hydrographic         lakes, reservoirs, springs, falls, glaciers
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

GEONAMES_BASE = "https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer"
LANDFORM_LAYER = 5
HYDRO_LAYER = 7

_CACHE_DIR = Path(os.environ.get("GNIS_CACHE_DIR", Path(__file__).resolve().parent.parent / ".gnis_cache"))

PAGE_SIZE = 1000
MAX_PAGES = 60

# GNIS feature class -> our filter vocabulary. Classes not listed are ignored:
# "Valley" and "Flat" are the most common landforms but are not things anyone
# chooses a hike for.
FEATURE_CLASS_MAP = {
    # Landforms
    "Summit": "peak",
    "Gap": "pass",
    "Ridge": "ridge",
    "Basin": "basin",
    "Cliff": "cliff",
    "Arch": "arch",
    "Cave": "cave",
    "Pillar": "pillar",
    "Island": "island",
    "Beach": "beach",
    "Range": "ridge",
    # Hydrographic
    "Lake": "lake",
    "Reservoir": "lake",
    "Spring": "spring",
    "Falls": "waterfall",
    "Glacier": "glacier",
    "Bay": "bay",
    "Swamp": "marsh",
}

# GNIS records hot springs as plain "Spring"; the name is the only signal.
_HOT_SPRING_HINT = "hot spring"


def _fetch_layer(layer_id: int, state: str = "CA", verbose: bool = True) -> list[dict]:
    """Page one gazetteer layer for a state."""
    url = f"{GEONAMES_BASE}/{layer_id}/query"
    features: list[dict] = []
    offset = 0

    for page in range(MAX_PAGES):
        params = {
            # Exact state filter beats a bbox: no Nevada or Oregon leakage.
            "where": f"state_alpha='{state}'",
            "outFields": "gaz_id,gaz_name,gaz_featureclass,state_alpha,county_name",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
        }
        response = requests.get(url, params=params, timeout=120)
        response.raise_for_status()
        payload = response.json()

        batch = payload.get("features") or []
        features.extend(batch)
        if verbose and batch:
            print(f"    layer {layer_id} page {page + 1}: +{len(batch)} (total {len(features)})")

        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
        time.sleep(0.3)

    return features


def _coords(geometry: dict) -> tuple[float, float] | None:
    """Extract (lng, lat) from either Esri geometry shape this service returns.

    Layer 7 (hydrographic) returns points as {'x', 'y'}; layer 5 (landforms)
    returns multipoint as {'points': [[lng, lat], ...]}. Reading only x/y silently
    dropped all 23,073 landforms — every summit and pass in California.
    """
    if geometry.get("x") is not None and geometry.get("y") is not None:
        return float(geometry["x"]), float(geometry["y"])

    points = geometry.get("points") or []
    valid = [p for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not valid:
        return None
    # Multipoint features (a ridge, a range) get their centroid.
    return (
        sum(p[0] for p in valid) / len(valid),
        sum(p[1] for p in valid) / len(valid),
    )


def _to_records(raw: list[dict]) -> list[dict]:
    """Convert Esri features into the POI shape the scenery grid expects."""
    records = []
    skipped_geometry = 0

    for feature in raw:
        attributes = feature.get("attributes") or {}
        position = _coords(feature.get("geometry") or {})
        if position is None:
            skipped_geometry += 1
            continue
        lng, lat = position

        feature_class = attributes.get("gaz_featureclass")
        kind = FEATURE_CLASS_MAP.get(feature_class)
        if not kind:
            continue

        name = (attributes.get("gaz_name") or "").strip() or None
        if kind == "spring" and name and _HOT_SPRING_HINT in name.lower():
            kind = "hot_spring"

        records.append(
            {
                "id": f"gnis:{attributes.get('gaz_id')}",
                "kind": kind,
                "name": name,
                "county": attributes.get("county_name"),
                "lat": float(lat),
                "lng": float(lng),
            }
        )

    if skipped_geometry:
        print(f"    warning: {skipped_geometry} records had unreadable geometry")
    return records


def fetch_features(state: str = "CA", use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """All usable GNIS scenery features for a state."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / f"gnis_{state.lower()}.json"

    if use_cache and cache.exists():
        try:
            records = json.loads(cache.read_text())
            if verbose:
                print(f"  loaded {len(records)} cached GNIS features")
            return records
        except Exception:
            pass

    raw: list[dict] = []
    for layer_id in (LANDFORM_LAYER, HYDRO_LAYER):
        if verbose:
            print(f"  fetching GNIS layer {layer_id}…")
        raw.extend(_fetch_layer(layer_id, state=state, verbose=verbose))

    records = _to_records(raw)
    cache.write_text(json.dumps(records))
    if verbose:
        print(f"  {len(records)} usable features from {len(raw)} gazetteer records")
    return records


if __name__ == "__main__":
    import collections

    records = fetch_features()
    print(f"\n{len(records)} California GNIS scenery features")
    for kind, count in collections.Counter(r["kind"] for r in records).most_common():
        print(f"  {kind:12} {count}")
