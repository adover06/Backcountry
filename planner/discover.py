"""Faceted trail discovery — the search engine behind the map.

Loads the prebuilt index once, then answers viewport + facet queries in memory.
At ~8k trails a linear scan is well under a millisecond, so there is no database
in the request path.

Two rules carried over from the safety work:

  * Missing is never rendered as a value. A trail whose elevation could not be
    computed has `gain_ft: None`, and a gain filter *excludes* it rather than
    treating it as 0 and silently claiming it is flat.
  * Every numeric claim carries its source. Difficulty is a published formula,
    not a vibe, and it is None when its inputs are missing.
"""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from rapidfuzz import fuzz

_BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_PATH = _BASE_DIR / "data" / "trails_index.json"
GEOM_PATH = _BASE_DIR / "data" / "trails_geom.json"

_lock = threading.Lock()
_index: dict | None = None
_geometry: dict | None = None


# ── Difficulty ────────────────────────────────────────────────────────────────
#
# Shenandoah National Park's published hiking difficulty rating:
#     rating = sqrt(2 * elevation_gain_ft * distance_mi)
# It is widely used, defensible, and reproducible — unlike an opaque score.

_DIFFICULTY_BANDS = [
    (50, "easy"),
    (100, "moderate"),
    (150, "hard"),
    (200, "strenuous"),
    (math.inf, "very strenuous"),
]


def difficulty_rating(length_mi: float | None, gain_ft: int | None) -> dict | None:
    """Return {'score', 'label', 'formula'} or None when inputs are missing."""
    if not length_mi or gain_ft is None or length_mi <= 0 or gain_ft < 0:
        return None
    score = math.sqrt(2 * gain_ft * length_mi)
    label = next(name for threshold, name in _DIFFICULTY_BANDS if score < threshold)
    return {
        "score": round(score, 1),
        "label": label,
        "formula": "sqrt(2 * gain_ft * miles) — Shenandoah NP rating",
    }


DIFFICULTY_ORDER = ["easy", "moderate", "hard", "strenuous", "very strenuous"]


# ── Steepness ─────────────────────────────────────────────────────────────────
#
# Effort and steepness are different questions and hikers judge them separately:
# "how big is this day" vs "how hard is the climbing". The Shenandoah score answers
# the first. This answers the second.
#
# Calibrated against Santa Clara County Parks' human-assigned difficulty ratings
# (1,747 trails, 98% rated), which are the only ground-truth labels found in any
# free California source. Measured over 217 of their named trails after DEM
# sampling, correlation with their 1-5 rating:
#
#     ft per mile (steepness)   r = +0.568
#     Shenandoah score (effort) r = +0.336
#     total gain                r = +0.363
#     length                    r = +0.152
#
# Their median ft/mi by rating ran 109 / 214 / 393 / 539 for ratings 2/3/4/5, and
# the bands below sit at the midpoints between those medians.

STEEPNESS_BANDS = [
    (150, "gentle"),
    (300, "moderate"),
    (475, "steep"),
    (math.inf, "very steep"),
]

STEEPNESS_ORDER = ["gentle", "moderate", "steep", "very steep"]


def steepness_rating(length_mi: float | None, gain_ft: int | None) -> dict | None:
    """Average climb per mile, banded against real human ratings."""
    if not length_mi or gain_ft is None or length_mi <= 0 or gain_ft < 0:
        return None
    ft_per_mi = gain_ft / length_mi
    label = next(name for threshold, name in STEEPNESS_BANDS if ft_per_mi < threshold)
    return {
        "ft_per_mi": round(ft_per_mi),
        "label": label,
        "basis": "calibrated against Santa Clara County Parks difficulty ratings",
    }


# ── Index loading ─────────────────────────────────────────────────────────────


def _augment(trail: dict) -> dict:
    """Add derived fields the search layer needs."""
    elevation = trail.get("elevation") or {}
    gain = elevation.get("gain_ft") if elevation else None
    trail["gain_ft"] = gain
    trail["max_elevation_ft"] = elevation.get("max_ft") if elevation else None
    trail["min_elevation_ft"] = elevation.get("min_ft") if elevation else None
    trail["difficulty"] = difficulty_rating(trail.get("length_miles"), gain)
    trail["steepness"] = steepness_rating(trail.get("length_miles"), gain)
    trail["_search"] = " ".join(
        str(part).lower()
        for part in (trail.get("name"), trail.get("mgmt_area"), trail.get("trail_no"))
        if part
    )
    return trail


def load_index(force: bool = False) -> dict:
    """Load the prebuilt index into memory (idempotent, thread-safe)."""
    global _index
    with _lock:
        if _index is not None and not force:
            return _index
        if not INDEX_PATH.exists():
            raise FileNotFoundError(
                f"Trail index not found at {INDEX_PATH}. Build it with:\n"
                "    python -m pipeline.build_index"
            )
        payload = json.loads(INDEX_PATH.read_text())
        payload["trails"] = [_augment(t) for t in payload.get("trails", [])]
        payload["by_id"] = {t["id"]: t for t in payload["trails"]}
        _index = payload
        return _index


def load_geometry() -> dict:
    """Lazily load the geometry sidecar (large; only needed for map + detail)."""
    global _geometry
    with _lock:
        if _geometry is not None:
            return _geometry
        _geometry = json.loads(GEOM_PATH.read_text()) if GEOM_PATH.exists() else {}
        return _geometry


def get_trail(trail_id: str) -> dict | None:
    return load_index()["by_id"].get(trail_id)


def get_geometry(trail_id: str) -> dict | None:
    return load_geometry().get(trail_id)


def index_status() -> dict:
    """What the index knows and, importantly, what it does not."""
    try:
        index = load_index()
    except FileNotFoundError as exc:
        return {"available": False, "error": str(exc)}

    trails = index["trails"]
    return {
        "available": True,
        "generated_at": index.get("generated_at"),
        "count": len(trails),
        "sources": index.get("sources", {}),
        "coverage": {
            "with_elevation": sum(1 for t in trails if t.get("gain_ft") is not None),
            "with_scenery": sum(1 for t in trails if t.get("features")),
            "scenery_uncomputed": sum(1 for t in trails if t.get("features") is None),
            "with_grade": sum(1 for t in trails if t.get("grade")),
            "with_season": sum(1 for t in trails if t.get("season")),
        },
    }


# ── Filtering ─────────────────────────────────────────────────────────────────


def _in_bbox(trail: dict, bbox: list[float]) -> bool:
    """True when the trail's own bbox intersects the query bbox."""
    tb = trail.get("bbox")
    if not tb:
        return False
    return not (tb[2] < bbox[0] or tb[0] > bbox[2] or tb[3] < bbox[1] or tb[1] > bbox[3])


def _range_ok(value: Any, bounds: Iterable[float] | None) -> bool:
    """Range check that treats an unknown value as a non-match, never as zero."""
    if not bounds:
        return True
    low, high = (list(bounds) + [None, None])[:2]
    if value is None:
        return False
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def _tokenize(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]


# Minimum fuzzy score to be considered a match at all.
_FUZZY_FLOOR = 85.0

# Below this length, a query is too short for fuzzy matching to be meaningful —
# partial_ratio("mist", "mitchell peak") scores high on "mi" alone, which put 265
# results behind a four-letter query. Short queries must actually appear.
_SUBSTRING_REQUIRED_BELOW = 6


def _text_score(trail: dict, query: str, tokens: list[str]) -> float:
    """Fuzzy relevance for a free-text query.

    Requiring every token to appear was too strict: "lost coast" matched nothing,
    because trails named "Lost …" do not contain "coast" and a single missing token
    disqualified the whole record. Uses the same rapidfuzz approach as the planner's
    typeahead, which handles word-order and partial names, plus exact/prefix bonuses
    so "mount whitney" ranks Mount Whitney above Whitney Butte.
    """
    if not query:
        return 0.0

    name = (trail.get("name") or "").lower()
    if not name:
        return -1.0
    area = (trail.get("mgmt_area") or "").lower()


    if len(query) < _SUBSTRING_REQUIRED_BELOW and query not in name and query not in area:
        return -1.0

    # token_set_ratio tolerates word order and extra words; partial_ratio rewards
    # the query appearing as a run inside a longer name.
    score = float(max(fuzz.token_set_ratio(query, name), fuzz.partial_ratio(query, name)))

    if area:
        score += fuzz.partial_ratio(query, area) * 0.25

    if name == query:
        score += 120
    elif name.startswith(query):
        score += 60
    elif query in name:
        score += 35

    # Every query token present in the name is a strong signal, but not required.
    if tokens and all(t in name for t in tokens):
        score += 25

    return score if score >= _FUZZY_FLOOR else -1.0


def search(
    bbox: list[float] | None = None,
    q: str = "",
    length_mi: list[float] | None = None,
    gain_ft: list[float] | None = None,
    max_elevation_ft: list[float] | None = None,
    difficulty: list[str] | None = None,
    steepness: list[str] | None = None,
    features: list[str] | None = None,
    features_mode: str = "any",
    route_type: str | None = None,
    month: int | None = None,
    activity: str | None = None,
    wilderness_only: bool = False,
    accessible_only: bool = False,
    sort: str = "relevance",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Run a faceted search and return results plus facet counts."""
    index = load_index()
    trails = index["trails"]
    query_text = (q or "").strip().lower()
    tokens = _tokenize(q) if q else []

    # Each filter is a named predicate so facet counts can be computed with that
    # filter's own dimension excluded. Counting facets over the fully-filtered set
    # makes selecting one filter shrink the options for every other, so a second
    # choice that would have matched appears unavailable. Facets must describe
    # "what would happen if you also picked this", not "what is left".
    def pass_length(t):
        return _range_ok(t.get("length_miles"), length_mi)

    def pass_gain(t):
        return _range_ok(t.get("gain_ft"), gain_ft)

    def pass_elevation(t):
        return _range_ok(t.get("max_elevation_ft"), max_elevation_ft)

    def pass_difficulty(t):
        if not difficulty:
            return True
        rating = t.get("difficulty")
        return bool(rating and rating["label"] in difficulty)

    def pass_steepness(t):
        if not steepness:
            return True
        grade = t.get("steepness")
        return bool(grade and grade["label"] in steepness)

    def pass_features(t):
        if not features:
            return True
        have = t.get("features")
        if have is None:
            return False  # scenery not computed: unknown, not "no"
        want = set(features)
        return want.issubset(set(have)) if features_mode == "all" else bool(want & set(have))

    def pass_route_type(t):
        return not route_type or t.get("route_type") == route_type

    def pass_month(t):
        if not month:
            return True
        season = t.get("season")
        # No recorded window means unrestricted, which is how the feed reads.
        return not season or season.get("year_round") or month in season.get("months", [])

    def pass_activity(t):
        if not activity:
            return True
        state = (t.get("activities") or {}).get(activity)
        return bool(state and state.get("allowed"))

    def pass_wilderness(t):
        return not wilderness_only or bool(t.get("mgmt_area"))

    def pass_accessible(t):
        return not accessible_only or t.get("accessibility") == "ACCESSIBLE"

    predicates = {
        "length": pass_length,
        "gain": pass_gain,
        "elevation": pass_elevation,
        "difficulty": pass_difficulty,
        "steepness": pass_steepness,
        "features": pass_features,
        "route_type": pass_route_type,
        "month": pass_month,
        "activity": pass_activity,
        "wilderness": pass_wilderness,
        "accessible": pass_accessible,
    }

    def in_scope(t):
        """Viewport and text query apply to every count, including facets."""
        if bbox and not _in_bbox(t, bbox):
            return False
        return True

    scoped = [t for t in trails if in_scope(t)]

    matched = []
    for trail in scoped:
        if not all(check(trail) for check in predicates.values()):
            continue
        if query_text:
            score = _text_score(trail, query_text, tokens)
            if score < 0:
                continue
            trail = {**trail, "_score": score}
        matched.append(trail)

    def counted_for(dimension: str) -> list[dict]:
        """Trails passing every filter except `dimension`."""
        others = [c for name, c in predicates.items() if name != dimension]
        subset = []
        for trail in scoped:
            if not all(check(trail) for check in others):
                continue
            if query_text and _text_score(trail, query_text, tokens) < 0:
                continue
            subset.append(trail)
        return subset

    facets = _facets(
        matched,
        difficulty_pool=counted_for("difficulty"),
        steepness_pool=counted_for("steepness"),
        features_pool=counted_for("features"),
        route_pool=counted_for("route_type"),
    )

    reverse = True
    if sort == "length":
        matched.sort(key=lambda t: t.get("length_miles") or 0, reverse=True)
    elif sort == "length_asc":
        matched.sort(key=lambda t: t.get("length_miles") or 0)
    elif sort == "gain":
        matched.sort(key=lambda t: t.get("gain_ft") if t.get("gain_ft") is not None else -1, reverse=True)
    elif sort == "difficulty":
        matched.sort(key=lambda t: (t.get("difficulty") or {}).get("score", -1), reverse=True)
    elif sort == "steepness":
        matched.sort(key=lambda t: (t.get("steepness") or {}).get("ft_per_mi", -1), reverse=True)
    elif sort == "name":
        matched.sort(key=lambda t: t.get("name") or "")
        reverse = False
    else:  # relevance
        matched.sort(
            key=lambda t: (t.get("_score", 0), t.get("length_miles") or 0),
            reverse=True,
        )

    total = len(matched)
    page = matched[offset : offset + limit]

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "facets": facets,
        "results": [_public(t) for t in page],
    }


def _facets(
    trails: list[dict],
    difficulty_pool: list[dict] | None = None,
    steepness_pool: list[dict] | None = None,
    features_pool: list[dict] | None = None,
    route_pool: list[dict] | None = None,
) -> dict:
    """Counts for the filter UI.

    Each dimension counts over a pool with its own filter removed, so a chip shows
    how many results choosing it would give — not how many survive the choices
    already made. Without that, picking one filter greys out every other.
    """
    feature_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    steepness_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    unknown_elevation = 0

    for trail in features_pool if features_pool is not None else trails:
        for feature in trail.get("features") or ():
            feature_counts[feature] = feature_counts.get(feature, 0) + 1

    for trail in difficulty_pool if difficulty_pool is not None else trails:
        rating = trail.get("difficulty")
        if rating:
            difficulty_counts[rating["label"]] = difficulty_counts.get(rating["label"], 0) + 1
        else:
            unknown_elevation += 1

    for trail in steepness_pool if steepness_pool is not None else trails:
        grade = trail.get("steepness")
        if grade:
            steepness_counts[grade["label"]] = steepness_counts.get(grade["label"], 0) + 1

    for trail in route_pool if route_pool is not None else trails:
        route = trail.get("route_type")
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1

    return {
        "features": dict(sorted(feature_counts.items(), key=lambda kv: -kv[1])),
        "difficulty": {k: difficulty_counts.get(k, 0) for k in DIFFICULTY_ORDER},
        "steepness": {k: steepness_counts.get(k, 0) for k in STEEPNESS_ORDER},
        "route_type": route_counts,
        "unknown_difficulty": unknown_elevation,
    }


_PUBLIC_FIELDS = (
    "id",
    "name",
    "slug",
    "trail_no",
    "length_miles",
    "route_type",
    "bbox",
    "center",
    "surface",
    "mgmt_area",
    "accessibility",
    "trail_class",
    "trail_class_label",
    "grade",
    "season",
    "features",
    "nearby",
    "gain_ft",
    "max_elevation_ft",
    "min_elevation_ft",
    "difficulty",
    "steepness",
    "elevation",
    # Present only on OSM long-distance routes.
    "source",
    "endpoints",
    "access",
    "technical",
    "permits",
    "network",
    "website",
    "wikipedia",
)


def _public(trail: dict) -> dict:
    """Strip internal fields before returning a trail to a client."""
    return {key: trail.get(key) for key in _PUBLIC_FIELDS if key in trail}


def bounds_of(trails: list[dict]) -> list[float] | None:
    """Combined bbox for a result set, for fitting the map."""
    boxes = [t.get("bbox") for t in trails if t.get("bbox")]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def map_features(
    bbox: list[float] | None = None,
    limit: int = 400,
    **filters,
) -> dict:
    """GeoJSON FeatureCollection for the map, honoring the same filters as search."""
    result = search(bbox=bbox, limit=limit, **filters)
    geometry = load_geometry()

    features = []
    for trail in result["results"]:
        entry = geometry.get(trail["id"]) or {}
        geom = entry.get("geometry")
        if not geom:
            continue
        features.append(
            {
                "type": "Feature",
                "id": trail["id"],
                "geometry": geom,
                "properties": {
                    "id": trail["id"],
                    "name": trail["name"],
                    "length_miles": trail.get("length_miles"),
                    "gain_ft": trail.get("gain_ft"),
                    "difficulty": (trail.get("difficulty") or {}).get("label"),
                    "features": ",".join(trail.get("features") or []),
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "total": result["total"],
        "returned": len(features),
        "truncated": result["total"] > len(features),
    }
