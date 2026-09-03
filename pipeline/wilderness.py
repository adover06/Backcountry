"""Designated wilderness — the land-status fact that changes how a hike is planned.

A trail inside designated wilderness is a materially different proposition from one
outside it: entry is usually permit-controlled and quota-limited, groups are capped,
bikes and all mechanised transport are prohibited by the Wilderness Act, dogs are
often barred, and campfire rules tighten. None of that is derivable from trail
geometry, and the index had no field for it — `mgmt_area` reaches only 25.4% and
carries an administrative forest name, not a land-status designation.

`permits.py` already flags that a permit *may* apply, joining Recreation.gov permit
facilities by proximity from a single coordinate that governs an entire forest. This
is the sharper instrument: a trail is either inside a wilderness boundary or it is
not, and the answer comes from the polygon rather than from a radius guess.

Source is USFS EDW `Wilderness_02`, a US federal work (public domain, no key).

**Point-in-polygon here is deliberately hand-rolled.** The pipeline avoids shapely
and GEOS on purpose — `trail_graph.py` makes the same call — because a source build
of the geospatial stack is the single biggest obstacle to installing this project.
Ray casting over a few hundred polygons, bbox-rejected first, is milliseconds.

A trail is not simply in or out. Trails cross boundaries, and a route that clips a
corner is not "in the Ansel Adams Wilderness" in any useful sense. So the geometry
is sampled and the **fraction inside** is recorded alongside the name, with a
threshold below which the trail is treated as outside. Partial overlap is reported
as what it is, not rounded to a yes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .nps import CALIFORNIA_BBOX, _fetch_paged

WILDERNESS_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
    "EDW_Wilderness_02/MapServer/0/query"
)

_CACHE_DIR = Path(
    os.environ.get(
        "USFS_CACHE_DIR", Path(__file__).resolve().parent.parent / ".usfs_cache"
    )
)

# Below this share of sampled points inside a boundary, the trail is treated as
# outside it. A trail brushing a corner should not inherit the area's permit rules.
MIN_INSIDE_FRACTION = 0.20

# Sampling every vertex is wasted work on a 4,000-point MultiLineString; this is
# dense enough that a trail cannot cross a wilderness without being seen.
MAX_SAMPLES = 60


def fetch_wilderness(
    bbox=CALIFORNIA_BBOX, use_cache: bool = True, verbose: bool = True
) -> list[dict]:
    """USFS designated wilderness polygons intersecting the bbox, cached."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / "usfs_wilderness_ca.json"

    if use_cache and cache.exists():
        try:
            features = json.loads(cache.read_text())
            if verbose:
                print(f"  loaded {len(features)} cached wilderness areas")
            return features
        except Exception:
            pass

    if verbose:
        print("  fetching USFS wilderness boundaries…")
    features = _fetch_paged(WILDERNESS_URL, bbox, verbose=verbose)
    cache.write_text(json.dumps(features))
    return features


def _rings(geometry: dict) -> list[list[list[list[float]]]]:
    """Normalise Polygon / MultiPolygon to a list of polygons, each a ring list."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return [coords]
    if gtype == "MultiPolygon":
        return coords
    return []


def _point_in_ring(lng: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray casting: count crossings of a horizontal ray to the east."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        # Does the edge straddle the ray's latitude, and is the crossing east?
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if x_cross > lng:
                inside = not inside
        j = i
    return inside


class WildernessIndex:
    """Bbox-rejected point-in-polygon over the wilderness boundaries.

    The stage costs ~265s over the 10,694-trail index and two attempts to speed it
    up were measured and reverted, so that nobody repeats them:

        linear scan over all 88 areas      265s   (this code)
        + 1 degree grid broad phase        262s
        + per-polygon bounds pre-check     265s

    Neither helps because the bbox rejection was never the bottleneck. The cost is
    ray casting the rings that survive rejection — a point near John Muir really
    does have to be walked against thousands of vertices. Making this faster means
    changing the algorithm (a prepared edge index, or fewer samples per trail), not
    adding another pre-filter.
    """

    def __init__(self, features: list[dict]) -> None:
        self.areas: list[dict] = []
        for feature in features:
            polygons = _rings(feature.get("geometry") or {})
            if not polygons:
                continue
            props = feature.get("properties") or {}
            name = props.get("wildernessname")
            if not name:
                continue

            lngs = [c[0] for poly in polygons for ring in poly for c in ring]
            lats = [c[1] for poly in polygons for ring in poly for c in ring]
            self.areas.append(
                {
                    "name": str(name).strip(),
                    "id": props.get("wildernessid"),
                    "acres": props.get("gis_acres"),
                    "polygons": polygons,
                    "bbox": (min(lngs), min(lats), max(lngs), max(lats)),
                }
            )

    def __len__(self) -> int:
        return len(self.areas)

    def contains(self, lng: float, lat: float) -> dict | None:
        for area in self.areas:
            min_lng, min_lat, max_lng, max_lat = area["bbox"]
            if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
                continue
            for polygon in area["polygons"]:
                if not polygon:
                    continue
                # Ring 0 is the exterior; any further rings are holes punched in it.
                if not _point_in_ring(lng, lat, polygon[0]):
                    continue
                if any(_point_in_ring(lng, lat, hole) for hole in polygon[1:]):
                    continue
                return area
        return None


def _sample_coords(geometry: dict | None) -> list[list[float]]:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        points = coords
    elif gtype == "MultiLineString":
        points = [c for line in coords for c in line]
    else:
        return []

    if len(points) <= MAX_SAMPLES:
        return points
    step = len(points) / MAX_SAMPLES
    return [points[int(i * step)] for i in range(MAX_SAMPLES)]


def enrich_trail(trail: dict, index: WildernessIndex, geometry: dict | None) -> dict:
    """Attach the wilderness a trail actually runs through, with its coverage."""
    points = _sample_coords(geometry)
    if not points:
        trail["wilderness"] = None
        return trail

    hits: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for point in points:
        area = index.contains(point[0], point[1])
        if area is None:
            continue
        hits[area["name"]] = area
        counts[area["name"]] = counts.get(area["name"], 0) + 1

    if not counts:
        # [] would claim we found nothing; None is right only before we looked, and
        # we have looked, so this is an explicit empty answer.
        trail["wilderness"] = {}
        return trail

    name, count = max(counts.items(), key=lambda kv: kv[1])
    fraction = count / len(points)
    if fraction < MIN_INSIDE_FRACTION:
        trail["wilderness"] = {}
        return trail

    area = hits[name]
    trail["wilderness"] = {
        "name": name,
        "id": area["id"],
        "acres": area["acres"],
        "inside_fraction": round(fraction, 3),
        "fully_inside": fraction >= 0.99,
        "source": "USFS EDW Wilderness",
    }
    return trail


def _permit_key(name: str) -> str:
    """The distinctive part of a wilderness name, for matching permit facilities."""
    key = name.lower().replace("wilderness", "").strip()
    # Trailing qualifiers ("Yolla Bolly-Middle Eel") stay; only noise is stripped.
    return " ".join(key.split())


def link_permits(trails: list[dict], permits: list[dict], verbose: bool = True) -> list[dict]:
    """Attach the permit facility that actually governs a trail's wilderness.

    `permits.py` joins Recreation.gov facilities within 35 miles, because one
    coordinate stands for a whole forest and a tight radius would attach nothing.
    That radius is honest but blunt: it says a permit desk is *near*, not that it
    governs this trail.

    Containment plus a name match is exact. "Desolation Wilderness Permit" governs
    the Desolation Wilderness, and we now know which trails are inside it. Only
    facilities whose name contains the wilderness name qualify, so a forest-level
    desk ("Inyo National Forest - Wilderness Permits") is deliberately not matched
    here — it covers several wildernesses and the proximity join already carries it.
    """
    by_key: dict[str, dict] = {}
    for permit in permits:
        name = (permit.get("name") or "").lower()
        for trail in trails:
            area = (trail.get("wilderness") or {}).get("name")
            if not area:
                continue
            key = _permit_key(area)
            if key and key in name and key not in by_key:
                by_key[key] = permit

    linked = 0
    for trail in trails:
        area = (trail.get("wilderness") or {}).get("name")
        if not area:
            continue
        permit = by_key.get(_permit_key(area))
        if not permit:
            continue
        trail["wilderness"]["permit"] = {
            "name": permit.get("name"),
            "id": permit.get("id"),
            "match": "wilderness boundary + name",
            "source": "Recreation.gov (RIDB)",
        }
        linked += 1

    if verbose:
        areas = sorted(by_key.values(), key=lambda p: p.get("name") or "")
        print(f"  {linked} trails matched to {len(areas)} wilderness permit facilities")
        for permit in areas:
            print(f"    {permit.get('name')}")
    return trails


def enrich_all(trails: list[dict], geometries: dict, verbose: bool = True) -> list[dict]:
    index = WildernessIndex(fetch_wilderness(verbose=verbose))
    if verbose:
        print(f"  indexed {len(index)} wilderness areas")

    for i, trail in enumerate(trails, start=1):
        entry = geometries.get(trail["id"]) or {}
        enrich_trail(trail, index, entry.get("geometry"))
        if verbose and i % 3000 == 0:
            print(f"  tested {i}/{len(trails)}")

    if verbose:
        inside = [t for t in trails if (t.get("wilderness") or {}).get("name")]
        full = sum(1 for t in inside if t["wilderness"]["fully_inside"])
        print(
            f"  {len(inside)}/{len(trails)} trails in wilderness "
            f"({len(inside) / len(trails):.1%}); {full} entirely inside"
        )

    permits_path = Path(__file__).resolve().parent.parent / "data" / "permits.json"
    if permits_path.exists():
        try:
            link_permits(trails, json.loads(permits_path.read_text()), verbose=verbose)
        except Exception as exc:
            if verbose:
                print(f"  permit linking skipped ({exc})")
    return trails


if __name__ == "__main__":
    index = WildernessIndex(fetch_wilderness())
    print(f"\n{len(index)} California wilderness areas")
    for area in sorted(index.areas, key=lambda a: -(a["acres"] or 0))[:12]:
        print(f"  {area['name']:44} {area['acres']:>12,.0f} acres")
