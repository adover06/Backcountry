"""Where to put the dot that represents a trail on the map.

The index already carries `center`, and it is the wrong point to draw. For USFS
records it is a coordinate average and for NPS records it is literally the midpoint
of the bounding box — so on any trail that bends, the point sits off the trail.
Measured across 4,000 trails against their own geometry:

    median distance from `center` to the nearest point on the trail   0.09 mi
    more than 0.25 mi off the trail                                   13.2%
    more than 1 mi off the trail                                       0.9%
    worst: California Riding And Hiking, 4.71 mi off its own trail

A dot 4.71 miles from the trail it labels is not a rendering nit; it points at the
wrong valley. So the marker is computed from the geometry instead, and is chosen to
answer the question a browsing user is actually asking:

1. **The trailhead**, when one is joined to this trail. That is where you start, it
   is what the dot means, and it is a real surveyed point.
2. Otherwise the **midpoint along the line** — the vertex at half the trail's
   cumulative length. Always exactly on the trail, and representative of where the
   trail lies rather than where its bounding box happens to be centred.

`center` is left untouched: it is the right thing for fitting a viewport to a
bounding box, which is what it is used for elsewhere.
"""

from __future__ import annotations

import math

EARTH_RADIUS_MI = 3958.7613


def _haversine_mi(a, b) -> float:
    lon1, lat1 = a[0], a[1]
    lon2, lat2 = b[0], b[1]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(h)))


def _points(geometry: dict | None) -> list:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return coords
    if gtype == "MultiLineString":
        # Longest part: a trail's identity is its main line, not a stray connector.
        parts = [p for p in coords if len(p) >= 2]
        return max(parts, key=len) if parts else []
    return []


def midpoint_along(points: list) -> list | None:
    """The vertex at half the cumulative length. Always on the line."""
    if len(points) < 2:
        return points[0][:2] if points else None

    steps = [_haversine_mi(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(steps)
    if total <= 0:
        return points[0][:2]

    walked = 0.0
    half = total / 2
    for i, step in enumerate(steps):
        walked += step
        if walked >= half:
            return points[i + 1][:2]
    return points[-1][:2]


# A trailhead further than this from the marker point is describing somewhere else.
TRAILHEAD_MAX_MI = 0.5


def marker_for(trail: dict, geometry: dict | None) -> dict | None:
    """The point to draw for this trail, with what it represents.

    Returns None when there is no geometry to work from — the caller must not
    invent a coordinate, because a fabricated dot is worse than a missing one.
    """
    points = _points(geometry)
    if not points:
        return None

    on_line = midpoint_along(points)
    if not on_line:
        return None

    trailhead = (trail.get("access") or {}).get("trailhead")
    if trailhead and trailhead.get("lat") is not None and trailhead.get("lng") is not None:
        point = [trailhead["lng"], trailhead["lat"]]
        if trailhead.get("distance_mi", 0) <= TRAILHEAD_MAX_MI:
            return {
                "point": [round(point[0], 6), round(point[1], 6)],
                "kind": "trailhead",
                "name": trailhead.get("name"),
            }

    return {"point": [round(on_line[0], 6), round(on_line[1], 6)], "kind": "midpoint"}


def enrich_all(trails: list[dict], geometries: dict, verbose: bool = True) -> list[dict]:
    for trail in trails:
        entry = geometries.get(trail["id"]) or {}
        trail["marker"] = marker_for(trail, entry.get("geometry"))

    if verbose:
        kinds: dict[str, int] = {}
        for trail in trails:
            marker = trail.get("marker")
            kinds[marker["kind"] if marker else "none"] = (
                kinds.get(marker["kind"] if marker else "none", 0) + 1
            )
        total = len(trails)
        for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:12} {count:6}/{total}  {count / total:5.1%}")
    return trails
