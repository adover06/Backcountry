"""Named hiking ways from OpenStreetMap, assembled into trails.

**This is the largest coverage gap in the index, and it is structural.** The two
trail sources are federal: USFS National Forest System and NPS Public Trails. Most
of California's population lives where there is no federal land, and the trails
they actually walk belong to regional and county open space districts, state parks,
and city preserves — none of which publish to EDW or the NPS layer.

Measured on the South Bay (36.95..37.45 N, -122.35..-121.55 W):

    trails in the index          15      3 NPS + 12 OSM route relations, 0 USFS
    named OSM path/footway/track 4,415

Sierra Azul, Almaden Quicksilver, Santa Teresa, Castle Rock, Henry Coe and Big Basin
are all in that gap. A trail app showing 15 trails across Silicon Valley is not
wrong about the federal data; it is asking the wrong sources.

**Why this is not just "fetch the ways".** OSM models a trail as however many way
fragments the mappers happened to split it into — junctions, surface changes, county
lines. Emitting one trail per way would produce thousands of quarter-mile stubs, the
exact fragmentation `trail_graph.py` exists to work around. So ways are grouped by
name, chained where they touch, and clustered spatially: two different "Ridge Trail"s
fifty miles apart are two trails, not one trail with two parts.

Data is ODbL. Personal-use and undistributed, so share-alike does not trigger.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from .normalize import _slugify, chain_lines, geometry_length_miles

_BASE_DIR = Path(__file__).resolve().parent.parent
OSM_WAYS_PATH = _BASE_DIR / "data" / "osm_ways.json"

# Default sweep area: the Bay Area, Santa Cruz Mountains and Diablo Range, where the
# federal sources are emptiest and the population is densest. Override with
# OSM_TRAILS_BBOX="west,south,east,north" to sweep somewhere else.
DEFAULT_BBOX = (-122.75, 36.85, -121.20, 38.20)

# `highway=footway` covers pavement as much as trail. These values are the urban
# ones, and including them fills the map with named sidewalks and crossings.
_EXCLUDED_FOOTWAY = {"sidewalk", "crossing", "traffic_island", "access_aisle"}

# Mappers use these as placeholders, not names. "(no access)" and "No Name 026
# Trail" are labels a user should never be shown as a destination.
_PLACEHOLDER_NAME = re.compile(r"^\s*[\[(]|^\s*no\s+name\b|^\s*unnamed\b", re.I)

# Runs of the same name further apart than this are different trails that happen to
# share a name, not one trail with a gap. A real gap — a road crossing, an unmapped
# stretch — is far shorter than this.
SAME_TRAIL_MILES = 2.0

# Below this a record is a fragment, not a hike worth listing on its own.
MIN_TRAIL_MILES = 0.25

EARTH_RADIUS_MI = 3958.7613


def _haversine_mi(a, b) -> float:
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp = p2 - p1
    dl = math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(h)))


def usable(way: dict) -> bool:
    """Is this way a trail, rather than pavement that happens to carry a name?"""
    name = (way.get("name") or "").strip()
    if not name or _PLACEHOLDER_NAME.match(name):
        return False
    tags = way.get("tags") or {}
    if (tags.get("footway") or "").lower() in _EXCLUDED_FOOTWAY:
        return False
    if (tags.get("area") or "").lower() == "yes":
        return False
    # A named driveway or service road is not a hike.
    if (tags.get("service") or "").lower() in {"driveway", "parking_aisle"}:
        return False
    return len(way.get("coordinates") or []) >= 2


def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _cluster_runs(runs: list[list[list[float]]]) -> list[list[list[list[float]]]]:
    """Group chained runs that belong to the same physical trail.

    Union-find over "are these two runs within SAME_TRAIL_MILES of each other",
    compared endpoint to endpoint. Cheap because a single name rarely has more than
    a handful of runs.
    """
    parent = list(range(len(runs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    ends = [(run[0], run[-1]) for run in runs]
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            if find(i) == find(j):
                continue
            near = min(
                _haversine_mi(a, b) for a in ends[i] for b in ends[j]
            )
            if near <= SAME_TRAIL_MILES:
                union(i, j)

    groups: dict[int, list] = {}
    for i, run in enumerate(runs):
        groups.setdefault(find(i), []).append(run)
    return list(groups.values())


def _mode(values: list[str]) -> str | None:
    values = [v for v in values if v]
    if not values:
        return None
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def assemble(ways: list[dict], verbose: bool = True) -> list[dict]:
    """Group named ways into trail records in the index's schema."""
    by_name: dict[str, list[dict]] = {}
    dropped = 0
    for way in ways:
        if not usable(way):
            dropped += 1
            continue
        by_name.setdefault(_norm_name(way["name"]), []).append(way)

    trails: list[dict] = []
    short = 0

    for key, group in by_name.items():
        runs = chain_lines([w["coordinates"] for w in group])
        for cluster in _cluster_runs(runs):
            length = geometry_length_miles(cluster)
            if length < MIN_TRAIL_MILES:
                short += 1
                continue

            name = group[0]["name"]
            lngs = [c[0] for line in cluster for c in line]
            lats = [c[1] for line in cluster for c in line]
            bbox = [min(lngs), min(lats), max(lngs), max(lats)]
            first = cluster[0][0]
            last = cluster[-1][-1]

            # A single chained run whose ends meet is a loop; anything else is a
            # line the walker has to come back along.
            closed = len(cluster) == 1 and _haversine_mi(first, last) < 0.05

            ident = min(w.get("osm_id") or "" for w in group) or key
            trails.append(
                {
                    "id": f"osmw:{_slugify(name)}:{abs(hash((key, round(bbox[0], 3), round(bbox[1], 3)))) % 10**8}",
                    "name": name,
                    "named": True,
                    "slug": _slugify(name),
                    "trail_no": None,
                    "admin_org": _mode([(w.get("tags") or {}).get("operator") for w in group]),
                    "endpoints": None,
                    "network": None,
                    "wikidata": None,
                    "wikipedia": None,
                    "website": None,
                    "trail_type": "TERRA",
                    "source": "OpenStreetMap ways",
                    "osm_ref": ident,
                    "length_miles": round(length, 2),
                    "geometry_length_miles": round(length, 2),
                    "trail_class": None,
                    "trail_class_label": None,
                    "grade": None,
                    "surface": _mode([(w.get("tags") or {}).get("surface") for w in group]),
                    "mgmt_area": _mode([(w.get("tags") or {}).get("operator") for w in group]),
                    "accessibility": None,
                    "activities": {
                        "hiking": {"allowed": True, "restricted": None, "season": None}
                    },
                    "season": None,
                    "route_type": "loop" if closed else "out-and-back",
                    "bbox": bbox,
                    "center": [
                        round((bbox[0] + bbox[2]) / 2, 6),
                        round((bbox[1] + bbox[3]) / 2, 6),
                    ],
                    "segment_count": len(group),
                    "part_count": len(cluster),
                    "geometry": {"type": "MultiLineString", "coordinates": cluster},
                    "elevation": None,
                    "features": None,
                }
            )

    trails.sort(key=lambda t: -t["length_miles"])
    if verbose:
        print(
            f"  {len(trails)} trails from {len(ways)} ways "
            f"({dropped} not trails, {short} under {MIN_TRAIL_MILES} mi)"
        )
    return trails


def _boxes_overlap(a: list[float], b: list[float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def dedupe_against(trails: list[dict], existing: list[dict], verbose: bool = True) -> list[dict]:
    """Drop OSM trails an authoritative source already publishes.

    The agency record is the one to keep: it carries trail class, grade, season and
    allowed uses that OSM does not.

    Matching is by name *and* overlapping bounding boxes, not by distance between
    centres. Two records of the same trail routinely cover slightly different
    extents — one source maps a spur, the other stops at a junction — which moves
    the centres miles apart while the trails plainly occupy the same ground. Centre
    distance is the fallback for records with no bbox.
    """
    known: dict[str, list[dict]] = {}
    for trail in existing:
        key = _norm_name(trail.get("name") or "")
        if not key:
            continue
        known.setdefault(key, []).append(trail)

    kept, dropped = [], 0
    for trail in trails:
        matches = known.get(_norm_name(trail["name"]), [])
        duplicate = False
        for other in matches:
            box = other.get("bbox")
            if box and _boxes_overlap(trail["bbox"], box):
                duplicate = True
                break
            centre = other.get("center")
            if not box and centre and _haversine_mi(trail["center"], centre) <= SAME_TRAIL_MILES:
                duplicate = True
                break
        if duplicate:
            dropped += 1
            continue
        kept.append(trail)

    if verbose:
        print(f"  {len(kept)} kept, {dropped} already covered by an agency source")
    return kept


def fetch_and_save(bbox=None, step_deg: float = 0.5, verbose: bool = True) -> Path:
    """Sweep OSM for named ways and save them for the build stage to assemble.

    Way geometry is heavy — far heavier than the POI and access sweeps — so this is
    run on its own and its output cached, never triggered from inside a build.
    """
    from .enrich_osm import fetch_hiking_ways

    if bbox is None:
        env = os.environ.get("OSM_TRAILS_BBOX")
        bbox = tuple(float(v) for v in env.split(",")) if env else DEFAULT_BBOX

    ways = fetch_hiking_ways(bbox, step_deg=step_deg, verbose=verbose)
    OSM_WAYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OSM_WAYS_PATH.write_text(json.dumps(ways))
    if verbose:
        print(f"  {len(ways)} ways -> {OSM_WAYS_PATH.name} "
              f"({OSM_WAYS_PATH.stat().st_size / 1e6:.1f} MB)")
    return OSM_WAYS_PATH


if __name__ == "__main__":
    import collections

    path = fetch_and_save()
    ways = json.loads(path.read_text())
    trails = assemble(ways)
    print(f"\n{len(trails)} trails assembled")
    for t in trails[:10]:
        print(f"  {t['length_miles']:7.2f} mi  {t['name'][:46]:46} "
              f"{t['segment_count']} ways")
    print("\noperators:", dict(collections.Counter(
        t.get("mgmt_area") or "unknown" for t in trails).most_common(8)))
