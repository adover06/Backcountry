"""
Match a GPX route to normalized trail entries from GeoJSON data.
"""

from __future__ import annotations

import logging
import math
from typing import List

from rapidfuzz import fuzz
from data import get_trails

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _distance_miles(lat1, lng1, lat2, lng2) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def suggest_trails(query: str, limit: int = 8) -> list[dict]:
    """Fast typeahead: return up to `limit` lightweight trail dicts for a query string."""
    hint = query.strip().lower()
    if not hint:
        return []

    trails = get_trails()
    scored: list[tuple[float, dict]] = []

    for t in trails:
        name = (t.get("name") or "").lower()
        area = (t.get("area") or "").lower()

        token_score = fuzz.token_set_ratio(hint, name)
        partial_name = fuzz.partial_ratio(hint, name)
        area_score = fuzz.partial_ratio(hint, area) * 0.35 if area else 0

        name_signal = max(token_score, partial_name)
        score = name_signal + area_score

        if hint in name:
            score = min(135, score + 20)

        if score >= 45:
            scored.append((score, t))

    scored.sort(key=lambda x: -x[0])
    results = []
    for _, t in scored[:limit]:
        results.append({
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "area": t.get("area", ""),
            "length_miles": t.get("length_miles", 0),
            "lat": t.get("lat", 0.0),
            "lng": t.get("lng", 0.0),
        })
    return results


def match_trail(route: dict, name_hint: str = "") -> dict:
    trails = get_trails()
    midpoint = route.get("midpoint") if route else None
    hint = (name_hint or "").strip().lower()
    
    # If no route/midpoint but have a name hint, search by name with fuzzy matching
    if not midpoint and hint:
        scored = []
        for t in trails:
            name = (t.get("name") or "").lower()
            area = (t.get("area") or "").lower()

            # Primary: token_set_ratio handles word-order variations well
            token_score = fuzz.token_set_ratio(hint, name)
            # Secondary: partial_ratio rewards substring containment
            partial_name = fuzz.partial_ratio(hint, name)
            # Area match as a tiebreaker signal (down-weighted)
            area_score = fuzz.partial_ratio(hint, area) * 0.35 if area else 0

            # Combine: take the best name signal, then add fractional area boost
            name_signal = max(token_score, partial_name)
            score = name_signal + area_score

            # Boost exact substring matches in name
            if hint in name:
                score = min(135, score + 20)

            # Only include if reasonable match
            if score >= 45:
                scored.append((score, t))

        scored.sort(key=lambda x: -x[0])
        shortlist = [s[1] for s in scored[:10]]
        auto_selected = shortlist[0] if shortlist else None

        confidence = "low"
        if auto_selected and scored:
            top_score = scored[0][0]
            matched_name = (auto_selected.get("name") or "").lower()
            if hint == matched_name or matched_name.startswith(hint):
                confidence = "high"
            elif fuzz.partial_ratio(hint, matched_name) >= 85:
                confidence = "high"
            elif top_score >= 75:
                confidence = "medium"

        return {
            "shortlist": shortlist,
            "auto_selected": auto_selected,
            "confidence": confidence,
        }
    
    if not midpoint:
        return {"shortlist": [], "auto_selected": None, "confidence": "low"}

    lat, lng = midpoint[0], midpoint[1]

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
