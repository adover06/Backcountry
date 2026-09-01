"""Long-distance hiking routes from OpenStreetMap `route=hiking` relations.

**This does not fix trail segmentation, and it is important to be clear why.**

Research against the California Geofabrik extract found that OSM models Half Dome
Trail as 2.00 mi — identical to the agency value. "Half Dome is a 14-mile hike" is a
statement about *hike composition* (trailhead → summit → back), not about trail
geometry, and no OSM object encodes it. Only 9.2% of our trails have a matching
relation, and just 4.2% of the sub-1-mile ones. Chaining ways by shared name is
worse still: it produces 76.3% sub-mile fragments against our current 54%.

What relations *are* good at is the genuinely long-distance routes that no agency
dataset assembles: the JMT (208.7 mi across 94 members), the Tahoe Rim Trail, the
Lost Coast Trail, Rae Lakes Loop, Skyline-to-the-Sea. Those are exactly the trips
this app should surface and cannot currently represent. That is the narrow, real win
this module takes.

Prerequisites (about 35s total, no GEOS/GDAL source build):

    brew install osmium-tool
    curl -O https://download.geofabrik.de/north-america/us/california-latest.osm.pbf
    osmium tags-filter california-latest.osm.pbf r/route=hiking -o hiking_full.osm.pbf
    osmium export hiking_full.osm.pbf -f geojsonseq --add-unique-id=type_id \\
        -o hiking_geom.geojsonl
    osmium tags-filter -R california-latest.osm.pbf r/route=hiking -o rels_only.osm.pbf
    osmium cat rels_only.osm.pbf -f opl -o rels.opl

Data is ODbL. This project is personal-use and undistributed, so share-alike does
not trigger; attribute OpenStreetMap contributors if that ever changes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .normalize import _slugify, chain_lines, geometry_length_miles

OSM_WORK_DIR = Path(os.environ.get("OSM_WORK_DIR", "/tmp/osmwork"))
RELATIONS_OPL = OSM_WORK_DIR / "rels.opl"
GEOMETRY_JSONL = OSM_WORK_DIR / "hiking_geom.geojsonl"

# Below this, a relation is a local path already covered by agency data. Above it,
# it is a route no other source in the pipeline assembles.
MIN_ROUTE_MILES = float(os.environ.get("OSM_MIN_ROUTE_MILES", "5.0"))

# 95.8% of members carry an empty role, and `forward`/`backward` appear once each
# statewide. Only the main line is followed: including `alternative` and `approach`
# splices spur and variant trails into the route, inflating its length and creating
# branches that are not part of the through-route.
_USABLE_ROLES = {"", "main"}
_EXCLUDED_ROLES = {"alternative", "approach", "excursion", "maybe"}


def _decode_opl(text: str) -> str:
    """OPL escapes non-ASCII and separators as %<hex>%."""
    return re.sub(r"%([0-9A-Fa-f]{2,4})%", lambda m: chr(int(m.group(1), 16)), text)


def parse_relations(path: Path = RELATIONS_OPL) -> list[dict]:
    """Parse the OPL relation dump into {id, tags, member_ways}."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run the osmium commands in this module's docstring."
        )

    relations = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("r"):
            continue

        tags: dict[str, str] = {}
        members: list[str] = []

        # Fields are space-separated and prefixed by a single letter.
        for field in line.split(" "):
            if field.startswith("T") and len(field) > 1:
                for pair in field[1:].split(","):
                    if "=" in pair:
                        key, _, value = pair.partition("=")
                        tags[_decode_opl(key)] = _decode_opl(value)
            elif field.startswith("M") and len(field) > 1:
                for member in field[1:].split(","):
                    if not member.startswith("w"):
                        continue  # node and sub-relation members carry no way geometry
                    ref, _, role = member.partition("@")
                    role = _decode_opl(role).strip().lower()
                    if role in _EXCLUDED_ROLES:
                        continue
                    if role in _USABLE_ROLES:
                        members.append(ref)

        if members:
            relations.append(
                {"id": line.split(" ", 1)[0], "tags": tags, "member_ways": members}
            )

    return relations


def load_way_geometry(path: Path = GEOMETRY_JSONL) -> dict[str, list[list[float]]]:
    """Map OSM way id ('w123') to its coordinate list."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run the osmium commands in this module's docstring."
        )

    geometry: dict[str, list[list[float]]] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            # geojsonseq records may be prefixed with the RS control character.
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (feature.get("geometry") or {}).get("type") != "LineString":
                continue
            way_id = feature.get("id")
            if isinstance(way_id, str) and way_id.startswith("w"):
                geometry[way_id] = feature["geometry"]["coordinates"]
    return geometry


def build_routes(
    min_miles: float = MIN_ROUTE_MILES, verbose: bool = True
) -> list[dict]:
    """Assemble relations into trail records, keeping the long-distance ones."""
    relations = parse_relations()
    ways = load_way_geometry()
    if verbose:
        print(f"  {len(relations)} relations, {len(ways)} member way geometries")

    routes = []
    skipped_short = 0
    skipped_empty = 0

    for relation in relations:
        parts = [ways[w] for w in relation["member_ways"] if w in ways]
        if not parts:
            skipped_empty += 1
            continue

        chains = chain_lines(parts)
        length_miles = round(geometry_length_miles(chains), 2)
        if length_miles < min_miles:
            skipped_short += 1
            continue

        tags = relation["tags"]
        name = (tags.get("name") or tags.get("ref") or "").strip()
        if not name:
            continue

        lngs = [c[0] for line in chains for c in line]
        lats = [c[1] for line in chains for c in line]
        bbox = [min(lngs), min(lats), max(lngs), max(lats)]

        routes.append(
            {
                "id": f"osm:{relation['id']}",
                "name": name,
                "named": True,
                "slug": _slugify(name),
                "trail_no": tags.get("ref"),
                "admin_org": tags.get("operator"),
                # `from`/`to` are on 28% of long routes and are exactly the label a
                # hiker wants ("Happy Isles → Whitney Portal"). `distance` is
                # deliberately ignored: its values mix km, mi and bare numbers with
                # no convention, so length always comes from the geometry.
                "endpoints": (
                    {"from": tags["from"], "to": tags["to"]}
                    if tags.get("from") and tags.get("to")
                    else None
                ),
                "network": tags.get("network"),
                "wikidata": tags.get("wikidata"),
                "wikipedia": tags.get("wikipedia"),
                "website": tags.get("website"),
                "trail_type": "TERRA",
                "source": "OpenStreetMap route relation",
                "length_miles": length_miles,
                "geometry_length_miles": length_miles,
                "trail_class": None,
                "trail_class_label": None,
                "grade": None,
                "surface": None,
                "mgmt_area": tags.get("operator") or _network_label(tags.get("network")),
                "accessibility": None,
                "activities": {"hiking": {"allowed": True, "restricted": None, "season": None}},
                "season": None,
                "route_type": (
                    "loop"
                    if tags.get("roundtrip") == "yes" or _looks_like_loop(chains)
                    else "out-and-back"
                ),
                "bbox": bbox,
                "center": [round((bbox[0] + bbox[2]) / 2, 6), round((bbox[1] + bbox[3]) / 2, 6)],
                "segment_count": len(relation["member_ways"]),
                "part_count": len(chains),
                "geometry": {"type": "MultiLineString", "coordinates": chains},
                "elevation": None,
                "features": None,
            }
        )

    routes.sort(key=lambda r: -r["length_miles"])
    if verbose:
        print(
            f"  {len(routes)} long-distance routes >= {min_miles} mi "
            f"({skipped_short} shorter, {skipped_empty} with no member geometry)"
        )
    return routes


def _network_label(network: str | None) -> str | None:
    return {
        "iwn": "International walking network",
        "nwn": "National walking network",
        "rwn": "Regional walking network",
        "lwn": "Local walking network",
    }.get(network or "")


def _looks_like_loop(chains: list[list[list[float]]]) -> bool:
    if len(chains) != 1 or len(chains[0]) < 3:
        return False
    start, end = chains[0][0], chains[0][-1]
    return abs(start[0] - end[0]) < 1e-3 and abs(start[1] - end[1]) < 1e-3


if __name__ == "__main__":
    result = build_routes()
    print(f"\n{len(result)} routes")
    for route in result[:20]:
        print(f"  {route['length_miles']:>8.1f} mi  {route['name'][:52]:54} {route['part_count']} part(s)")
