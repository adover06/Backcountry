"""A routing graph over the trail network, for composing hikes.

Why this exists: no open dataset models a *hike*. Every source — USFS, NPS, OSM,
Recreation.gov — publishes trail **segments**, because segments are what agencies
administer and maintain budgets against. "Half Dome is a 14-mile hike from Happy
Isles" is a visitor-facing composition that no land manager has an operational
reason to record, which is why OSM also models Half Dome Trail as 2.00 mi. The
absence is structural, so the composition has to be computed.

The approach:

1. Snap every trail vertex to a fixed grid. Any snapped point shared by two or more
   distinct trails is a junction.
2. Split each trail at its junctions, giving edges that meet only at nodes.
3. Weight each edge by length and by elevation gain in each direction — gain is
   directional, and a route's climb depends on which way you walk it.

From that, a hike is a path: trailhead -> destination -> back.

STATUS: FOUNDATION ONLY — NOT WIRED INTO THE PRODUCT.

The graph builds correctly and quickly (217k nodes / 459k edges in ~14s) and paths
resolve, but the numbers are not yet trustworthy enough to show anyone. Measured
against known routes:

    snap    Whitney (truth 22 mi / 6,100 ft)   Half Dome (truth ~15 mi / 4,800 ft)
    20 m    8.6 mi  /  6,106 ft                11.2 mi / 5,862 ft
    10 m    14.3 mi /  8,758 ft                no path
     5 m    18.1 mi / 12,530 ft                no path
     2 m    no path                            no path

Two unsolved problems, both visible above:

1. **Snap distance trades fusion against disconnection.** Loose snapping merges
   adjacent switchback legs into one node and the router cuts straight up the
   mountain — Whitney's 99 switchbacks are the pathological case. Tight snapping
   stops the fusion but leaves the network disconnected, because agency datasets
   do not share exact coordinates where trails meet. The fix is not a better single
   value: it is tight snapping for node identity *plus* a separate pass that
   explicitly stitches endpoints within a tolerance, so junctions connect without
   mid-trail vertices fusing.

2. **Gain accumulates noise.** Per-edge climb is read from the trail's 250-point
   downsampled profile indexed by position, so as edges multiply the sum
   over-counts — the same failure the DEM sampler had before it got a hysteresis
   threshold. Elevation should be sampled per edge from the DEM directly, or
   accumulated with a threshold, not summed from profile deltas.

Until both are fixed this must not drive anything user-facing. Reporting "Half Dome
is 11.2 miles" with confidence would be exactly the class of error the rest of this
codebase exists to prevent.

Deliberately dependency-free (no networkx/shapely) to keep the pipeline installable;
the graph is small enough that plain dicts and a heap are fast.
"""

from __future__ import annotations

import heapq
import json
import math
from collections import defaultdict
from pathlib import Path

from .normalize import haversine_miles

_BASE_DIR = Path(__file__).resolve().parent.parent

# Vertices within roughly this distance are treated as the same node. 10 m is the
# least-bad single value found (see the table above), not a solution — the real fix
# is tight snapping plus an explicit endpoint-stitching pass.
SNAP_METERS = 10.0

# Degrees per metre at California latitudes (~37N). Longitude is compressed by
# cos(lat); using a single factor keeps snapping cheap and is accurate enough here.
_DEG_PER_M_LAT = 1.0 / 111_320.0
_DEG_PER_M_LNG = 1.0 / (111_320.0 * math.cos(math.radians(37.5)))


def _snap(lng: float, lat: float, metres: float | None = None) -> tuple[int, int]:
    """Quantise a coordinate to a grid cell id.

    Reads SNAP_METERS at call time rather than binding it as a default argument:
    a default is evaluated once at definition, so tuning the module global had no
    effect and every snap distance produced an identical graph.
    """
    metres = SNAP_METERS if metres is None else metres
    return (
        int(round(lat / (metres * _DEG_PER_M_LAT))),
        int(round(lng / (metres * _DEG_PER_M_LNG))),
    )


def _lines(geometry: dict | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [coords] if len(coords) >= 2 else []
    if gtype == "MultiLineString":
        return [c for c in coords if isinstance(c, list) and len(c) >= 2]
    return []


class TrailGraph:
    """Undirected graph of trail segments, with directional elevation cost."""

    def __init__(self) -> None:
        # node -> list of (neighbour, miles, gain_ft, trail_id)
        self.adjacency: dict[tuple[int, int], list[tuple]] = defaultdict(list)
        self.node_coord: dict[tuple[int, int], tuple[float, float]] = {}
        self.trail_names: dict[str, str] = {}

    # ── construction ─────────────────────────────────────────────────────────

    def build(self, trails: list[dict], geometries: dict, verbose: bool = True) -> "TrailGraph":
        if verbose:
            print(f"  indexing {len(trails)} trails")

        # Pass 1: how many distinct trails touch each snapped point?
        touches: dict[tuple[int, int], set[str]] = defaultdict(set)
        for trail in trails:
            entry = geometries.get(trail["id"]) or {}
            for line in _lines(entry.get("geometry")):
                for lng, lat in ((c[0], c[1]) for c in line):
                    touches[_snap(lng, lat)].add(trail["id"])

        junctions = {node for node, ids in touches.items() if len(ids) > 1}
        if verbose:
            print(f"  {len(touches)} snapped points, {len(junctions)} junctions")

        # Pass 2: split each line at junctions and at its own endpoints.
        for trail in trails:
            entry = geometries.get(trail["id"]) or {}
            profile = entry.get("profile") or []
            self.trail_names[trail["id"]] = trail.get("name") or trail["id"]

            for line in _lines(entry.get("geometry")):
                self._add_line(trail["id"], line, junctions, profile)

        if verbose:
            edges = sum(len(v) for v in self.adjacency.values()) // 2
            print(f"  {len(self.adjacency)} nodes, {edges} edges")
        return self

    def _add_line(
        self,
        trail_id: str,
        line: list[list[float]],
        junctions: set,
        profile: list[dict],
    ) -> None:
        """Split one line into edges that run junction-to-junction."""
        # Elevation is sampled along the whole trail, so interpolate by position.
        def elevation_at(fraction: float) -> float | None:
            if not profile:
                return None
            index = min(len(profile) - 1, max(0, int(fraction * (len(profile) - 1))))
            return profile[index].get("ft")

        start = 0
        running = 0.0
        # Distance travelled at the previous split. Carrying this forward keeps the
        # walk linear; recomputing the prefix sum per segment made it quadratic and
        # the full-state build never finished.
        running_at_split = 0.0

        for i in range(1, len(line)):
            running += haversine_miles(line[i - 1][1], line[i - 1][0], line[i][1], line[i][0])
            node = _snap(line[i][0], line[i][1])
            is_end = i == len(line) - 1
            if node not in junctions and not is_end:
                continue

            a = _snap(line[start][0], line[start][1])
            b = node
            if a == b:
                start = i
                running_at_split = running
                continue

            seg_miles = running - running_at_split
            if seg_miles <= 0:
                start = i
                running_at_split = running
                continue

            self.node_coord.setdefault(a, (line[start][0], line[start][1]))
            self.node_coord.setdefault(b, (line[i][0], line[i][1]))

            ele_a = elevation_at((start / max(1, len(line) - 1)))
            ele_b = elevation_at((i / max(1, len(line) - 1)))
            climb = max(0.0, (ele_b - ele_a)) if (ele_a is not None and ele_b is not None) else 0.0
            drop = max(0.0, (ele_a - ele_b)) if (ele_a is not None and ele_b is not None) else 0.0

            self.adjacency[a].append((b, seg_miles, climb, trail_id))
            self.adjacency[b].append((a, seg_miles, drop, trail_id))
            start = i
            running_at_split = running

    # ── queries ──────────────────────────────────────────────────────────────

    def nearest_node(self, lng: float, lat: float, max_miles: float = 0.25):
        """Closest graph node to a coordinate, or None."""
        node = _snap(lng, lat)
        if node in self.adjacency:
            return node
        best = None
        best_distance = max_miles
        # Search outward a few cells rather than scanning every node.
        for dlat in range(-3, 4):
            for dlng in range(-3, 4):
                candidate = (node[0] + dlat, node[1] + dlng)
                coord = self.node_coord.get(candidate)
                if not coord:
                    continue
                distance = haversine_miles(lat, lng, coord[1], coord[0])
                if distance < best_distance:
                    best, best_distance = candidate, distance
        return best

    def shortest_path(self, source, target, max_miles: float = 40.0) -> dict | None:
        """Dijkstra on distance. Returns miles, gain, and the trails used."""
        if source not in self.adjacency or target not in self.adjacency:
            return None

        distances = {source: 0.0}
        gains = {source: 0.0}
        previous: dict = {}
        queue = [(0.0, source)]
        seen = set()

        while queue:
            distance, node = heapq.heappop(queue)
            if node in seen:
                continue
            seen.add(node)
            if node == target:
                break
            if distance > max_miles:
                break

            for neighbour, miles, climb, trail_id in self.adjacency[node]:
                if neighbour in seen:
                    continue
                candidate = distance + miles
                if candidate < distances.get(neighbour, float("inf")):
                    distances[neighbour] = candidate
                    gains[neighbour] = gains[node] + climb
                    previous[neighbour] = (node, trail_id)
                    heapq.heappush(queue, (candidate, neighbour))

        if target not in distances:
            return None

        trails: list[str] = []
        node = target
        while node in previous:
            node, trail_id = previous[node]
            if not trails or trails[-1] != trail_id:
                trails.append(trail_id)
        trails.reverse()

        return {
            "miles_one_way": round(distances[target], 2),
            "gain_ft_one_way": int(round(gains.get(target, 0.0))),
            "trail_ids": trails,
            "trail_names": [self.trail_names.get(t, t) for t in trails],
        }

    def compose_hike(self, source, target, out_and_back: bool = True) -> dict | None:
        """A complete hike: out to the destination and back."""
        leg = self.shortest_path(source, target)
        if not leg:
            return None
        return {
            "miles": round(leg["miles_one_way"] * (2 if out_and_back else 1), 2),
            # Returning by the same trail climbs whatever it descended on the way out.
            "gain_ft": leg["gain_ft_one_way"] * (2 if out_and_back else 1),
            "one_way_miles": leg["miles_one_way"],
            "trail_names": leg["trail_names"],
            "segments_used": len(leg["trail_ids"]),
            "route_type": "out-and-back" if out_and_back else "point-to-point",
        }


def load_graph(verbose: bool = True) -> TrailGraph:
    index = json.loads((_BASE_DIR / "data" / "trails_index.json").read_text())
    geometries = json.loads((_BASE_DIR / "data" / "trails_geom.json").read_text())
    return TrailGraph().build(index["trails"], geometries, verbose=verbose)


if __name__ == "__main__":
    graph = load_graph()
    print(f"\nnodes: {len(graph.adjacency):,}")
