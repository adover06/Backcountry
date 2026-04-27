"""
Parse GPX route uploads into normalized route geometry + stats.
"""

from __future__ import annotations

import math
from typing import List, Tuple

import gpxpy


def _haversine_miles(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def parse_gpx_bytes(data: bytes) -> dict:
    gpx = gpxpy.parse(data)
    points: List[Tuple[float, float, float]] = []

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                points.append((point.latitude, point.longitude, point.elevation or 0.0))

    if not points:
        raise ValueError("No GPX track points found.")

    distance = 0.0
    elev_gain = 0.0
    for i in range(1, len(points)):
        a = points[i - 1]
        b = points[i]
        distance += _haversine_miles((a[0], a[1]), (b[0], b[1]))
        delta = b[2] - a[2]
        if delta > 0:
            elev_gain += delta

    lat_mid = sum(p[0] for p in points) / len(points)
    lng_mid = sum(p[1] for p in points) / len(points)

    route = {
        "points": [{"lat": p[0], "lng": p[1], "ele": p[2]} for p in points],
        "length_miles": round(distance, 2),
        "elev_gain_ft": int(round(elev_gain * 3.28084)),
        "midpoint": [round(lat_mid, 6), round(lng_mid, 6)],
    }
    return route
