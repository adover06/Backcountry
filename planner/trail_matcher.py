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


def match_trail(route: dict, name_hint: str = "") -> dict:
    trails = get_trails()
    midpoint = route.get("midpoint") if route else None
    hint = (name_hint or "").strip().lower()
    
    # If no route/midpoint but have a name hint, search by name with fuzzy matching
    if not midpoint and hint:
        hint_words = hint.split()
        scored = []
        for t in trails:
            name = (t.get("name") or "").lower()
            area = (t.get("area") or "").lower()
            city = (t.get("city") or "").lower()
            
            # Fuzzy match scores
            name_score = fuzz.partial_ratio(hint, name)
            area_score = fuzz.partial_ratio(hint, area) if area else 0
            city_score = fuzz.partial_ratio(hint, city) if city else 0
            token_score = fuzz.token_set_ratio(hint, name)
            
            # Combine scores with weights
            score = max(name_score * 1.0, area_score * 0.5, city_score * 0.3, token_score * 0.8)
            
            # Boost exact substring matches
            if hint in name:
                score = min(100, score + 15)
            
            # Only include if reasonable match
            if score >= 40:
                scored.append((score, t))
        
        scored.sort(key=lambda x: -x[0])
        shortlist = [s[1] for s in scored[:10]]
        auto_selected = shortlist[0] if shortlist else None
        
        confidence = "low"
        if auto_selected:
            matched_name = (auto_selected.get("name") or "").lower()
            if hint == matched_name or matched_name.startswith(hint):
                confidence = "high"
            elif fuzz.partial_ratio(hint, matched_name) >= 85:
                confidence = "high"
            elif score >= 70:
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
