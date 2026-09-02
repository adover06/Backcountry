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

STATUS: written against the documented API but NOT verified against a live
response — every endpoint returns 401 without a key, so the response shapes below
are from the docs, not observed. Treat the field mapping as unconfirmed until it
has run once with a real key.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .spatial import PointGrid

RIDB_BASE = "https://ridb.recreation.gov/api/v1"
API_KEY = os.environ.get("RIDB_API_KEY", "")

_CACHE_DIR = Path(os.environ.get("RIDB_CACHE_DIR", Path(__file__).resolve().parent.parent / ".ridb_cache"))
PERMITS_PATH = Path(__file__).resolve().parent.parent / "data" / "permits.json"

CALIFORNIA_BBOX = (-124.5, 32.5, -114.1, 42.1)

# A permit desk further than this from the trail is not that trail's permit.
MATCH_RADIUS_MI = 3.0

PAGE_SIZE = 50
MAX_PAGES = 60


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
            lat, lng = facility.get("FacilityLatitude"), facility.get("FacilityLongitude")
            if not lat or not lng:
                continue

            reservable = bool(facility.get("Reservable"))
            type_name = (facility.get("FacilityTypeDescription") or "").lower()
            name = facility.get("FacilityName") or ""
            # Permit-issuing facilities are typed as permits, or are reservable
            # entries whose name says so.
            looks_like_permit = (
                "permit" in type_name
                or "permit" in name.lower()
                or "wilderness" in name.lower()
            )
            if not (looks_like_permit or reservable):
                continue

            records.append(
                {
                    "id": str(facility.get("FacilityID")),
                    "name": name,
                    "type": facility.get("FacilityTypeDescription"),
                    "reservable": reservable,
                    "url": facility.get("FacilityReservationURL") or facility.get("FacilityURL"),
                    "phone": facility.get("FacilityPhone"),
                    "lat": float(lat),
                    "lng": float(lng),
                    "is_permit": looks_like_permit,
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
        }
        for p in permits
    ]
    return trail


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
