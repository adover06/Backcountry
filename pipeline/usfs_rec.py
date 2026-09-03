"""USFS developed recreation sites — the trailheads the pipeline was missing.

`HANDOFF.md` recorded the gap: only 990 of 10,694 trails had a trailhead, because
NPS was the only source publishing them and NPS only covers park units. The USFS
half of the index — 8,162 trails across 18 national forests — had no start points
at all, which blocked both "where do I park" and `trail_graph`, whose composed
hikes have to begin somewhere.

USFS does publish them, just not under a name you would search for. There is no
trailheads layer in EDW; trailheads are one `site_type` inside the developed
recreation sites layer, alongside campgrounds and everything else the agency
maintains as infrastructure. California holds 3,056 sites:

    TRAILHEAD        625      CAMPGROUND       876      CAMPING AREA     282
    PICNIC SITE      250      DAY USE AREA     183      GROUP CAMPGROUND 132
    BOATING SITE     134      OBSERVATION       56      HORSE CAMP        20

So one layer closes four separate gaps: trailheads (625, against 448 from NPS),
camping (1,310 across four site types, where the plan had been 579 campgrounds from
RIDB), day-use parking, and scenery anchors (observation, swimming, climbing,
lookout). It needs no API key and is a US federal work — public domain.

The records are also unusually rich for agency data. A campground here carries fee,
drinking water, restrooms, season dates, capacity, and a Recreation.gov link, which
is most of what a hiker wants to know and none of which the trail geometry holds.
Those ride along on the joined record rather than being discarded.

Two things about the feed that shaped the parsing:

* Absent values arrive as the **string** `"No Data"`, not null, and also as
  `"None"`, `"Unknown"`, and `""`. Passing those through would violate the rule
  that missing is never rendered as a value — a campground would claim its water
  availability is the text "No Data".
* `site_type` is a controlled vocabulary of 33 values, most of which are not
  hiking-relevant (TARGET RANGE, RECREATION RESIDENCE, DUMP STATION). Mapping is a
  whitelist, so a new upstream type is ignored until someone decides where it goes,
  rather than silently entering the index as an unknown kind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Identical ArcGIS query contract to the NPS layers, so the pager is shared rather
# than reimplemented; only the URL and the field names differ.
from .nps import CALIFORNIA_BBOX, _fetch_paged

USFS_REC_SITES_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
    "EDW_RecInfraRecreationSites_02/MapServer/0/query"
)

_CACHE_DIR = Path(
    os.environ.get(
        "USFS_CACHE_DIR", Path(__file__).resolve().parent.parent / ".usfs_cache"
    )
)

# Whitelist: upstream site types mapped to the kinds this pipeline reasons about.
# Types with no hiking relevance are deliberately absent and therefore dropped.
_SITE_KIND = {
    "TRAILHEAD": "trailhead",
    "CAMPGROUND": "campground",
    "CAMPING AREA": "campground",
    "GROUP CAMPGROUND": "campground",
    "HORSE CAMP": "campground",
    "DAY USE AREA": "day_use",
    "PICNIC SITE": "picnic",
    "GROUP PICNIC SITE": "picnic",
    "OHV STAGING AREA": "staging",
    "SNOWPARK": "snowpark",
    "OBSERVATION SITE": "viewpoint",
    "SWIMMING SITE": "swimming",
    "FISHING SITE": "fishing",
    "CLIMBING AREA": "climbing",
    "LOOKOUT/CABIN": "lookout",
    "INTERPRETIVE SITE": "interpretive",
    "INTERPRETIVE VISITOR CENTER (MAJOR)": "visitor_center",
    "INTERPRETIVE VISITOR CENTER (MINOR)": "visitor_center",
    "INFO SITE/FEE STATION": "visitor_center",
}

# Facility attributes worth carrying onto the joined trail record. Everything else
# in the 70-column feed is administrative (CRC checksums, org codes, update stamps).
_DETAIL_FIELDS = {
    "fee_charged": "fee",
    "fee_description": "fee_description",
    "water_availability": "water",
    "restroom_availability": "restroom",
    "open_season": "open_season",
    "season_description": "season_description",
    "operational_hours": "hours",
    "usage_level": "usage_level",
    "total_capacity": "capacity",
    "closest_towns": "closest_towns",
    "directions": "directions",
    "restrictions": "restrictions",
    "permit_information": "permit_info",
    "passes": "passes",
    "rec1stop_url": "reservation_url",
    "usda_portal_url": "info_url",
    "seasonal_operational_status": "status",
}

# The feed spells absence several ways, all of them as text.
_MISSING = {"", "no data", "none", "n/a", "null", "unknown", "not specified", "nan"}


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _MISSING else text


def fetch_sites(
    bbox=CALIFORNIA_BBOX, use_cache: bool = True, verbose: bool = True
) -> list[dict]:
    """All USFS developed recreation sites intersecting the bbox, cached."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / "usfs_rec_sites_ca.json"

    if use_cache and cache.exists():
        try:
            features = json.loads(cache.read_text())
            if verbose:
                print(f"  loaded {len(features)} cached USFS recreation sites")
            return features
        except Exception:
            pass

    if verbose:
        print("  fetching USFS recreation sites…")
    features = _fetch_paged(USFS_REC_SITES_URL, bbox, verbose=verbose)
    cache.write_text(json.dumps(features))
    return features


def site_records(features: list[dict]) -> list[dict]:
    """USFS sites in the shape the access grid expects.

    `details` holds the facility attributes that survive cleaning. It is omitted
    entirely when nothing survives, so a caller can distinguish "no details" from
    a dict of nulls.
    """
    records: list[dict] = []

    for feature in features:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            continue

        site_type = (_clean(props.get("site_type")) or "").upper()
        kind = _SITE_KIND.get(site_type)
        if not kind:
            continue

        # public_site_name is the visitor-facing form ("Goldledge Campground");
        # site_name is the operational one ("GOLDLEDGE"). Prefer the former.
        name = (
            _clean(props.get("public_site_name"))
            or _clean(props.get("recarea_name"))
            or _clean(props.get("site_name"))
        )

        details = {}
        for source_field, out_key in _DETAIL_FIELDS.items():
            cleaned = _clean(props.get(source_field))
            if cleaned is not None:
                details[out_key] = cleaned

        record = {
            "id": f"usfs-rec:{props.get('site_cn') or props.get('objectid')}",
            "kind": kind,
            "name": name,
            "lat": float(coords[1]),
            "lng": float(coords[0]),
            "source": "USFS INFRA recreation sites",
            "site_type": site_type,
        }
        if details:
            record["details"] = details
        records.append(record)

    return records


def load_records(use_cache: bool = True, verbose: bool = True) -> list[dict]:
    return site_records(fetch_sites(use_cache=use_cache, verbose=verbose))


if __name__ == "__main__":
    import collections

    recs = load_records()
    print(f"\n{len(recs)} usable USFS recreation sites")
    for kind, count in collections.Counter(r["kind"] for r in recs).most_common():
        print(f"  {kind:16} {count}")
    detailed = sum(1 for r in recs if r.get("details"))
    print(f"\n{detailed} carry facility details")
    named = sum(1 for r in recs if r.get("name"))
    print(f"{named} are named")
