"""Load and normalize trail data from the local GeoJSON dataset."""

import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

_BASE_DIR = Path(__file__).resolve().parent


def _resolve_source_path() -> Path:
    env_source = os.environ.get("TRAILS_SOURCE", "").strip()
    if env_source:
        p = Path(env_source).expanduser()
        if not p.is_absolute():
            p = (_BASE_DIR / p).resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"TRAILS_SOURCE points to missing file: {p}")

    preferred = _BASE_DIR / "National_Forest_System_Trails_(Feature_Layer) (3).geojson"
    if preferred.exists():
        return preferred

    geojson_files = sorted(_BASE_DIR.glob("*.geojson"))
    if geojson_files:
        return geojson_files[0]

    raise FileNotFoundError(
        "No GeoJSON trail source found. Place a .geojson file in the project root "
        "or set TRAILS_SOURCE to the file path."
    )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return slug.strip("-")


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _first_coord(geometry: dict) -> tuple[float, float] | None:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "LineString" and isinstance(coords, list) and coords:
        c = coords[0]
        if isinstance(c, list) and len(c) >= 2:
            return float(c[1]), float(c[0])
    if gtype == "MultiLineString" and isinstance(coords, list) and coords and coords[0]:
        c = coords[0][0]
        if isinstance(c, list) and len(c) >= 2:
            return float(c[1]), float(c[0])
    return None


def _is_loop(geometry: dict) -> bool:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    line = None
    if gtype == "LineString" and isinstance(coords, list) and len(coords) >= 2:
        line = coords
    elif gtype == "MultiLineString" and isinstance(coords, list) and coords and len(coords[0]) >= 2:
        line = coords[0]
    if not line:
        return False
    x1, y1 = line[0][0], line[0][1]
    x2, y2 = line[-1][0], line[-1][1]
    return abs(x1 - x2) < 0.001 and abs(y1 - y2) < 0.001


def _difficulty_from_class(trail_class: str) -> str:
    # USFS Trail Class: lower number is more primitive/challenging.
    mapping = {"1": "very hard", "2": "hard", "3": "moderate", "4": "easy", "5": "easy"}
    return mapping.get(str(trail_class).strip(), "moderate")


def _feature_tags(name: str, area: str, mgmt: str, surface: str) -> list[str]:
    text = " ".join([name or "", area or "", mgmt or "", surface or ""]).lower()
    tags = []
    keywords = {
        "lake": "lake",
        "falls": "waterfall",
        "waterfall": "waterfall",
        "river": "river",
        "creek": "river",
        "ridge": "views",
        "peak": "views",
        "view": "views",
        "forest": "forest",
        "wilderness": "wildlife",
        "wild": "wildlife",
    }
    for k, tag in keywords.items():
        if k in text:
            tags.append(tag)
    return sorted(set(tags))


def _collect_geometry(geometry: dict) -> list[tuple[float, float]]:
    """Collect all coordinates from a geometry as (lat, lng) pairs."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    result = []
    if gtype == "LineString" and isinstance(coords, list):
        for c in coords:
            if isinstance(c, list) and len(c) >= 2:
                result.append((float(c[1]), float(c[0])))
    elif gtype == "MultiLineString" and isinstance(coords, list):
        for line in coords:
            for c in line:
                if isinstance(c, list) and len(c) >= 2:
                    result.append((float(c[1]), float(c[0])))
    return result


def _aggregate_group(trail_id: str, segs: list[tuple[dict, dict]]) -> dict:
    names = [str(p.get("TRAIL_NAME", "")).strip() for p, _ in segs if str(p.get("TRAIL_NAME", "")).strip()]
    name = Counter(names).most_common(1)[0][0] if names else f"Unnamed Trail {trail_id}"

    areas = []
    for p, _ in segs:
        for k in ("ADMIN_ORG", "MANAGING_ORG", "SPECIAL_MGMT_AREA"):
            v = str(p.get(k, "")).strip()
            if v:
                areas.append(v)
                break
    area = Counter(areas).most_common(1)[0][0] if areas else "Unknown Area"

    coords = [_first_coord(g) for _, g in segs]
    coords = [c for c in coords if c is not None]
    if coords:
        lat = round(sum(c[0] for c in coords) / len(coords), 6)
        lng = round(sum(c[1] for c in coords) / len(coords), 6)
    else:
        lat = 0.0
        lng = 0.0

    all_geom_coords = []
    for _, g in segs:
        all_geom_coords.extend(_collect_geometry(g))

    geometry = None
    if all_geom_coords:
        geometry = {
            "type": "LineString",
            "coordinates": [[c[1], c[0]] for c in all_geom_coords]
        }

    length_miles = round(sum(_safe_float(p.get("SEGMENT_LENGTH", 0.0)) for p, _ in segs), 1)
    route_type = "loop" if any(_is_loop(g) for _, g in segs) else "out-and-back"

    classes = [str((p.get("TRAIL_CLASS", "") or "")).strip() for p, _ in segs if str(p.get("TRAIL_CLASS", "")).strip()]
    trail_class = min(classes) if classes else ""
    difficulty = _difficulty_from_class(trail_class)

    surface = next((str(p.get("TRAIL_SURFACE", "")).strip() for p, _ in segs if str(p.get("TRAIL_SURFACE", "")).strip()), "")
    mgmt = next((str(p.get("SPECIAL_MGMT_AREA", "")).strip() for p, _ in segs if str(p.get("SPECIAL_MGMT_AREA", "")).strip()), "")
    features = _feature_tags(name=name, area=area, mgmt=mgmt, surface=surface)

    return {
        "id": trail_id,
        "name": name,
        "area": area,
        "city": "",
        "lat": lat,
        "lng": lng,
        "length_miles": max(0.1, length_miles),
        "elev_gain_ft": 0,
        "difficulty": difficulty,
        "route_type": route_type,
        "avg_rating": 0,
        "num_reviews": 0,
        "popularity": 0.0,
        "features": features,
        "activities": ["hiking", "backpacking"],
        "visitor_usage": "",
        "slug": _slugify(name),
        "geometry": geometry,
    }


def load_backpacking_trails() -> list[dict]:
    """Return normalized trails from the local GeoJSON (grouped by TRAIL_CN)."""
    source_path = _resolve_source_path()
    with open(source_path, encoding="utf-8") as f:
        raw = json.load(f)

    groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for feature in raw.get("features", []):
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        trail_id = str(props.get("TRAIL_CN") or props.get("OBJECTID") or "").strip()
        if not trail_id:
            continue
        groups[trail_id].append((props, geom))

    trails = [_aggregate_group(tid, segs) for tid, segs in groups.items() if segs]
    return trails


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
