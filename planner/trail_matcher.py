"""
Match a GPX route to normalized trail entries from GeoJSON data.
"""

from __future__ import annotations

import math
from typing import List

from data import get_trails


def _distance_miles(lat1, lng1, lat2, lng2) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def match_trail(route: dict, name_hint: str = "") -> dict:
    trails = get_trails()
    midpoint = route.get("midpoint")
    if not midpoint:
        return {"shortlist": [], "auto_selected": None, "confidence": "low"}

    lat, lng = midpoint[0], midpoint[1]
    hint = (name_hint or "").strip().lower()

    scored = []
    for t in trails:
        d = _distance_miles(lat, lng, t["lat"], t["lng"])
        name = t["name"].lower()
        score = max(0.0, 5.0 - d)  # closer is better
        if hint and hint in name:
            score += 3.0
        scored.append((score, d, t))

    scored.sort(key=lambda x: (-x[0], x[1]))
    shortlist = [s[2] for s in scored[:5]]
    auto_selected = shortlist[0] if shortlist else None

    confidence = "low"
    if shortlist:
        confidence = "medium" if scored[0][0] >= 3 else "low"
        if scored[0][0] >= 5:
            confidence = "high"

    return {
        "shortlist": shortlist,
        "auto_selected": auto_selected,
        "confidence": confidence,
    }
