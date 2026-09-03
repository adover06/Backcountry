"""Trailhead and access-point enrichment.

Attaches the nearest trailhead, parking, water and campground to each trail, from
two public-domain federal sources:

* **NPS Public POIs** — 448 trailheads, 408 parking lots, 62 water points. Park
  units only.
* **USFS INFRA recreation sites** (`usfs_rec.py`) — 625 trailheads and 1,310
  camping sites across the national forests, which NPS does not cover and which
  is where most of this index's 8,162 USFS trails actually are.

Adding the second source is what moved trailhead coverage off the floor: NPS alone
reached 990 of 10,694 trails, because it can only speak for park units.

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
from .osm_access import OSM_ACCESS_PATH
from .osm_access import load_saved as load_osm_access
from .usfs_rec import load_records as load_usfs_records

_BASE_DIR = Path(__file__).resolve().parent.parent
ACCESS_PATH = _BASE_DIR / "data" / "access_points.json"

# Bumped when the set of sources changes, so an index built against the old source
# list rebuilds instead of silently serving partial data from cache.
ACCESS_CACHE_VERSION = 4

# A trailhead further than this from the trail is not that trail's trailhead.
TRAILHEAD_RADIUS_MI = 0.5
PARKING_RADIUS_MI = 0.5
WATER_RADIUS_MI = 0.35

# Camping is the exception to the tight radii. A campground a mile off the trail is
# still the place you sleep to walk it, whereas a trailhead a mile away is simply a
# different trail's trailhead.
CAMPGROUND_RADIUS_MI = 1.0

_KIND_RADIUS = {
    "trailhead": TRAILHEAD_RADIUS_MI,
    "parking": PARKING_RADIUS_MI,
    "water": WATER_RADIUS_MI,
    "campground": CAMPGROUND_RADIUS_MI,
    "day_use": PARKING_RADIUS_MI,
    "staging": PARKING_RADIUS_MI,
    "picnic": PARKING_RADIUS_MI,
    "snowpark": PARKING_RADIUS_MI,
    "visitor_center": CAMPGROUND_RADIUS_MI,
    # OSM-only kinds.
    "shelter": CAMPGROUND_RADIUS_MI,
    "backcountry_camp": CAMPGROUND_RADIUS_MI,
}


def load_access_points(use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """NPS + USFS + OSM access points, cached to data/access_points.json.

    The cache is an envelope carrying its version. A bare list is the pre-USFS
    format and is treated as stale, so an existing checkout picks the new sources
    up on the next build without anyone having to know to delete a file.

    The version alone is not sufficient. `osm_access.py` runs on its own schedule —
    it is a ~1h statewide sweep, deliberately not part of a build — so a cache can
    be written at the current version *before* that file exists and would then keep
    answering without OSM data forever. Comparing mtimes catches exactly that.
    """
    if use_cache and ACCESS_PATH.exists():
        try:
            payload = json.loads(ACCESS_PATH.read_text())
            fresh = (
                isinstance(payload, dict)
                and payload.get("version") == ACCESS_CACHE_VERSION
            )
            if fresh and OSM_ACCESS_PATH.exists():
                if OSM_ACCESS_PATH.stat().st_mtime > ACCESS_PATH.stat().st_mtime:
                    fresh = False
                    if verbose:
                        print("  OSM access sweep is newer than the cache; rebuilding")
            if fresh:
                records = payload["records"]
                if verbose:
                    print(f"  loaded {len(records)} cached access points")
                return records
            if payload.get("version") != ACCESS_CACHE_VERSION and verbose:
                print("  access cache predates the current sources; rebuilding")
        except Exception:
            pass

    nps = [r for r in poi_records(fetch_pois(verbose=verbose)) if r["kind"] in _KIND_RADIUS]
    usfs = [r for r in load_usfs_records(verbose=verbose) if r["kind"] in _KIND_RADIUS]
    # Optional third source: present only once `python -m pipeline.osm_access` has
    # run its statewide sweep. Absent, the join is still correct, just thinner.
    osm = [r for r in load_osm_access() if r["kind"] in _KIND_RADIUS]
    records = nps + usfs + osm

    ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCESS_PATH.write_text(
        json.dumps({"version": ACCESS_CACHE_VERSION, "records": records})
    )
    if verbose:
        print(
            f"  {len(records)} access points saved "
            f"({len(nps)} NPS, {len(usfs)} USFS, {len(osm)} OSM)"
        )
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
            entry = {
                "name": hit.get("name"),
                "distance_mi": hit["distance_mi"],
                "lat": hit["lat"],
                "lng": hit["lng"],
            }
            # Fee, water, restrooms, season and a reservation link matter as much
            # as the coordinate for anything you sleep at or park in.
            for extra in ("source", "site_type", "details"):
                if hit.get(extra):
                    entry[extra] = hit[extra]
            nearest[kind] = entry

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

    if verbose:
        counts: dict[str, int] = {}
        for trail in trails:
            for kind in trail.get("access") or {}:
                counts[kind] = counts.get(kind, 0) + 1
        total = len(trails)
        for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:16} {count:6}/{total}  {count / total:5.1%}")
    return trails


if __name__ == "__main__":
    import collections

    records = load_access_points()
    print(f"\n{len(records)} access points")
    for kind, count in collections.Counter(r["kind"] for r in records).most_common():
        print(f"  {kind:12} {count}")
