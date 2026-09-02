"""Permits and quotas from Recreation.gov (RIDB).

The one thing no other source in this pipeline provides: whether you are actually
allowed to walk the trail on the day you want. Half Dome cables, Mt Whitney, and
most Sierra wilderness entries are lottery- or quota-controlled, and a discovery
app that surfaces a hike without saying "this needs a permit you had to win in
March" is giving advice that cannot be acted on.

RIDB models facilities and permits, never trail geometry — it is a reservations
database, not a trails one. So this joins permit-issuing facilities to trails by
proximity rather than expecting any shared identifier.

    Requires a free key: https://ridb.recreation.gov/profile  ->  API Key
    Then:  RIDB_API_KEY=... in .env

Verified against the live API. Findings that changed the implementation:

* `/permitentrances` looks like the obvious endpoint and is not: 907 nationally but
  only **34 in California**, named as opaque codes ("AA01", "YOS1"), with no Half
  Dome or Whitney entry at all.
* Permits are modelled as **facilities** with `FacilityTypeDescription == "Permit"`.
  There are only **7 in California**, but they are precisely the ones that gate
  California hiking: Half Dome, Inyo NF Wilderness (Whitney), Sequoia & Kings
  Canyon, Desolation, Hoover, Cedar Creek Falls.

Because a wilderness permit governs an entire forest from a single coordinate, the
join radius is deliberately wide and the result is advisory: "a permit may apply
here, check", never "you need this exact permit".
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path

import requests

from .spatial import PointGrid

RIDB_BASE = "https://ridb.recreation.gov/api/v1"
API_KEY = os.environ.get("RIDB_API_KEY", "")

_CACHE_DIR = Path(os.environ.get("RIDB_CACHE_DIR", Path(__file__).resolve().parent.parent / ".ridb_cache"))
PERMITS_PATH = Path(__file__).resolve().parent.parent / "data" / "permits.json"

CALIFORNIA_BBOX = (-124.5, 32.5, -114.1, 42.1)

# Wide on purpose. "Inyo National Forest - Wilderness Permits" is a single point
# that governs the whole eastern Sierra, so a tight radius would attach it to almost
# nothing. The consequence is that matches are advisory, and labelled as such.
MATCH_RADIUS_MI = 35.0

PAGE_SIZE = 50
MAX_PAGES = 60


def _plain(text: str | None, limit: int = 600) -> str | None:
    """RIDB descriptions are HTML fragments; render them as readable text."""
    if not text:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text[:limit] or None


class MissingKey(RuntimeError):
    """Raised when RIDB_API_KEY is not configured."""


def _get(path: str, params: dict) -> dict:
    if not API_KEY:
        raise MissingKey(
            "RIDB_API_KEY is not set. Get a free key at "
            "https://ridb.recreation.gov/profile and add it to .env"
        )
    response = requests.get(
        f"{RIDB_BASE}/{path}",
        params=params,
        headers={"apikey": API_KEY, "accept": "application/json"},
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def fetch_permit_facilities(use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """Facilities in California that issue permits, with their coordinates."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / "ca_permit_facilities.json"
    if use_cache and cache.exists():
        try:
            records = json.loads(cache.read_text())
            if verbose:
                print(f"  loaded {len(records)} cached permit facilities")
            return records
        except Exception:
            pass

    records: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = _get(
            "facilities",
            {
                "state": "CA",
                "limit": PAGE_SIZE,
                "offset": offset,
                "full": "true",
            },
        )
        batch = payload.get("RECDATA") or []
        for facility in batch:
            # The authoritative signal, confirmed against the live API. Matching on
            # the name instead pulls in campgrounds and misses the real permits.
            facility_type = facility.get("FacilityTypeDescription") or ""
            if facility_type not in ("Permit", "Ticket Facility", "Timed Entry"):
                continue

            # RIDB keeps deprecated entries live, marked in the name. "(OLD) Mt.
            # Whitney (OLD)" was outranking the current Inyo wilderness permit.
            name = facility.get("FacilityName") or ""
            if "(OLD)" in name.upper() or name.upper().startswith("OLD "):
                continue

            lat, lng = facility.get("FacilityLatitude"), facility.get("FacilityLongitude")
            if not lat or not lng:
                continue

            facility_id = str(facility.get("FacilityID"))

            # FacilityReservationURL is frequently empty — it is blank for Half
            # Dome — so fall back to the canonical Recreation.gov path, which is
            # what a user actually needs to reach the lottery.
            url = facility.get("FacilityReservationURL") or (
                f"https://www.recreation.gov/permits/{facility_id}"
                if facility_type == "Permit"
                else f"https://www.recreation.gov/camping/campgrounds/{facility_id}"
            )

            media = [
                {
                    "url": m.get("URL"),
                    "title": m.get("Title") or m.get("Description"),
                    "credits": m.get("Credits"),
                }
                for m in (facility.get("MEDIA") or [])
                if m.get("URL") and (m.get("MediaType") or "Image") == "Image"
            ][:6]

            recarea = (facility.get("RECAREA") or [{}])[0]
            org = (facility.get("ORGANIZATION") or [{}])[0]

            records.append(
                {
                    "id": facility_id,
                    "name": name,
                    "type": facility_type,
                    "reservable": bool(facility.get("Reservable")),
                    "url": url,
                    "phone": facility.get("FacilityPhone") or None,
                    "email": facility.get("FacilityEmail") or None,
                    "description": _plain(facility.get("FacilityDescription")),
                    "directions": _plain(facility.get("FacilityDirections"), 400),
                    "fee": _plain(facility.get("FacilityUseFeeDescription"), 300),
                    # "Half Dome,Half Dome Trail,The Cables,Yose,Yosemite" — a much
                    # better matching signal than coordinates alone.
                    "keywords": [
                        k.strip()
                        for k in (facility.get("Keywords") or "").split(",")
                        if k.strip()
                    ],
                    "media": media,
                    "rec_area": recarea.get("RecAreaName"),
                    "org": org.get("OrgAbbrevName"),
                    "ada": facility.get("FacilityAdaAccess") == "Y",
                    "lat": float(lat),
                    "lng": float(lng),
                    "is_permit": facility_type == "Permit",
                }
            )

        if verbose:
            print(f"  offset {offset}: +{len(batch)} (kept {len(records)})")
        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
        time.sleep(0.3)

    cache.write_text(json.dumps(records))
    PERMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERMITS_PATH.write_text(json.dumps(records))
    if verbose:
        print(f"  {len(records)} permit/reservable facilities in California")
    return records


def build_grid(records: list[dict]) -> PointGrid:
    grid = PointGrid(cell_deg=0.08)
    for record in records:
        grid.add(record["lat"], record["lng"], {k: v for k, v in record.items() if k not in ("lat", "lng")})
    return grid


def _coords(geometry: dict | None) -> list[list[float]]:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return coords
    if gtype == "MultiLineString":
        return [c for line in coords for c in line]
    return []


def enrich_trail(trail: dict, grid: PointGrid, geometry: dict | None, stride: int = 12) -> dict:
    """Attach nearby permit desks to one trail."""
    coords = _coords(geometry)
    if not coords:
        trail["permits"] = None
        return trail

    hits = grid.near_path(coords, MATCH_RADIUS_MI, stride=stride)
    permits = sorted(
        (h for h in hits.values() if h.get("is_permit")),
        key=lambda h: h["distance_mi"],
    )[:3]

    # [] means "we checked and found none nearby"; None means the lookup never ran.
    trail["permits"] = [
        {
            "name": p.get("name"),
            "url": p.get("url"),
            "distance_mi": p["distance_mi"],
            "reservable": p.get("reservable"),
            "description": p.get("description"),
            "fee": p.get("fee"),
            "phone": p.get("phone"),
            "rec_area": p.get("rec_area"),
            "org": p.get("org"),
            "media": p.get("media"),
            # A single coordinate stands in for a whole wilderness, so a proximity
            # match is a prompt to check, not a statement that the permit applies.
            # Keyword agreement with the trail name is much stronger evidence.
            "advisory": not _keyword_match(trail, p),
        }
        for p in permits
    ]
    return trail


def _keyword_match(trail: dict, permit: dict) -> bool:
    """True when the permit's own keywords name this trail.

    Half Dome Permits lists "Half Dome, Half Dome Trail, The Cables". When that
    lines up with the trail name the match is definite rather than advisory.
    """
    name = (trail.get("name") or "").lower()
    if not name:
        return False
    return any(k.lower() in name or name in k.lower() for k in (permit.get("keywords") or []) if len(k) > 4)


def enrich_all(trails: list[dict], geometries: dict, verbose: bool = True) -> list[dict]:
    records = fetch_permit_facilities(verbose=verbose)
    if not records:
        if verbose:
            print("  no permit facilities available — leaving permits uncomputed")
        return trails

    grid = build_grid(records)
    if verbose:
        print(f"  indexed {len(grid)} permit facilities")

    for index, trail in enumerate(trails, start=1):
        entry = geometries.get(trail["id"]) or {}
        enrich_trail(trail, grid, entry.get("geometry"))
        if verbose and index % 3000 == 0:
            print(f"  joined {index}/{len(trails)}")

    with_permits = sum(1 for t in trails if t.get("permits"))
    if verbose:
        print(f"  {with_permits} trails have a permit desk within {MATCH_RADIUS_MI} mi")
    return trails


if __name__ == "__main__":
    try:
        records = fetch_permit_facilities()
        print(f"\n{len(records)} permit facilities")
        for r in records[:10]:
            print(f"  {r['name'][:56]:58} reservable={r['reservable']}")
    except MissingKey as exc:
        print(exc)
