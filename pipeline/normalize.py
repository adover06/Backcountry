"""Normalize the raw USFS National Forest System Trails GeoJSON into trail records.

The raw feed is one row per *segment*; a trail (`trail_cn`) is many segments. This
module groups them, normalizes the dual-encoded attribute values, and keeps the
geometry as a MultiLineString so disjoint segments are never joined by a line that
does not exist on the ground.

Missing is not the same as known. Every field that could not be determined is left
as None rather than defaulted, so downstream filters can say "unknown" instead of
silently asserting a value.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = _BASE_DIR / "data" / "trails.geojson"

# Values the USFS feed uses to mean "not applicable / not recorded".
_NULLISH = {"", "none", "n/a", "na", "null", "unknown"}

# Endpoint match tolerance when chaining segments, in degrees (~11 m at CA latitudes).
_JOIN_TOLERANCE_DEG = 1e-4


def _clean(value: Any) -> str | None:
    """Return a trimmed string, or None for any of the feed's null spellings."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return None
    return text


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


# ── Value normalization ───────────────────────────────────────────────────────
#
# The feed encodes several attributes two ways, e.g. "12-20%" and "TG05 - +12-20%"
# for the same grade band. Normalize to one canonical form so faceting works.

_GRADE_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*%")


def normalize_grade(raw: Any) -> dict | None:
    """Parse a typical_trail_grade value into {'min_pct', 'max_pct', 'label'}."""
    text = _clean(raw)
    if not text:
        return None
    match = _GRADE_RE.search(text)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    if low > high:
        low, high = high, low
    return {"min_pct": low, "max_pct": high, "label": f"{low}-{high}%"}


def normalize_surface(raw: Any) -> str | None:
    """Strip the 'NAT - ' style code prefix and title-case the surface name."""
    text = _clean(raw)
    if not text:
        return None
    # "NAT - NATIVE MATERIAL" and "AC- ASPHALT" both carry a leading code.
    text = re.sub(r"^[A-Z]{1,4}\s*-\s*", "", text).strip()
    return text.title() if text else None


def normalize_mgmt_area(raw: Any) -> str | None:
    """'NM - NATIONAL MONUMENT' -> 'National Monument'."""
    text = _clean(raw)
    if not text:
        return None
    text = re.sub(r"^[A-Z]{2,4}\s*-\s*", "", text).strip()
    return text.title() if text else None


# USFS Trail Class: 1 is most primitive/challenging, 5 is most developed.
_TRAIL_CLASS_LABEL = {
    1: "primitive",
    2: "simple",
    3: "developed",
    4: "highly developed",
    5: "fully developed",
}


def normalize_trail_class(raw: Any) -> int | None:
    text = _clean(raw)
    if not text:
        return None
    try:
        value = int(float(text))
    except ValueError:
        return None
    return value if 1 <= value <= 5 else None


_DATE_RANGE_RE = re.compile(r"(\d{2})/(\d{2})\s*-\s*(\d{2})/(\d{2})")


def parse_season(raw: Any) -> dict | None:
    """Parse an 'MM/DD-MM/DD' window into open months.

    Returns {'label', 'months', 'year_round'} or None when no window is recorded.
    Windows that wrap the new year (e.g. 11/16-04/30) are handled.
    """
    text = _clean(raw)
    if not text:
        return None
    match = _DATE_RANGE_RE.search(text)
    if not match:
        return None
    start_month, start_day, end_month, end_day = (int(g) for g in match.groups())
    if not (1 <= start_month <= 12 and 1 <= end_month <= 12):
        return None

    year_round = (start_month, start_day, end_month, end_day) == (1, 1, 12, 31)
    if start_month <= end_month:
        months = list(range(start_month, end_month + 1))
    else:  # wraps the new year
        months = list(range(start_month, 13)) + list(range(1, end_month + 1))

    return {
        "label": f"{start_month:02d}/{start_day:02d}-{end_month:02d}/{end_day:02d}",
        "months": months,
        "year_round": year_round,
    }


# ── Allowed-use extraction ────────────────────────────────────────────────────

# Each activity in the feed has managed / accpt / accpt_disc / restricted columns.
_ACTIVITY_PREFIXES = {
    "hiking": "hiker_pedestrian",
    "horse": "pack_saddle",
    "bike": "bicycle",
    "motorcycle": "motorcycle",
    "atv": "atv",
    "fourwd": "fourwd",
    "snowshoe": "snowshoe",
    "xc_ski": "xcountry_ski",
    "snowmobile": "snowmobile",
    "e_bike": "e_bike_class1",
}


def _activity_state(props: dict, prefix: str) -> dict | None:
    """Summarize one activity's columns into {'allowed', 'season', 'restricted'}.

    `allowed` is True only when the feed positively records a window. It is None
    (not False) when nothing is recorded — absence of a record is not a prohibition.
    """
    managed = _clean(props.get(f"{prefix}_managed"))
    accepted = _clean(props.get(f"{prefix}_accpt")) or _clean(props.get(f"{prefix}_accpt_disc"))
    restricted = _clean(props.get(f"{prefix}_restricted"))

    if not any((managed, accepted, restricted)):
        return None

    season = parse_season(managed) or parse_season(accepted)
    return {
        "allowed": bool(managed or accepted) or None,
        "restricted": bool(restricted) or None,
        "season": season,
    }


# ── Geometry handling ─────────────────────────────────────────────────────────


def _segment_lines(geometry: dict | None) -> list[list[list[float]]]:
    """Return a list of coordinate rings ([[lng, lat], ...]) from any line geometry."""
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "LineString" and isinstance(coords, list) and len(coords) >= 2:
        return [coords]
    if gtype == "MultiLineString" and isinstance(coords, list):
        return [line for line in coords if isinstance(line, list) and len(line) >= 2]
    return []


def _close_enough(a: list[float], b: list[float]) -> bool:
    return abs(a[0] - b[0]) < _JOIN_TOLERANCE_DEG and abs(a[1] - b[1]) < _JOIN_TOLERANCE_DEG


def chain_lines(lines: list[list[list[float]]]) -> list[list[list[float]]]:
    """Greedily join line parts that share endpoints into longer continuous parts.

    Parts that do not connect stay separate. This is what keeps a trail with a real
    gap from being drawn as one continuous line across country it does not cross.
    """
    remaining = [list(line) for line in lines if len(line) >= 2]
    chains: list[list[list[float]]] = []

    while remaining:
        current = remaining.pop(0)
        extended = True
        while extended:
            extended = False
            for i, candidate in enumerate(remaining):
                if _close_enough(current[-1], candidate[0]):
                    current.extend(candidate[1:])
                elif _close_enough(current[-1], candidate[-1]):
                    current.extend(list(reversed(candidate))[1:])
                elif _close_enough(current[0], candidate[-1]):
                    current = candidate[:-1] + current
                elif _close_enough(current[0], candidate[0]):
                    current = list(reversed(candidate))[:-1] + current
                else:
                    continue
                remaining.pop(i)
                extended = True
                break
        chains.append(current)

    return chains


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def geometry_length_miles(parts: list[list[list[float]]]) -> float:
    total = 0.0
    for line in parts:
        for i in range(1, len(line)):
            lng1, lat1 = line[i - 1][0], line[i - 1][1]
            lng2, lat2 = line[i][0], line[i][1]
            total += haversine_miles(lat1, lng1, lat2, lng2)
    return total


def _bbox(parts: list[list[list[float]]]) -> list[float] | None:
    lngs = [c[0] for line in parts for c in line]
    lats = [c[1] for line in parts for c in line]
    if not lngs or not lats:
        return None
    return [min(lngs), min(lats), max(lngs), max(lats)]


def _is_loop(parts: list[list[list[float]]]) -> bool:
    """A trail is a loop when its overall start and end meet."""
    if not parts:
        return False
    start = parts[0][0]
    end = parts[-1][-1]
    return len(parts) == 1 and _close_enough(start, end)


def _majority(values: Iterable[Any]) -> Any | None:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    # Counter needs hashables; dict-valued fields are handled by their callers.
    return Counter(cleaned).most_common(1)[0][0]


# ── Trail assembly ────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def build_trail(trail_cn: str, segments: list[tuple[dict, dict]]) -> dict | None:
    """Aggregate one trail's segments into a single normalized record."""
    props_list = [p for p, _ in segments]

    names = [_clean(p.get("trail_name")) for p in props_list]
    name = _majority(names)
    if not name:
        return None  # unnamed trails are not discoverable; drop rather than invent

    raw_parts: list[list[list[float]]] = []
    for _, geom in segments:
        raw_parts.extend(_segment_lines(geom))
    if not raw_parts:
        return None

    parts = chain_lines(raw_parts)
    bbox = _bbox(parts)
    if not bbox:
        return None

    # Official segment lengths, falling back to measured geometry length.
    reported = [_safe_float(p.get("segment_length")) for p in props_list]
    reported_total = sum(v for v in reported if v is not None)
    measured_total = geometry_length_miles(parts)
    length_miles = round(reported_total or measured_total, 2)

    trail_classes = [normalize_trail_class(p.get("trail_class")) for p in props_list]
    trail_classes = [c for c in trail_classes if c is not None]
    # The most primitive (lowest) class governs how hard the trail is overall.
    trail_class = min(trail_classes) if trail_classes else None

    grades = [normalize_grade(p.get("typical_trail_grade")) for p in props_list]
    grades = [g for g in grades if g]
    grade = max(grades, key=lambda g: g["max_pct"]) if grades else None

    surfaces = [normalize_surface(p.get("trail_surface")) for p in props_list]
    surface = _majority(surfaces)

    mgmt_areas = [normalize_mgmt_area(p.get("special_mgmt_area")) for p in props_list]
    mgmt_area = _majority(mgmt_areas)

    trail_types = [_clean(p.get("trail_type")) for p in props_list]
    trail_type = _majority(trail_types)

    accessibility = _majority([_clean(p.get("accessibility_status")) for p in props_list])

    activities: dict[str, dict] = {}
    for label, prefix in _ACTIVITY_PREFIXES.items():
        for props in props_list:
            state = _activity_state(props, prefix)
            if state:
                activities[label] = state
                break

    hiking = activities.get("hiking") or {}
    season = hiking.get("season")

    admin_org = _majority([_clean(p.get("admin_org")) for p in props_list])
    trail_no = _majority([_clean(p.get("trail_no")) for p in props_list])

    center_lng = (bbox[0] + bbox[2]) / 2
    center_lat = (bbox[1] + bbox[3]) / 2

    return {
        "id": trail_cn,
        "name": name.title(),
        "slug": _slugify(name),
        "trail_no": trail_no,
        "admin_org": admin_org,
        "trail_type": trail_type,
        "length_miles": length_miles,
        "geometry_length_miles": round(measured_total, 2),
        "trail_class": trail_class,
        "trail_class_label": _TRAIL_CLASS_LABEL.get(trail_class) if trail_class else None,
        "grade": grade,
        "surface": surface,
        "mgmt_area": mgmt_area,
        "accessibility": accessibility,
        "activities": activities,
        "season": season,
        "route_type": "loop" if _is_loop(parts) else "out-and-back",
        "bbox": bbox,
        "center": [round(center_lng, 6), round(center_lat, 6)],
        "segment_count": len(segments),
        "part_count": len(parts),
        "geometry": {"type": "MultiLineString", "coordinates": parts},
        # Filled in by later pipeline stages; None means "not yet computed".
        "elevation": None,
        "features": None,
    }


def is_hikeable(trail: dict) -> bool:
    """Exclude trails that are clearly not for walking.

    Deliberately inclusive: a trail is dropped only when the feed positively says
    it is a snow/motorized route or that hiking is restricted. Silence in the feed
    is not treated as prohibition, because most hikeable USFS trails record nothing.
    """
    if trail.get("trail_type") == "SNOW":
        return False
    if (trail.get("surface") or "").lower() == "snow":
        return False
    hiking = (trail.get("activities") or {}).get("hiking") or {}
    if hiking.get("restricted") and not hiking.get("allowed"):
        return False
    return True


def load_raw(source: Path | str | None = None) -> dict:
    path = Path(source) if source else DEFAULT_SOURCE
    if not path.exists():
        raise FileNotFoundError(
            f"Trail source not found at {path}. Set TRAILS_SOURCE or place the "
            "USFS GeoJSON at data/trails.geojson."
        )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def normalize_trails(source: Path | str | None = None) -> list[dict]:
    """Load the raw feed and return normalized, hikeable trail records."""
    raw = load_raw(source)

    groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for feature in raw.get("features", []):
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        # NOTE: the feed uses lowercase keys. Reading TRAIL_CN here yields nothing.
        trail_cn = _clean(props.get("trail_cn")) or _clean(props.get("objectid"))
        if not trail_cn:
            continue
        groups[trail_cn].append((props, geometry))

    trails = []
    for trail_cn, segments in groups.items():
        trail = build_trail(trail_cn, segments)
        if trail and is_hikeable(trail):
            trails.append(trail)

    trails.sort(key=lambda t: t["name"])
    return trails


if __name__ == "__main__":
    import sys

    result = normalize_trails(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"normalized {len(result)} hikeable trails")
    for trail in result[:3]:
        print(json.dumps({k: v for k, v in trail.items() if k != "geometry"}, indent=2))
