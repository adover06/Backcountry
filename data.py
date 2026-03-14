"""
Loads and filters backpacking trails from the source JSON.
All units are converted to imperial (miles, feet) at load time.
"""

import json
from pathlib import Path
from typing import Optional

_SOURCE = Path(__file__).parent.parent / "lomein.json"

DIFFICULTY = {
    "1": "easy", "2": "easy",
    "3": "moderate", "4": "moderate",
    "5": "hard", "6": "hard",
    "7": "very hard",
}

ROUTE = {"O": "out-and-back", "L": "loop", "P": "point-to-point"}


def _meters_to_miles(m: float) -> float:
    return round(m / 1609.34, 1)


def _meters_to_feet(m: float) -> int:
    return round(m * 3.28084)


def _normalize(trail: dict) -> dict:
    """Return a clean, human-readable trail dict."""
    return {
        "id":            trail["trail_id"],
        "name":          trail["name"],
        "area":          trail.get("area_name", ""),
        "city":          trail.get("city_name", ""),
        "lat":           trail["_geoloc"]["lat"],
        "lng":           trail["_geoloc"]["lng"],
        "length_miles":  _meters_to_miles(trail.get("length", 0)),
        "elev_gain_ft":  _meters_to_feet(trail.get("elevation_gain", 0)),
        "difficulty":    DIFFICULTY.get(str(trail.get("difficulty_rating", "")), "unknown"),
        "route_type":    ROUTE.get(trail.get("route_type", ""), trail.get("route_type", "")),
        "avg_rating":    trail.get("avg_rating", 0),
        "num_reviews":   trail.get("num_reviews", 0),
        "popularity":    round(trail.get("popularity", 0), 1),
        "features":      trail.get("features", []),
        "activities":    trail.get("activities", []),
        "visitor_usage": trail.get("visitor_usage", ""),
        "slug":          trail.get("slug", ""),
    }


def load_backpacking_trails() -> list[dict]:
    """Return all CA trails that include backpacking as an activity."""
    with open(_SOURCE) as f:
        raw = json.load(f)
    return [
        _normalize(t) for t in raw
        if "backpacking" in t.get("activities", [])
    ]


# Module-level cache — loaded once
_TRAILS: list[dict] = []


def get_trails() -> list[dict]:
    global _TRAILS
    if not _TRAILS:
        _TRAILS = load_backpacking_trails()
    return _TRAILS


def find_by_name(name: str) -> Optional[dict]:
    name_lower = name.lower()
    return next(
        (t for t in get_trails() if name_lower in t["name"].lower()),
        None,
    )
