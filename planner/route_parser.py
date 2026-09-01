"""Parse GPX uploads into normalized route geometry and statistics.

Corrections over the previous version:

* Missing elevation stays None instead of becoming 0.0. Coercing it produced a flat
  route reporting 0 ft of gain, and made "the highest point on the route" — used to
  place the weather and snow samples — effectively arbitrary.
* Elevation gain is thresholded. Summing every positive delta turns GPS jitter into
  thousands of feet of phantom climbing on an otherwise flat walk.
* `midpoint` is the point halfway *along the route*, not the mean of all coordinates.
  On a horseshoe or lollipop the mean can land miles off-trail in a different
  drainage, which then drives every weather, AQI, fire, and snow lookup.
* `<rte>` routes and standalone waypoints are read, not just `<trk>` tracks. Exports
  from CalTopo and Gaia commonly use routes.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import gpxpy

METERS_TO_FEET = 3.28084

# Ignore climbs smaller than this, to reject GPS elevation noise.
GAIN_THRESHOLD_FT = 15.0

# Refuse absurd uploads outright rather than melting downstream calls.
MAX_POINTS = 200_000


class GPXParseError(ValueError):
    """Raised when a GPX file contains no usable track."""


def _haversine_miles(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def _collect_points(gpx) -> List[Tuple[float, float, Optional[float]]]:
    """Gather points from tracks, then routes, then waypoints."""
    points: List[Tuple[float, float, Optional[float]]] = []

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                points.append((point.latitude, point.longitude, point.elevation))

    if not points:
        for route in getattr(gpx, "routes", []):
            for point in route.points:
                points.append((point.latitude, point.longitude, point.elevation))

    if not points:
        waypoints = getattr(gpx, "waypoints", [])
        if len(waypoints) >= 2:
            for point in waypoints:
                points.append((point.latitude, point.longitude, point.elevation))

    return points


def _elevation_gain_ft(elevations: List[Optional[float]]) -> Tuple[Optional[int], Optional[int]]:
    """Thresholded ascent/descent in feet, or (None, None) with no elevation data."""
    series = [e * METERS_TO_FEET for e in elevations if isinstance(e, (int, float))]
    if len(series) < 2:
        return None, None

    gain = loss = 0.0
    anchor = series[0]
    direction = 0

    for value in series[1:]:
        delta = value - anchor
        if delta >= GAIN_THRESHOLD_FT:
            gain += delta
            anchor = value
            direction = 1
        elif delta <= -GAIN_THRESHOLD_FT:
            loss += -delta
            anchor = value
            direction = -1
        elif direction > 0 and value > anchor:
            gain += value - anchor
            anchor = value
        elif direction < 0 and value < anchor:
            loss += anchor - value
            anchor = value

    return int(round(gain)), int(round(loss))


def _midpoint_along_route(
    points: List[Tuple[float, float, Optional[float]]], total_miles: float
) -> List[float]:
    """The coordinate at half the route's travelled distance."""
    if total_miles <= 0:
        return [round(points[0][0], 6), round(points[0][1], 6)]

    target = total_miles / 2
    travelled = 0.0
    for i in range(1, len(points)):
        previous, current = points[i - 1], points[i]
        travelled += _haversine_miles((previous[0], previous[1]), (current[0], current[1]))
        if travelled >= target:
            return [round(current[0], 6), round(current[1], 6)]

    last = points[-1]
    return [round(last[0], 6), round(last[1], 6)]


def parse_gpx_bytes(data: bytes) -> dict:
    try:
        gpx = gpxpy.parse(data)
    except Exception as exc:
        raise GPXParseError(f"Could not read GPX file: {exc}") from exc

    points = _collect_points(gpx)
    if len(points) < 2:
        raise GPXParseError(
            "No usable track found. The file needs at least two points in a "
            "track (<trk>), route (<rte>), or waypoint list."
        )
    if len(points) > MAX_POINTS:
        raise GPXParseError(f"GPX has {len(points):,} points; the limit is {MAX_POINTS:,}.")

    distance = 0.0
    for i in range(1, len(points)):
        distance += _haversine_miles(
            (points[i - 1][0], points[i - 1][1]), (points[i][0], points[i][1])
        )

    elevations = [p[2] for p in points]
    gain_ft, loss_ft = _elevation_gain_ft(elevations)
    known = [e for e in elevations if isinstance(e, (int, float))]

    highest = None
    if known:
        peak = max(
            (p for p in points if isinstance(p[2], (int, float))), key=lambda p: p[2]
        )
        highest = {
            "lat": peak[0],
            "lng": peak[1],
            "ele_ft": int(round(peak[2] * METERS_TO_FEET)),
        }

    return {
        "points": [
            {"lat": p[0], "lng": p[1], "ele": p[2]}  # ele stays None when absent
            for p in points
        ],
        "length_miles": round(distance, 2),
        # None, not 0, when the file carries no elevation — an unmeasured route is
        # not a flat one.
        "elev_gain_ft": gain_ft,
        "elev_loss_ft": loss_ft,
        "has_elevation": bool(known),
        "min_ele_ft": int(round(min(known) * METERS_TO_FEET)) if known else None,
        "max_ele_ft": int(round(max(known) * METERS_TO_FEET)) if known else None,
        "highest_point": highest,
        "midpoint": _midpoint_along_route(points, distance),
        "point_count": len(points),
    }
