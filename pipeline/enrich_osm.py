"""Enrich trails with OpenStreetMap data.

Two jobs:

1. **Scenery tags.** Spatially join OSM points of interest (peaks, waterfalls, lakes,
   hot springs, viewpoints) to trail geometry, so "show me hikes with a waterfall"
   is a real spatial fact rather than a guess from the trail's name.

2. **Coverage.** The USFS feed is National Forest only — it has no Yosemite, Sequoia,
   or state park trails. OSM does. `fetch_hiking_ways` pulls named hiking paths so
   those gaps can be filled.

POIs are fetched once per bbox tile and cached on disk, then joined offline. Querying
Overpass per trail would be thousands of requests; this is a few dozen, once.

Overpass is the default because it needs no setup. For a full rebuild, a Geofabrik
extract (california-latest.osm.pbf) removes rate limits entirely — see README notes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .spatial import PointGrid, bbox_tiles

# The public Overpass instances rate-limit and time out unpredictably. Rotate across
# mirrors rather than failing a tile the first time one of them is busy.
OVERPASS_MIRRORS = [
    url.strip()
    for url in os.environ.get(
        "OVERPASS_URLS",
        ",".join(
            [
                "https://overpass.kumi.systems/api/interpreter",
                "https://overpass-api.de/api/interpreter",
                "https://overpass.private.coffee/api/interpreter",
                "https://overpass.osm.jp/api/interpreter",
            ]
        ),
    ).split(",")
    if url.strip()
]

_CACHE_DIR = Path(os.environ.get("OSM_CACHE_DIR", Path(__file__).resolve().parent.parent / ".osm_cache"))

# California, generously bounded.
CALIFORNIA_BBOX = (-124.5, 32.5, -114.1, 42.1)

TILE_STEP_DEG = float(os.environ.get("OSM_TILE_STEP", "1.0"))

# Be polite to the public Overpass instance.
REQUEST_PAUSE_SECONDS = float(os.environ.get("OSM_PAUSE", "2.0"))

# How close a feature must be to the trail to count as "on" it.
JOIN_RADIUS_MI = {
    # Tight: you have to be essentially on it for it to be a feature of the hike.
    "waterfall": 0.25,
    "hot_spring": 0.25,
    "spring": 0.2,
    "viewpoint": 0.25,
    "cave": 0.25,
    "arch": 0.25,
    "pillar": 0.25,
    # Wider: large features are a feature of the hike from further away, and their
    # recorded point is a centroid rather than the part you walk past.
    "lake": 0.35,
    "peak": 0.5,
    "pass": 0.4,
    "glacier": 0.5,
    "cliff": 0.3,
    "basin": 0.5,
    "ridge": 0.5,
    "island": 0.5,
    "beach": 0.3,
    "bay": 0.4,
    "marsh": 0.3,
}

DEFAULT_JOIN_RADIUS_MI = 0.25


def _poi_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL for the scenery features that matter to hikers."""
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    box = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:180];
(
  node["natural"="peak"]({box});
  node["natural"="volcano"]({box});
  node["waterway"="waterfall"]({box});
  node["natural"="hot_spring"]({box});
  node["natural"="spring"]({box});
  node["tourism"="viewpoint"]({box});
  node["natural"="arch"]({box});
  node["natural"="cave_entrance"]({box});
  way["natural"="water"]["water"~"lake|reservoir|pond"]({box});
  way["natural"="glacier"]({box});
);
out center tags;
"""


def _classify_poi(tags: dict) -> str | None:
    """Map OSM tags to the feature vocabulary used for filtering."""
    natural = tags.get("natural", "")
    waterway = tags.get("waterway", "")
    tourism = tags.get("tourism", "")

    if natural in ("peak", "volcano"):
        return "peak"
    if waterway == "waterfall":
        return "waterfall"
    if natural == "hot_spring":
        return "hot_spring"
    if natural == "spring":
        return "spring"
    if tourism == "viewpoint":
        return "viewpoint"
    if natural == "arch":
        return "arch"
    if natural == "cave_entrance":
        return "cave"
    if natural == "glacier":
        return "glacier"
    if natural == "water":
        return "lake"
    return None


def _cache_path(kind: str, bbox: tuple[float, float, float, float]) -> Path:
    name = "_".join(f"{value:.2f}" for value in bbox).replace("-", "m")
    return _CACHE_DIR / f"{kind}_{name}.json"


def _overpass(query: str, attempts_per_mirror: int = 2) -> dict:
    """Run an Overpass query, rotating mirrors until one answers.

    Raises the last error only after every mirror has been tried, so a single busy
    instance never causes a tile to be silently recorded as empty.
    """
    last_error: Exception | None = None
    for attempt in range(attempts_per_mirror):
        for mirror in OVERPASS_MIRRORS:
            try:
                response = requests.post(
                    mirror,
                    data={"data": query},
                    timeout=240,
                    headers={"User-Agent": "BackcountryPlanner/1.0 (trail discovery)"},
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                # Back off a little before moving to the next mirror.
                time.sleep(1.0 + attempt * 2.0)
    raise RuntimeError(f"all Overpass mirrors failed: {last_error}")


def fetch_pois(
    bbox: tuple[float, float, float, float] = CALIFORNIA_BBOX,
    step_deg: float = TILE_STEP_DEG,
    verbose: bool = True,
) -> list[dict]:
    """Fetch scenery POIs across a bbox, caching each tile. Returns flat POI records."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tiles = bbox_tiles(bbox, step_deg)
    pois: list[dict] = []

    for index, tile in enumerate(tiles, start=1):
        path = _cache_path("poi", tile)
        if path.exists():
            try:
                elements = json.loads(path.read_text())
            except Exception:
                elements = None
        else:
            elements = None

        if elements is None:
            try:
                data = _overpass(_poi_query(tile))
                elements = data.get("elements", [])
                path.write_text(json.dumps(elements))
                if verbose:
                    print(f"  [{index}/{len(tiles)}] fetched {len(elements):>5} elements {tile}")
                time.sleep(REQUEST_PAUSE_SECONDS)
            except Exception as exc:
                # A failed tile is recorded as a miss, never as "no features here".
                if verbose:
                    print(f"  [{index}/{len(tiles)}] FAILED {tile}: {exc}")
                continue
        elif verbose and index % 20 == 0:
            print(f"  [{index}/{len(tiles)}] cached")

        for element in elements:
            kind = _classify_poi(element.get("tags") or {})
            if not kind:
                continue
            if element.get("type") == "node":
                lat, lng = element.get("lat"), element.get("lon")
            else:
                center = element.get("center") or {}
                lat, lng = center.get("lat"), center.get("lon")
            if lat is None or lng is None:
                continue
            tags = element.get("tags") or {}
            pois.append(
                {
                    "id": f"{element.get('type')}/{element.get('id')}",
                    "kind": kind,
                    "name": tags.get("name"),
                    "ele_m": tags.get("ele"),
                    "lat": float(lat),
                    "lng": float(lng),
                }
            )

    return pois


def build_poi_grid(pois: list[dict]) -> PointGrid:
    grid = PointGrid()
    for poi in pois:
        grid.add(poi["lat"], poi["lng"], {k: v for k, v in poi.items() if k not in ("lat", "lng")})
    return grid


def _geometry_coords(geometry: dict) -> list[list[float]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return coords
    if gtype == "MultiLineString":
        return [c for line in coords for c in line]
    return []


def enrich_trail(trail: dict, grid: PointGrid, stride: int = 3) -> dict:
    """Attach nearby scenery features to one trail.

    Sets `features` to a list of tag names and `nearby` to the detailed hits. An
    empty list means "we looked and found nothing", which is different from the
    None that `normalize` leaves when enrichment has not run.
    """
    coords = _geometry_coords(trail.get("geometry") or {})
    if not coords:
        trail["features"] = []
        trail["nearby"] = []
        return trail

    # Query at the widest radius once, then filter per-kind by its own threshold.
    widest = max(JOIN_RADIUS_MI.values())
    hits = grid.near_path(coords, widest, stride=stride)

    nearby = []
    for hit in hits.values():
        limit = JOIN_RADIUS_MI.get(hit["kind"], DEFAULT_JOIN_RADIUS_MI)
        if hit["distance_mi"] <= limit:
            nearby.append(
                {
                    "kind": hit["kind"],
                    "name": hit.get("name"),
                    "distance_mi": hit["distance_mi"],
                }
            )

    nearby.sort(key=lambda item: item["distance_mi"])
    trail["nearby"] = nearby[:25]
    trail["features"] = sorted({item["kind"] for item in nearby})
    return trail


def enrich_all(trails: list[dict], pois: list[dict], verbose: bool = True) -> list[dict]:
    grid = build_poi_grid(pois)
    if verbose:
        print(f"  indexed {len(grid)} POIs")
    for index, trail in enumerate(trails, start=1):
        enrich_trail(trail, grid)
        if verbose and index % 1000 == 0:
            print(f"  enriched {index}/{len(trails)}")
    return trails


# ── Coverage: hiking ways OSM has and the USFS feed does not ──────────────────


def _hiking_way_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    box = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:240];
(
  way["highway"~"^(path|footway|track)$"]["name"]({box});
);
out geom tags;
"""


def fetch_hiking_ways(
    bbox: tuple[float, float, float, float],
    step_deg: float = 0.5,
    verbose: bool = True,
) -> list[dict]:
    """Fetch named OSM hiking ways in a bbox.

    Used to cover national and state parks that the USFS feed omits. Kept separate
    from POI fetching because way geometry is far heavier than point data, so this
    is normally run over targeted park bboxes rather than the whole state.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tiles = bbox_tiles(bbox, step_deg)
    ways: list[dict] = []

    for index, tile in enumerate(tiles, start=1):
        path = _cache_path("ways", tile)
        if path.exists():
            try:
                elements = json.loads(path.read_text())
            except Exception:
                elements = []
        else:
            try:
                data = _overpass(_hiking_way_query(tile))
                elements = data.get("elements", [])
                path.write_text(json.dumps(elements))
                if verbose:
                    print(f"  [{index}/{len(tiles)}] {len(elements):>5} ways {tile}")
                time.sleep(REQUEST_PAUSE_SECONDS)
            except Exception as exc:
                if verbose:
                    print(f"  [{index}/{len(tiles)}] FAILED {tile}: {exc}")
                continue

        for element in elements:
            geometry = element.get("geometry") or []
            if len(geometry) < 2:
                continue
            tags = element.get("tags") or {}
            ways.append(
                {
                    "osm_id": f"way/{element.get('id')}",
                    "name": tags.get("name"),
                    "sac_scale": tags.get("sac_scale"),
                    "trail_visibility": tags.get("trail_visibility"),
                    "surface": tags.get("surface"),
                    "coordinates": [[p["lon"], p["lat"]] for p in geometry],
                }
            )

    return ways


if __name__ == "__main__":
    sample = fetch_pois(bbox=(-119.7, 37.6, -119.4, 37.8), step_deg=0.5)
    print(f"\nYosemite-area POIs: {len(sample)}")
    from collections import Counter

    for kind, count in Counter(p["kind"] for p in sample).most_common():
        print(f"  {kind:12} {count}")
