"""Trailhead and access-point enrichment.

Attaches the nearest trailhead, parking and drinking water to each trail, from the
NPS Public POIs layer (448 trailheads, 408 parking lots, 62 water points in
California — public domain, no key).

Two reasons this matters beyond a label:

* "Where do I start?" is the first practical question about any hike, and the trail
  geometry alone does not answer it.
* Trailheads are the natural anchors for `trail_graph`. A composed hike is
  trailhead → destination → back, so these points are what the router starts from.

As everywhere else in this pipeline, absence is recorded as absence: a trail with no
trailhead within the search radius gets None, never a nearest-anything at any
distance.
"""

from __future__ import annotations

import json
from pathlib import Path

from .nps import fetch_pois, poi_records
from .spatial import PointGrid

_BASE_DIR = Path(__file__).resolve().parent.parent
ACCESS_PATH = _BASE_DIR / "data" / "access_points.json"

# A trailhead further than this from the trail is not that trail's trailhead.
TRAILHEAD_RADIUS_MI = 0.5
PARKING_RADIUS_MI = 0.5
WATER_RADIUS_MI = 0.35

_KIND_RADIUS = {
    "trailhead": TRAILHEAD_RADIUS_MI,
    "parking": PARKING_RADIUS_MI,
    "water": WATER_RADIUS_MI,
}


def load_access_points(use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """NPS access points, cached to data/access_points.json."""
    if use_cache and ACCESS_PATH.exists():
        try:
            records = json.loads(ACCESS_PATH.read_text())
            if verbose:
                print(f"  loaded {len(records)} cached access points")
            return records
        except Exception:
            pass

    records = [r for r in poi_records(fetch_pois(verbose=verbose)) if r["kind"] in _KIND_RADIUS]
    ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCESS_PATH.write_text(json.dumps(records))
    if verbose:
        print(f"  {len(records)} access points saved")
    return records


def build_grid(records: list[dict]) -> PointGrid:
    grid = PointGrid()
    for record in records:
        grid.add(record["lat"], record["lng"], {k: v for k, v in record.items() if k not in ("lat", "lng")})
    return grid


def _coords(geometry: dict | None) -> list[list[float]]:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return coords
    if gtype == "MultiLineString":
        return [c for line in coords for c in line]
    return []


def enrich_trail(trail: dict, grid: PointGrid, geometry: dict | None, stride: int = 4) -> dict:
    """Attach the nearest trailhead / parking / water to one trail."""
    coords = _coords(geometry)
    if not coords:
        trail["access"] = None
        return trail

    widest = max(_KIND_RADIUS.values())
    hits = grid.near_path(coords, widest, stride=stride)

    nearest: dict[str, dict] = {}
    for hit in hits.values():
        kind = hit.get("kind")
        limit = _KIND_RADIUS.get(kind)
        if limit is None or hit["distance_mi"] > limit:
            continue
        current = nearest.get(kind)
        if current is None or hit["distance_mi"] < current["distance_mi"]:
            nearest[kind] = {
                "name": hit.get("name"),
                "distance_mi": hit["distance_mi"],
                "lat": hit["lat"],
                "lng": hit["lng"],
            }

    # {} rather than None: we looked and found nothing, which is different from
    # never having looked.
    trail["access"] = nearest
    return trail


def enrich_all(trails: list[dict], geometries: dict, verbose: bool = True) -> list[dict]:
    records = load_access_points(verbose=verbose)
    grid = build_grid(records)
    if verbose:
        print(f"  indexed {len(grid)} access points")

    for index, trail in enumerate(trails, start=1):
        entry = geometries.get(trail["id"]) or {}
        enrich_trail(trail, grid, entry.get("geometry"))
        if verbose and index % 3000 == 0:
            print(f"  joined {index}/{len(trails)}")

    with_th = sum(1 for t in trails if (t.get("access") or {}).get("trailhead"))
    if verbose:
        print(f"  {with_th} trails have a trailhead within {TRAILHEAD_RADIUS_MI} mi")
    return trails


if __name__ == "__main__":
    import collections

    records = load_access_points()
    print(f"\n{len(records)} access points")
    for kind, count in collections.Counter(r["kind"] for r in records).most_common():
        print(f"  {kind:12} {count}")
