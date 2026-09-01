"""National Park Service trails — the coverage the USFS feed cannot provide.

The USFS National Forest System feed contains no Yosemite, Redwood, Joshua Tree,
Lassen, Point Reyes, or Death Valley trails. This layer does, it is a US federal
work (public domain, 17 USC 105), and it needs no API key.

Verified against the live service: 5,757 features intersect the California bbox,
4,916 of them (85%) carry a human-readable `TRLNAME`.

Known gap: Sequoia & Kings Canyon (SEKI) returns zero records from this national
layer. `SEKI_FALLBACK_URL` points at a re-hosted extract with the same schema, but
its provenance is unconfirmed, so it is opt-in rather than fetched by default.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

import requests

from .normalize import _slugify, chain_lines, geometry_length_miles, parse_season

NPS_TRAILS_URL = (
    "https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/"
    "NPS_Public_Trails_Geographic/MapServer/0/query"
)
NPS_POIS_URL = (
    "https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/"
    "NPS_Public_POIs_Geographic/MapServer/0/query"
)

# Opt-in: schema matches the national layer but this is a third-party rehost.
SEKI_FALLBACK_URL = (
    "https://services.arcgis.com/HRPe58bUyBqyyiCt/arcgis/rest/services/"
    "seki_trails/FeatureServer/0/query"
)

CALIFORNIA_BBOX = (-124.5, 32.5, -114.1, 42.1)

# The bbox corners reach into Nevada and Oregon, so filter by park unit as well.
# A whitelist rather than a blocklist: a unit added upstream in another state is
# then excluded by default instead of silently appearing in California results.
# (Death Valley spans the CA/NV line but is administered as, and mostly is, California.)
CALIFORNIA_UNITS = {
    "SAMO",  # Santa Monica Mountains NRA
    "YOSE",  # Yosemite NP
    "REDW",  # Redwood NSP
    "GOGA",  # Golden Gate NRA
    "JOTR",  # Joshua Tree NP
    "LAVO",  # Lassen Volcanic NP
    "CHIS",  # Channel Islands NP
    "PORE",  # Point Reyes National Seashore
    "WHIS",  # Whiskeytown NRA
    "LABE",  # Lava Beds NM
    "DEVA",  # Death Valley NP
    "RORI",  # Rosie the Riveter WWII Home Front NHP
    "MUWO",  # Muir Woods NM
    "PINN",  # Pinnacles NP
    "JOMU",  # John Muir NHS
    "MANZ",  # Manzanar NHS
    "MOJA",  # Mojave National Preserve
    "DEPO",  # Devils Postpile NM
    "SAFR",  # San Francisco Maritime NHP
    "CABR",  # Cabrillo NM
    "EUON",  # Eugene O'Neill NHS
    "SEKI",  # Sequoia & Kings Canyon (empty upstream; kept for the fallback source)
}

PAGE_SIZE = 1000
MAX_PAGES = 30

_CACHE_DIR = Path(os.environ.get("NPS_CACHE_DIR", Path(__file__).resolve().parent.parent / ".nps_cache"))

# Trail statuses that mean the trail is not currently walkable.
_EXCLUDED_STATUS = {"Decommissioned", "Proposed", "Temporarily Closed"}


def _fetch_paged(url: str, bbox, out_fields: str = "*", verbose: bool = True) -> list[dict]:
    """Page an ArcGIS layer over a bbox, returning GeoJSON features."""
    features: list[dict] = []
    offset = 0

    for page in range(MAX_PAGES):
        params = {
            "where": "1=1",
            "geometry": ",".join(str(v) for v in bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
        }
        response = requests.get(url, params=params, timeout=120)
        response.raise_for_status()
        payload = response.json()

        batch = payload.get("features") or []
        features.extend(batch)
        if verbose:
            print(f"  page {page + 1}: +{len(batch)} (total {len(features)})")

        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
        time.sleep(0.4)

    return features


def fetch_trails(bbox=CALIFORNIA_BBOX, use_cache: bool = True, verbose: bool = True) -> list[dict]:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / "nps_trails_ca.json"
    if use_cache and cache.exists():
        try:
            features = json.loads(cache.read_text())
            if verbose:
                print(f"  loaded {len(features)} cached NPS features")
            return features
        except Exception:
            pass

    if verbose:
        print("  fetching NPS trails…")
    features = _fetch_paged(NPS_TRAILS_URL, bbox, verbose=verbose)
    cache.write_text(json.dumps(features))
    return features


def fetch_pois(bbox=CALIFORNIA_BBOX, use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """Trailheads, viewpoints, waterfalls, water, and parking inside park units."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / "nps_pois_ca.json"
    if use_cache and cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    features = _fetch_paged(NPS_POIS_URL, bbox, verbose=verbose)
    cache.write_text(json.dumps(features))
    return features


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "n/a", "null", "unknown", "not specified"}:
        return None
    return text


def _activities_from_use(trluse: str | None) -> dict:
    """`TRLUSE` is a pipe-delimited list like 'Hiker/Pedestrian|Bicycle'."""
    if not trluse:
        return {}
    mapping = {
        "hiker/pedestrian": "hiking",
        "pack and saddle": "horse",
        "bicycle": "bike",
        "cross-country ski": "xc_ski",
        "snowshoe": "snowshoe",
        "motorcycle": "motorcycle",
        "atv": "atv",
        "watercraft": "watercraft",
    }
    activities: dict[str, dict] = {}
    for part in trluse.split("|"):
        key = mapping.get(part.strip().lower())
        if key:
            activities[key] = {"allowed": True, "restricted": None, "season": None}
    return activities


def _lines(geometry: dict | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString" and len(coords) >= 2:
        return [coords]
    if gtype == "MultiLineString":
        return [line for line in coords if isinstance(line, list) and len(line) >= 2]
    return []


def normalize_nps(
    features: list[dict],
    verbose: bool = True,
    include_unnamed: bool = True,
) -> list[dict]:
    """Group NPS segments into trail records matching the USFS-normalized schema.

    `include_unnamed` keeps segments that carry no TRLNAME. This matters entirely
    because of Yosemite: only 45% of its segments are named (every other California
    park is 95-100%), and the unnamed ones are not connectors — they are
    TRLFEATTYPE="Park Trail", Class 3, hiker/pack-stock, and account for roughly
    62% of the park's trail mileage. Dropping them left the map showing about a
    third of Yosemite.

    They are kept with `named: False` and an explicit "Unnamed trail" label rather
    than a name guessed from a nearby GNIS feature. Inventing a name would be the
    same failure this project has been correcting throughout: presenting something
    unknown as if it were known.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    unnamed: list[dict] = []

    for feature in features:
        props = feature.get("properties") or {}
        if _clean(props.get("TRLSTATUS")) in _EXCLUDED_STATUS:
            continue
        unit = _clean(props.get("UNITCODE"))
        if unit not in CALIFORNIA_UNITS:
            continue  # drops Great Basin (NV), Oregon Caves (OR), Lake Mead, Tule Springs

        name = _clean(props.get("TRLNAME")) or _clean(props.get("TRLALTNAME"))
        if not name:
            if include_unnamed:
                unnamed.append(feature)
            continue
        groups[(unit, name.lower())].append(feature)

    trails = []
    for (unit, _), segments in groups.items():
        props = segments[0].get("properties") or {}
        name = _clean(props.get("TRLNAME")) or _clean(props.get("TRLALTNAME"))

        raw_parts: list[list[list[float]]] = []
        for segment in segments:
            raw_parts.extend(_lines(segment.get("geometry")))
        if not raw_parts:
            continue

        parts = chain_lines(raw_parts)
        lngs = [c[0] for line in parts for c in line]
        lats = [c[1] for line in parts for c in line]
        if not lngs:
            continue
        bbox = [min(lngs), min(lats), max(lngs), max(lats)]

        length_miles = round(geometry_length_miles(parts), 2)
        if length_miles <= 0:
            continue

        activities = {}
        for segment in segments:
            activities.update(_activities_from_use(_clean((segment.get("properties") or {}).get("TRLUSE"))))

        season = parse_season(_clean(props.get("SEASONAL")))
        surface = _clean(props.get("TRLSURFACE"))

        trails.append(
            {
                # Namespaced so NPS ids can never collide with USFS trail_cn values.
                "id": f"nps:{unit}:{_slugify(name)}",
                "name": name.title(),
                "slug": _slugify(name),
                "trail_no": None,
                "admin_org": "NPS",
                "trail_type": "TERRA",
                "source": "NPS Public Trails",
                "length_miles": length_miles,
                "geometry_length_miles": length_miles,
                "trail_class": None,
                "trail_class_label": None,
                "grade": None,
                "surface": surface.title() if surface else None,
                "mgmt_area": _clean(props.get("UNITNAME")),
                "accessibility": None,
                "activities": activities,
                "season": season,
                "season_note": _clean(props.get("SEASDESC")),
                "route_type": "out-and-back",
                "bbox": bbox,
                "center": [round((bbox[0] + bbox[2]) / 2, 6), round((bbox[1] + bbox[3]) / 2, 6)],
                "segment_count": len(segments),
                "part_count": len(parts),
                "geometry": {"type": "MultiLineString", "coordinates": parts},
                "elevation": None,
                "features": None,
            }
        )

    if unnamed:
        trails.extend(_unnamed_networks(unnamed, verbose=verbose))

    trails.sort(key=lambda t: t["name"])
    if verbose:
        named_count = sum(1 for t in trails if t.get("named", True))
        print(
            f"  normalized {len(trails)} NPS trails from {len(features)} segments "
            f"({named_count} named, {len(trails) - named_count} unnamed)"
        )
    return trails


def _unnamed_networks(features: list[dict], verbose: bool = True) -> list[dict]:
    """Chain unnamed segments per park into connected trail runs.

    Chaining first means a continuous stretch of unnamed backcountry trail becomes
    one walkable record rather than dozens of disconnected fragments.
    """
    by_unit: dict[str, list[list[list[float]]]] = defaultdict(list)
    unit_names: dict[str, str] = {}

    for feature in features:
        props = feature.get("properties") or {}
        unit = _clean(props.get("UNITCODE")) or "NPS"
        unit_names[unit] = _clean(props.get("UNITNAME")) or unit
        by_unit[unit].extend(_lines(feature.get("geometry")))

    records = []
    for unit, raw_parts in by_unit.items():
        park = unit_names.get(unit, unit)
        for index, part in enumerate(chain_lines(raw_parts), start=1):
            length_miles = round(geometry_length_miles([part]), 2)
            # Sub-quarter-mile fragments are genuinely connectors, not routes.
            if length_miles < 0.25:
                continue
            lngs = [c[0] for c in part]
            lats = [c[1] for c in part]
            bbox = [min(lngs), min(lats), max(lngs), max(lats)]
            records.append(
                {
                    "id": f"nps:{unit}:unnamed-{index}",
                    "name": f"Unnamed trail ({park})",
                    "named": False,
                    "slug": f"{unit.lower()}-unnamed-{index}",
                    "trail_no": None,
                    "admin_org": "NPS",
                    "trail_type": "TERRA",
                    "source": "NPS Public Trails",
                    "length_miles": length_miles,
                    "geometry_length_miles": length_miles,
                    "trail_class": None,
                    "trail_class_label": None,
                    "grade": None,
                    "surface": None,
                    "mgmt_area": park,
                    "accessibility": None,
                    "activities": {"hiking": {"allowed": True, "restricted": None, "season": None}},
                    "season": None,
                    "season_note": None,
                    "route_type": "out-and-back",
                    "bbox": bbox,
                    "center": [round((bbox[0] + bbox[2]) / 2, 6), round((bbox[1] + bbox[3]) / 2, 6)],
                    "segment_count": 1,
                    "part_count": 1,
                    "geometry": {"type": "MultiLineString", "coordinates": [part]},
                    "elevation": None,
                    "features": None,
                }
            )

    if verbose and records:
        miles = sum(r["length_miles"] for r in records)
        print(f"  + {len(records)} unnamed trail runs ({miles:,.0f} mi) kept as map geometry")
    return records


def poi_records(features: list[dict]) -> list[dict]:
    """NPS POIs in the shape the scenery grid expects."""
    kind_map = {
        "waterfall": "waterfall",
        "viewpoint": "viewpoint",
        "scenic overlook": "viewpoint",
        "trailhead": "trailhead",
        "parking lot": "parking",
        "drinking water": "water",
    }
    records = []
    for feature in features:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            continue
        poi_type = (_clean(props.get("POITYPE")) or "").lower()
        kind = kind_map.get(poi_type)
        if not kind:
            continue
        records.append(
            {
                "id": f"nps-poi:{props.get('OBJECTID')}",
                "kind": kind,
                "name": _clean(props.get("POINAME")),
                "lat": float(coords[1]),
                "lng": float(coords[0]),
            }
        )
    return records


def fetch_seki(use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """Sequoia & Kings Canyon trails from the fallback layer.

    SEKI returns zero records from the NPS national layer — verified by both a
    UNITNAME query and a spatial bbox query. This re-hosted extract carries 772
    trails including Mount Whitney. Its schema is a subset of the national layer
    (TRLNAME / TRLCLASS / TRLSURFACE) and its provenance is third-party, so records
    are tagged `source: "SEKI fallback (third-party rehost)"` and stay
    distinguishable in the index rather than being passed off as NPS-authoritative.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / "seki_trails.json"
    if use_cache and cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    features: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
        }
        response = requests.get(SEKI_FALLBACK_URL, params=params, timeout=120)
        response.raise_for_status()
        batch = response.json().get("features") or []
        features.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)

    cache.write_text(json.dumps(features))
    if verbose:
        print(f"  fetched {len(features)} SEKI features")
    return features


def normalize_seki(features: list[dict], verbose: bool = True) -> list[dict]:
    """Group the SEKI fallback layer into trail records."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for feature in features:
        props = feature.get("properties") or {}
        name = _clean(props.get("TRLNAME"))
        if not name:
            continue
        groups[name.lower()].append(feature)

    trails = []
    for segments in groups.values():
        props = segments[0].get("properties") or {}
        name = _clean(props.get("TRLNAME"))

        raw_parts: list[list[list[float]]] = []
        for segment in segments:
            raw_parts.extend(_lines(segment.get("geometry")))
        if not raw_parts:
            continue

        parts = chain_lines(raw_parts)
        lngs = [c[0] for line in parts for c in line]
        lats = [c[1] for line in parts for c in line]
        if not lngs:
            continue
        bbox = [min(lngs), min(lats), max(lngs), max(lats)]
        length_miles = round(geometry_length_miles(parts), 2)
        if length_miles <= 0:
            continue

        surface = _clean(props.get("TRLSURFACE"))
        trails.append(
            {
                "id": f"nps:SEKI:{_slugify(name)}",
                "name": name.title(),
                "named": True,
                "slug": _slugify(name),
                "trail_no": None,
                "admin_org": "NPS",
                "trail_type": "TERRA",
                "source": "SEKI fallback (third-party rehost)",
                "length_miles": length_miles,
                "geometry_length_miles": length_miles,
                "trail_class": None,
                "trail_class_label": None,
                "grade": None,
                "surface": surface.title() if surface else None,
                "mgmt_area": "Sequoia & Kings Canyon National Parks",
                "accessibility": None,
                "activities": {"hiking": {"allowed": True, "restricted": None, "season": None}},
                "season": None,
                "season_note": None,
                "route_type": "out-and-back",
                "bbox": bbox,
                "center": [round((bbox[0] + bbox[2]) / 2, 6), round((bbox[1] + bbox[3]) / 2, 6)],
                "segment_count": len(segments),
                "part_count": len(parts),
                "geometry": {"type": "MultiLineString", "coordinates": parts},
                "elevation": None,
                "features": None,
            }
        )

    if verbose:
        print(f"  normalized {len(trails)} SEKI trails from {len(features)} segments")
    return trails


if __name__ == "__main__":
    features = fetch_trails()
    trails = normalize_nps(features)
    print(f"\n{len(trails)} NPS trails")
    from collections import Counter

    for park, count in Counter(t["mgmt_area"] for t in trails).most_common(12):
        print(f"  {park}: {count}")
