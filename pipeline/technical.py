"""Technical-terrain and route-finding flags from OSM way tags.

`sac_scale` and `trail_visibility` are the only human-assessed difficulty signals in
any open source, but coverage is tiny: 3,359 and 5,953 California ways respectively,
about 0.3-0.5% of the 1.02M hikeable ways. That is far too sparse to be a filter — a
facet that silently drops 99.7% of the index is worse than no facet at all.

They are, however, excellent *badges*, because the coverage is not random. Mappers
tag `sac_scale` precisely where it matters: on alpine and scrambling terrain where a
trail stops being a walk. The ~1,385 ways at `mountain_hiking` or above are exactly
the ones a hiker needs warning about, and `trail_visibility` in {bad, horrible, no}
(1,356 ways) flags routes where the tread disappears.

So these attach as flags and never drive ranking or filtering.

Extraction (~15s):

    osmium tags-filter california-latest.osm.pbf w/sac_scale w/trail_visibility \\
        -o technical.osm.pbf
    osmium export technical.osm.pbf -f geojsonseq --add-unique-id=type_id \\
        -o technical.geojsonl
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .spatial import PointGrid

OSM_WORK_DIR = Path(os.environ.get("OSM_WORK_DIR", "/tmp/osmwork"))
TECHNICAL_JSONL = OSM_WORK_DIR / "technical.geojsonl"

# How close an OSM way must be to our trail to be describing the same ground.
MATCH_RADIUS_MI = 0.03  # ~50 m

# Ordered least to most severe; a trail takes the worst grade found along it.
SAC_ORDER = [
    "strolling",
    "hiking",
    "mountain_hiking",
    "demanding_mountain_hiking",
    "alpine_hiking",
    "demanding_alpine_hiking",
    "difficult_alpine_hiking",
]

SAC_LABEL = {
    "hiking": "Hiking trail",
    "mountain_hiking": "Mountain hiking — sure footing needed",
    "demanding_mountain_hiking": "Demanding — exposed sections, hands may be needed",
    "alpine_hiking": "Alpine — exposure, snowfields, simple scrambling",
    "demanding_alpine_hiking": "Demanding alpine — scrambling, glacier travel",
    "difficult_alpine_hiking": "Difficult alpine — climbing, high exposure",
}

# Only flag from here up; "hiking" and "strolling" describe an ordinary path.
SAC_FLAG_FROM = "mountain_hiking"

POOR_VISIBILITY = {"bad", "horrible", "no"}
VISIBILITY_LABEL = {
    "bad": "Faint tread — route-finding needed",
    "horrible": "Tread often absent — navigation required",
    "no": "No visible tread — full route-finding",
}


def load_tagged_ways(path: Path = TECHNICAL_JSONL, verbose: bool = True) -> list[dict]:
    """OSM ways carrying sac_scale or trail_visibility, as sampled points."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run the osmium commands in this module's docstring."
        )

    points = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (feature.get("geometry") or {}).get("type") != "LineString":
                continue

            props = feature.get("properties") or {}
            sac = props.get("sac_scale")
            visibility = props.get("trail_visibility")
            if sac not in SAC_ORDER and visibility not in POOR_VISIBILITY:
                continue

            coords = feature["geometry"]["coordinates"]
            # Sample the way rather than storing every vertex; a handful of points
            # is enough to decide whether our trail runs along it.
            step = max(1, len(coords) // 8)
            for i in range(0, len(coords), step):
                c = coords[i]
                points.append(
                    {
                        "id": f"{feature.get('id')}:{i}",
                        "lat": c[1],
                        "lng": c[0],
                        "sac_scale": sac if sac in SAC_ORDER else None,
                        "trail_visibility": visibility if visibility in POOR_VISIBILITY else None,
                    }
                )

    if verbose:
        print(f"  {len(points)} sampled points from tagged ways")
    return points


def build_grid(points: list[dict]) -> PointGrid:
    grid = PointGrid(cell_deg=0.01)
    for point in points:
        grid.add(point["lat"], point["lng"], {k: v for k, v in point.items() if k not in ("lat", "lng")})
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


def enrich_trail(trail: dict, grid: PointGrid, geometry: dict | None, stride: int = 6) -> dict:
    """Flag a trail with the worst technical grade found along it."""
    coords = _coords(geometry)
    if not coords:
        trail["technical"] = None
        return trail

    hits = grid.near_path(coords, MATCH_RADIUS_MI, stride=stride)

    worst_sac = None
    worst_visibility = None
    for hit in hits.values():
        sac = hit.get("sac_scale")
        if sac and (worst_sac is None or SAC_ORDER.index(sac) > SAC_ORDER.index(worst_sac)):
            worst_sac = sac
        visibility = hit.get("trail_visibility")
        if visibility and worst_visibility is None:
            worst_visibility = visibility

    flags = {}
    if worst_sac and SAC_ORDER.index(worst_sac) >= SAC_ORDER.index(SAC_FLAG_FROM):
        flags["sac_scale"] = worst_sac
        flags["sac_label"] = SAC_LABEL.get(worst_sac, worst_sac)
    if worst_visibility:
        flags["trail_visibility"] = worst_visibility
        flags["visibility_label"] = VISIBILITY_LABEL.get(worst_visibility)

    # None means "no OSM assessment exists here", which is the common case and is
    # not the same as "this trail is easy".
    trail["technical"] = flags or None
    return trail


def enrich_all(trails: list[dict], geometries: dict, verbose: bool = True) -> list[dict]:
    grid = build_grid(load_tagged_ways(verbose=verbose))
    if verbose:
        print(f"  indexed {len(grid)} points")

    for index, trail in enumerate(trails, start=1):
        entry = geometries.get(trail["id"]) or {}
        enrich_trail(trail, grid, entry.get("geometry"))
        if verbose and index % 3000 == 0:
            print(f"  joined {index}/{len(trails)}")

    flagged = sum(1 for t in trails if t.get("technical"))
    if verbose:
        print(f"  {flagged} trails carry a technical or visibility flag")
    return trails
