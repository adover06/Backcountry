"""Trailheads, drinking water and shelters from OpenStreetMap.

`usfs_rec.py` and `nps.py` between them cover federal land the agencies administer,
which is most of California's trail mileage but not most of its trailheads. The
agency layers hold 1,073 trailheads (625 USFS + 448 NPS); OSM holds 1,175 tagged
`highway=trailhead` on its own, plus another 243 parking areas named as trailheads,
across state parks, regional open space districts, county parks and city preserves —
the places nobody federal publishes a point for.

Camping is the third gap. `usfs_rec.py` supplies 1,310 *developed* sites, which is
what an agency maintains and bills for. OSM carries 4,215 `tourism=camp_site`, and
the ones tagged `backcountry=yes` are the primitive and dispersed sites that no
agency inventories as infrastructure — precisely the ones a backcountry app exists
to surface. Developed and backcountry stay separate kinds; a walk-in site with no
water is not a campground with flush toilets, and merging them would flatten the
distinction a trip actually turns on.

Drinking water is the starker gap. The index reached 1.4% water coverage from the
62 NPS points, in an app whose whole subject is walking into places where water
matters. OSM carries **5,601** `amenity=drinking_water` nodes statewide.

Deliberately narrow. This module answers "where do I start, park, drink and shelter",
so it takes only tags that mean exactly that:

* `natural=spring` is **not** here despite 17,384 of them statewide. Springs are
  already collected by `enrich_osm.py` as scenery, and a spring is not drinking
  water — it is untreated surface water that may be seasonal or dry. Promoting it
  into an access field would tell a hiker there is water where there may be none,
  which is the exact failure mode the pipeline's governing rule exists to prevent.
* Parking is admitted only when the name says trailhead. Every supermarket car park
  in California is `amenity=parking`, and a 0.5 mi radius around an urban trail
  would collect them all.

Data is ODbL. Personal-use and undistributed, so share-alike does not trigger;
attribute OpenStreetMap contributors if that changes.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .enrich_osm import (
    CALIFORNIA_BBOX,
    REQUEST_PAUSE_SECONDS,
    _CACHE_DIR,
    _overpass,
)
from .spatial import bbox_tiles

# Larger than the scenery sweep's 1 degree. Measured: a tile costs ~55s regardless
# of how much it returns, because the time is Overpass queue latency rather than
# data. Four 1 degree tiles are therefore four times the wait for the same ground,
# and 2 degrees keeps the whole state to 30 requests.
ACCESS_TILE_DEG = float(os.environ.get("OSM_ACCESS_TILE_STEP", "2.0"))

_BASE_DIR = Path(__file__).resolve().parent.parent
OSM_ACCESS_PATH = _BASE_DIR / "data" / "osm_access_points.json"

# Substring match, deliberately: "Bear Gulch Trailhead", "TRAILHEAD - Mist Falls"
# and "Trail Head Parking" all qualify. It does not exclude a car park named
# "Trailhead Road Lot", and that is an acceptable false positive — the alternative
# is anchoring, which drops the many real trailheads that carry a suffix.
_TRAILHEAD_NAME = re.compile(r"trail\s?head", re.IGNORECASE)


def _access_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL for access infrastructure, not scenery."""
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    box = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:180];
(
  node["highway"="trailhead"]({box});
  way["highway"="trailhead"]({box});
  node["amenity"="parking"]["name"~"[Tt]rail ?[Hh]ead"]({box});
  way["amenity"="parking"]["name"~"[Tt]rail ?[Hh]ead"]({box});
  node["amenity"="drinking_water"]({box});
  node["tourism"="wilderness_hut"]({box});
  way["tourism"="wilderness_hut"]({box});
  node["amenity"="shelter"]["shelter_type"~"basic_hut|lean_to|weather_shelter"]({box});
  node["tourism"="camp_site"]({box});
  way["tourism"="camp_site"]({box});
);
out center tags;
"""


def _classify(tags: dict) -> str | None:
    if tags.get("highway") == "trailhead":
        return "trailhead"
    if tags.get("amenity") == "parking":
        # The query already filtered on the name; this guards against a mirror
        # returning a looser match than asked for.
        return "trailhead" if _TRAILHEAD_NAME.search(tags.get("name") or "") else None
    if tags.get("amenity") == "drinking_water":
        return "water"
    if tags.get("tourism") == "wilderness_hut" or tags.get("amenity") == "shelter":
        return "shelter"
    if tags.get("tourism") == "camp_site":
        # backcountry=yes is the tag that separates a walk-in primitive site from a
        # drive-in campground, and the difference decides whether a trip is possible.
        backcountry = (tags.get("backcountry") or "").lower()
        return "backcountry_camp" if backcountry == "yes" else "campground"
    return None


def fetch_access(
    bbox: tuple[float, float, float, float] = CALIFORNIA_BBOX,
    step_deg: float = ACCESS_TILE_DEG,
    verbose: bool = True,
) -> list[dict]:
    """Fetch access infrastructure across a bbox, caching each tile."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tiles = bbox_tiles(bbox, step_deg)
    records: list[dict] = []
    failures = 0

    for index, tile in enumerate(tiles, start=1):
        name = "_".join(f"{v:.2f}" for v in tile).replace("-", "m")
        # Versioned: the tile cache is keyed by area, so a query change has to
        # invalidate it or old tiles silently answer a question they never asked.
        path = Path(_CACHE_DIR) / f"access_v2_{name}.json"

        elements = None
        if path.exists():
            try:
                elements = json.loads(path.read_text())
            except Exception:
                elements = None

        if elements is None:
            try:
                elements = _overpass(_access_query(tile)).get("elements", [])
                path.write_text(json.dumps(elements))
                if verbose:
                    print(f"  [{index}/{len(tiles)}] fetched {len(elements):>5} {tile}")
                time.sleep(REQUEST_PAUSE_SECONDS)
            except Exception as exc:
                # A failed tile is a miss, never "no access points here".
                failures += 1
                if verbose:
                    print(f"  [{index}/{len(tiles)}] FAILED {tile}: {exc}")
                continue

        for element in elements:
            tags = element.get("tags") or {}
            kind = _classify(tags)
            if not kind:
                continue
            if element.get("type") == "node":
                lat, lng = element.get("lat"), element.get("lon")
            else:
                center = element.get("center") or {}
                lat, lng = center.get("lat"), center.get("lon")
            if lat is None or lng is None:
                continue

            record = {
                "id": f"osm:{element.get('type')}/{element.get('id')}",
                "kind": kind,
                "name": tags.get("name"),
                "lat": float(lat),
                "lng": float(lng),
                "source": "OpenStreetMap",
            }
            details = {}
            for tag, key in (
                ("fee", "fee"),
                ("access", "access"),
                ("capacity", "capacity"),
                ("operator", "operator"),
                ("drinking_water", "potable"),
                ("seasonal", "seasonal"),
                ("backcountry", "backcountry"),
                ("tents", "tents"),
            ):
                if tags.get(tag):
                    details[key] = tags[tag]
            if details:
                record["details"] = details
            records.append(record)

    if verbose and failures:
        print(f"  {failures}/{len(tiles)} tiles failed; coverage is partial")
    return records


def save_records(records: list[dict]) -> Path:
    """Persist to data/ so `access.py` can merge without triggering a fetch.

    The statewide Overpass sweep takes tens of minutes against congested public
    mirrors. A build must never pay that implicitly, so this stage is run on its
    own and its output is what the join reads.
    """
    OSM_ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OSM_ACCESS_PATH.write_text(json.dumps(records))
    return OSM_ACCESS_PATH


def load_saved() -> list[dict]:
    """Previously fetched OSM access points, or [] if the sweep has not been run."""
    if not OSM_ACCESS_PATH.exists():
        return []
    try:
        return json.loads(OSM_ACCESS_PATH.read_text())
    except Exception:
        return []


if __name__ == "__main__":
    import collections

    recs = fetch_access()
    save_records(recs)
    print(f"\n{len(recs)} OSM access points")
    for kind, count in collections.Counter(r["kind"] for r in recs).most_common():
        named = sum(1 for r in recs if r["kind"] == kind and r.get("name"))
        print(f"  {kind:12} {count:6}  ({named} named)")
