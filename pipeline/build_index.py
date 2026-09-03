"""Build the discovery index from raw sources.

Runs as a build step, not at request time. The output is two files:

  data/trails_index.json   compact records for search and faceting (no geometry)
  data/trails_geom.json    geometry keyed by trail id, loaded lazily for the map

Splitting them matters: the search index stays small enough to hold in memory and
scan quickly, while the 100+ MB of geometry is only touched when a map actually
needs it.

Stages are independent and resumable. Elevation and OSM enrichment are slow and
network-bound, so each writes progress back into the index and can be re-run to
fill only what is still missing. A stage that fails leaves the field as None —
"not computed" — and never as a fabricated zero.

Usage:
    python -m pipeline.build_index                 # all stages
    python -m pipeline.build_index --skip-osm      # normalize + elevation only
    python -m pipeline.build_index --limit 200     # quick smoke run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .elevation import elevation_for_geometry
from .enrich_osm import CALIFORNIA_BBOX, build_poi_grid, enrich_trail, fetch_pois
from .access import enrich_all as enrich_access
from .permits import MissingKey as PermitsMissingKey
from .wilderness import enrich_all as enrich_wilderness
from .markers import enrich_all as enrich_markers
from .osm_trails import assemble as assemble_osm_trails, dedupe_against
from .permits import enrich_all as enrich_permits
from .technical import enrich_all as enrich_technical
from .gnis import fetch_features as fetch_gnis_features
from .normalize import normalize_trails
from .nps import fetch_trails as fetch_nps_trails
from .nps import fetch_seki, normalize_nps, normalize_seki, poi_records as nps_poi_records
from .osm_routes import build_routes as build_osm_routes

_BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = _BASE_DIR / "data"
INDEX_PATH = DATA_DIR / "trails_index.json"
GEOM_PATH = DATA_DIR / "trails_geom.json"
POI_PATH = DATA_DIR / "pois.json"

# Fields kept in the searchable index (everything except geometry and profile).
_HEAVY_FIELDS = {"geometry"}


def _split_record(trail: dict) -> tuple[dict, dict]:
    """Separate a trail into its searchable record and its geometry."""
    record = {k: v for k, v in trail.items() if k not in _HEAVY_FIELDS}
    # The elevation profile is only needed on a detail view; keep the summary inline.
    elevation = record.get("elevation")
    profile = None
    if isinstance(elevation, dict) and "profile" in elevation:
        elevation = dict(elevation)
        profile = elevation.pop("profile")
        record["elevation"] = elevation
    geometry = {"geometry": trail.get("geometry"), "profile": profile}
    return record, geometry


def stage_normalize(source: str | None, limit: int | None, verbose: bool) -> list[dict]:
    if verbose:
        print("\n[normalize]   USFS trail segments")
    trails = normalize_trails(source)
    if limit:
        trails = trails[:limit]
    if verbose:
        total_miles = sum(t["length_miles"] for t in trails)
        print(f"      {len(trails)} hikeable trails, {total_miles:,.0f} mi")
    return trails


def stage_nps(trails: list[dict], verbose: bool) -> list[dict]:
    """Add National Park trails, which the USFS feed does not contain at all.

    Merged by id; NPS ids are namespaced (`nps:UNIT:slug`) so they cannot collide
    with USFS `trail_cn` values.
    """
    if verbose:
        print("\n[nps]         National Park Service trails")
    try:
        features = fetch_nps_trails(verbose=verbose)
        nps_trails = normalize_nps(features, verbose=verbose)
    except Exception as exc:
        # A failed fetch leaves the index USFS-only rather than half-populated.
        if verbose:
            print(f"      NPS fetch failed ({exc}) — continuing without park trails")
        return trails

    # Sequoia & Kings Canyon is absent from the national layer; fill it separately.
    try:
        nps_trails.extend(normalize_seki(fetch_seki(verbose=verbose), verbose=verbose))
    except Exception as exc:
        if verbose:
            print(f"      SEKI fallback failed ({exc}) — Sequoia/Kings Canyon will be missing")

    existing = {t["id"] for t in trails}
    added = [t for t in nps_trails if t["id"] not in existing]
    if verbose:
        print(f"      added {len(added)} park trails")
    return trails + added


def stage_osm_routes(trails: list[dict], verbose: bool) -> list[dict]:
    """Add long-distance OSM route relations (JMT, PCT sections, Tahoe Rim, ...).

    Deliberately narrow. OSM does not solve segmentation — it models Half Dome as
    2.00 mi exactly as the agency data does, because "a 14-mile hike" is a fact
    about how a trip is composed, not about trail geometry. What relations *do*
    uniquely provide are multi-day routes that no agency source assembles, so only
    those are ingested.

    Skipped silently when the osmium extract is absent; it is an optional stage.
    """
    if verbose:
        print("\n[osm-routes]  long-distance route relations")
    try:
        routes = build_osm_routes(verbose=verbose)
    except FileNotFoundError as exc:
        if verbose:
            print(f"      skipped: {exc}")
        return trails
    except Exception as exc:
        if verbose:
            print(f"      failed ({exc}) — continuing without OSM routes")
        return trails

    existing = {t["id"] for t in trails}
    added = [r for r in routes if r["id"] not in existing]
    if verbose:
        print(f"      added {len(added)} routes")
    return trails + added


def stage_elevation(trails: list[dict], workers: int, verbose: bool) -> list[dict]:
    """Sample the DEM for every trail that does not already have elevation."""
    pending = [t for t in trails if not t.get("elevation")]
    if verbose:
        print(f"\n[elevation]   DEM sampling {len(pending)} trails ({workers} workers)")
    if not pending:
        return trails

    started = time.time()
    done = 0
    failed = 0

    def work(trail: dict) -> tuple[dict, dict | None]:
        return trail, elevation_for_geometry(trail.get("geometry") or {})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, trail) for trail in pending]
        for future in as_completed(futures):
            trail, elevation = future.result()
            trail["elevation"] = elevation
            done += 1
            if elevation is None:
                failed += 1
            if verbose and done % 250 == 0:
                rate = done / max(1e-6, time.time() - started)
                remaining = (len(pending) - done) / max(1e-6, rate)
                print(f"      {done}/{len(pending)}  {rate:.1f}/s  ~{remaining/60:.1f} min left")

    if verbose:
        ok = len(pending) - failed
        print(f"      elevation computed for {ok}, unavailable for {failed}")
    return trails


def stage_osm(trails: list[dict], bbox, step: float, verbose: bool, use_osm: bool = False) -> list[dict]:
    """Attach named scenery features to each trail.

    GNIS is the primary source: it is public domain, needs no key, is not rate
    limited, and filters exactly to California. Overpass repeatedly timed out during
    development, so OSM is supplemental and off by default (`--with-osm`).

    Note the control flow: this fetches **only when `pois.json` is absent**. Once
    that file exists, the `--with-osm` branch is unreachable, which is why an OSM
    supplement never entered a resumed build. The supplement is now run explicitly
    and merged into `pois.json` (see `enrich_osm.merge_pois`), so the cached file is
    GNIS + OSM and this stage just reads it.
    """
    if verbose:
        print("\n[scenery]     GNIS + OSM named features")

    pois: list[dict] = []
    if POI_PATH.exists():
        try:
            pois = json.loads(POI_PATH.read_text())
            if verbose:
                print(f"      loaded {len(pois)} cached POIs")
        except Exception:
            pois = []

    if not pois:
        try:
            pois = fetch_gnis_features(verbose=verbose)
        except Exception as exc:
            if verbose:
                print(f"      GNIS fetch failed ({exc})")
            pois = []

        if use_osm:
            try:
                osm_pois = fetch_pois(bbox=bbox, step_deg=step, verbose=verbose)
                # GNIS wins on overlap; OSM adds viewpoints GNIS does not record.
                pois.extend(osm_pois)
            except Exception as exc:
                if verbose:
                    print(f"      OSM supplement failed ({exc}) — continuing with GNIS only")

        if pois:
            POI_PATH.write_text(json.dumps(pois))
            if verbose:
                print(f"      saved {len(pois)} POIs")

    if not pois:
        # Enrichment could not run. Leave `features` as None so the UI can say
        # "scenery data unavailable" instead of showing an empty filter result.
        if verbose:
            print("      no POIs available — leaving scenery tags uncomputed")
        return trails

    grid = build_poi_grid(pois)
    if verbose:
        print(f"      indexed {len(grid)} POIs; joining to trails")

    for index, trail in enumerate(trails, start=1):
        enrich_trail(trail, grid)
        if verbose and index % 2000 == 0:
            print(f"      joined {index}/{len(trails)}")

    tagged = sum(1 for t in trails if t.get("features"))
    if verbose:
        print(f"      {tagged} trails have at least one scenery feature")
    return trails


def stage_access(trails: list[dict], geometries: dict, verbose: bool) -> list[dict]:
    """Attach trailheads, parking and water — where a hike actually starts."""
    if verbose:
        print("\n[access]      trailheads, parking, water, camping (NPS + USFS + OSM)")
    try:
        return enrich_access(trails, geometries, verbose=verbose)
    except Exception as exc:
        if verbose:
            print(f"      access enrichment failed ({exc}) — continuing")
        return trails


def stage_technical(trails: list[dict], geometries: dict, verbose: bool) -> list[dict]:
    """Flag alpine terrain and poor tread from OSM sac_scale / trail_visibility.

    Coverage is ~0.3-0.5% of hikeable ways, so these are badges, never facets. But
    the coverage is not random: mappers tag it where a trail stops being a walk.
    """
    if verbose:
        print("\n[technical]   OSM sac_scale / trail_visibility flags")
    try:
        return enrich_technical(trails, geometries, verbose=verbose)
    except FileNotFoundError as exc:
        if verbose:
            print(f"      skipped: {exc}")
        return trails
    except Exception as exc:
        if verbose:
            print(f"      failed ({exc}) — continuing")
        return trails


def stage_permits(trails: list[dict], geometries: dict, verbose: bool) -> list[dict]:
    """Attach nearby permit desks from Recreation.gov.

    Optional: skipped cleanly without RIDB_API_KEY, since it is the only stage that
    needs a credential.
    """
    if verbose:
        print("\n[permits]     Recreation.gov (RIDB)")
    try:
        return enrich_permits(trails, geometries, verbose=verbose)
    except PermitsMissingKey as exc:
        if verbose:
            print(f"      skipped: {exc}")
        return trails
    except Exception as exc:
        if verbose:
            print(f"      failed ({exc}) — continuing")
        return trails


def stage_wilderness(trails: list[dict], geometries: dict, verbose: bool) -> list[dict]:
    """Mark trails inside designated wilderness.

    Land status, not proximity: the boundary polygon answers it outright, where
    `stage_permits` can only say a permit desk is nearby. Wilderness implies quota
    entry, group-size caps and a mechanised-transport ban, none of which the trail
    geometry knows.
    """
    if verbose:
        print("\n[wilderness]  USFS designated boundaries")
    try:
        return enrich_wilderness(trails, geometries, verbose=verbose)
    except Exception as exc:
        if verbose:
            print(f"      failed ({exc}) — continuing")
        return trails


# Where the OSM way sweep leaves its output. Written by `osm_trails_fetch`, read
# here; the fetch is far too slow to hide inside a build.
OSM_WAYS_PATH = DATA_DIR / "osm_ways.json"


def stage_osm_trails(trails: list[dict], verbose: bool) -> list[dict]:
    """Add trails assembled from named OSM ways.

    Closes the structural gap in the two federal sources: USFS and NPS between them
    cover national forest and park land, and most Californians walk on regional,
    county and state park land that neither publishes. Measured on the South Bay,
    the index held 15 trails where OSM has 4,415 named ways.

    Runs before elevation so the new trails get a DEM pass like any other, and
    before access/markers/wilderness so they are enriched identically. Skipped
    cleanly when the sweep has not been run.
    """
    if not OSM_WAYS_PATH.exists():
        if verbose:
            print("\n[osm-trails]  skipped — run `python -m pipeline.osm_trails`")
        return trails

    if verbose:
        print("\n[osm-trails]  named OSM ways assembled into trails")
    try:
        ways = json.loads(OSM_WAYS_PATH.read_text())
        assembled = assemble_osm_trails(ways, verbose=verbose)
        fresh = dedupe_against(assembled, trails, verbose=verbose)
        # Drop any that a previous run already added, so the stage is idempotent.
        known = {t["id"] for t in trails}
        fresh = [t for t in fresh if t["id"] not in known]
        if verbose:
            print(f"  {len(fresh)} new trails added ({len(trails)} -> {len(trails) + len(fresh)})")
        return trails + fresh
    except Exception as exc:
        if verbose:
            print(f"      failed ({exc}) — continuing")
        return trails


def stage_markers(trails: list[dict], geometries: dict, verbose: bool) -> list[dict]:
    """Pick the point that represents each trail on the map.

    Must run after `stage_access`, because a joined trailhead is the preferred
    marker and only exists once access enrichment has run.
    """
    if verbose:
        print("\n[markers]     map point per trail (trailhead, else on-line midpoint)")
    try:
        return enrich_markers(trails, geometries, verbose=verbose)
    except Exception as exc:
        if verbose:
            print(f"      failed ({exc}) — continuing")
        return trails


def write_outputs(trails: list[dict], verbose: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    records = []
    geometries = {}
    for trail in trails:
        record, geometry = _split_record(trail)
        records.append(record)
        geometries[trail["id"]] = geometry

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(records),
        "sources": {
            "trails": (
                "USFS National Forest + NPS Public Trails + SEKI + "
                "OpenStreetMap (California)"
            ),
            "elevation": "AWS Terrarium DEM (~30 m)",
            "scenery": "USGS GNIS named features (public domain)",
            "access": (
                "NPS Public POIs + USFS INFRA recreation sites "
                "+ OpenStreetMap (ODbL)"
            ),
            "wilderness": "USFS EDW designated wilderness boundaries",
            "permits": "Recreation.gov (RIDB)",
        },
        "trails": records,
    }
    INDEX_PATH.write_text(json.dumps(payload))
    GEOM_PATH.write_text(json.dumps(geometries))

    if verbose:
        print(f"\nwrote {INDEX_PATH.name} ({INDEX_PATH.stat().st_size/1e6:.1f} MB)")
        print(f"wrote {GEOM_PATH.name} ({GEOM_PATH.stat().st_size/1e6:.1f} MB)")


def load_existing() -> list[dict] | None:
    """Reload a previous build so slow stages can resume instead of restarting."""
    if not (INDEX_PATH.exists() and GEOM_PATH.exists()):
        return None
    try:
        index = json.loads(INDEX_PATH.read_text())
        geometries = json.loads(GEOM_PATH.read_text())
    except Exception:
        return None

    trails = []
    for record in index.get("trails", []):
        entry = geometries.get(record["id"]) or {}
        trail = dict(record)
        trail["geometry"] = entry.get("geometry")
        if trail.get("elevation") and entry.get("profile"):
            trail["elevation"] = {**trail["elevation"], "profile": entry["profile"]}
        trails.append(trail)
    return trails


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the trail discovery index")
    parser.add_argument("--source", default=None, help="path to the USFS GeoJSON")
    parser.add_argument("--limit", type=int, default=None, help="only process N trails")
    parser.add_argument("--workers", type=int, default=12, help="DEM sampling threads")
    parser.add_argument("--osm-step", type=float, default=1.0, help="OSM tile size in degrees")
    parser.add_argument("--skip-elevation", action="store_true")
    parser.add_argument("--skip-nps", action="store_true")
    parser.add_argument("--skip-osm-routes", action="store_true")
    parser.add_argument("--skip-access", action="store_true")
    parser.add_argument("--skip-technical", action="store_true")
    parser.add_argument("--skip-permits", action="store_true")
    parser.add_argument("--skip-wilderness", action="store_true")
    parser.add_argument("--skip-markers", action="store_true")
    parser.add_argument("--skip-osm-trails", action="store_true")
    parser.add_argument("--skip-osm", action="store_true")
    parser.add_argument("--with-osm", action="store_true", help="also query Overpass (slow, flaky)")
    parser.add_argument("--resume", action="store_true", help="reuse the previous build")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    verbose = not args.quiet

    trails = load_existing() if args.resume else None
    if trails:
        if verbose:
            print(f"resuming from previous build ({len(trails)} trails)")
        if args.limit:
            trails = trails[: args.limit]
    else:
        trails = stage_normalize(args.source, args.limit, verbose)

    if not args.skip_nps:
        trails = stage_nps(trails, verbose)

    if not args.skip_osm_routes:
        trails = stage_osm_routes(trails, verbose)

    if not args.skip_osm_trails:
        trails = stage_osm_trails(trails, verbose)

    if not args.skip_elevation:
        trails = stage_elevation(trails, args.workers, verbose)
    if not args.skip_osm:
        trails = stage_osm(trails, CALIFORNIA_BBOX, args.osm_step, verbose, use_osm=args.with_osm)

    if not args.skip_access:
        geometries = {t["id"]: {"geometry": t.get("geometry")} for t in trails}
        trails = stage_access(trails, geometries, verbose)

    if not args.skip_technical:
        geometries = {t["id"]: {"geometry": t.get("geometry")} for t in trails}
        trails = stage_technical(trails, geometries, verbose)

    if not args.skip_permits:
        geometries = {t["id"]: {"geometry": t.get("geometry")} for t in trails}
        trails = stage_permits(trails, geometries, verbose)

    if not args.skip_wilderness:
        geometries = {t["id"]: {"geometry": t.get("geometry")} for t in trails}
        trails = stage_wilderness(trails, geometries, verbose)

    if not args.skip_markers:
        geometries = {t["id"]: {"geometry": t.get("geometry")} for t in trails}
        trails = stage_markers(trails, geometries, verbose)

    write_outputs(trails, verbose)

    if verbose:
        with_elev = sum(1 for t in trails if t.get("elevation"))
        with_feat = sum(1 for t in trails if t.get("features"))
        from collections import Counter

        sources = Counter(t.get("source") or "USFS National Forest System" for t in trails)
        print("\nsummary")
        print(f"  trails            {len(trails)}")
        for source, count in sources.most_common():
            print(f"    {source:<32} {count}")
        with_th = sum(1 for t in trails if (t.get("access") or {}).get("trailhead"))
        with_camp = sum(1 for t in trails if (t.get("access") or {}).get("campground"))
        with_wild = sum(1 for t in trails if (t.get("wilderness") or {}).get("name"))
        print(f"  with elevation    {with_elev}")
        print(f"  with scenery tags {with_feat}")
        print(f"  with trailhead    {with_th}")
        print(f"  with campground   {with_camp}")
        print(f"  in wilderness     {with_wild}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
