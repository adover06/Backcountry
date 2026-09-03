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

STATUS: WORKING. Validated against known routes:

    route                       computed              published
    Half Dome (Happy Isles)     13.9 mi / 5,245 ft    ~15 mi / 4,800 ft
    Nevada Fall (Happy Isles)    5.5 mi / 2,176 ft    ~5.4 mi / 1,900 ft
    Ryan Mountain                2.8 mi / 1,054 ft    ~3 mi / 1,050 ft
    Mt Whitney (Portal)         18.5 mi / 7,447 ft    22 mi / 6,100 ft

Half Dome indexes as a 2.00 mi *segment* and composes to a 13.9 mi *hike*, which is
the whole point. Whitney is the weakest case: our Mount Whitney trail geometry is
itself short (8.7 mi against a true 11 one-way), so the shortfall is upstream data,
not routing.

Four things had to be right, each of which was wrong first:

1. Node identity must be tight (4 m). Loose snapping fused adjacent switchback legs
   and let the router cut straight up Whitney — 8.6 mi against a true 22.
2. Tight snapping alone disconnects the network, because agencies do not share
   coordinates where trails meet. A separate pass stitches *endpoints* within 35 m;
   stitching mid-trail vertices would reintroduce the fusion.
3. Gain is computed once along the finished path with the DEM hysteresis threshold,
   never summed per edge. Nodes are metres apart, so per-edge accumulation counted
   DEM noise as climb and reported Whitney at 14,232 ft.
4. `nearest_node` must size its cell search from max_miles. A hardcoded +/-3 cells
   spanned 60 m at 20 m snapping but only 12 m at 4 m, so trailheads silently
   resolved to no node and routes returned "no path".

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

# Node identity is deliberately tight: only vertices that are essentially the same
# point become the same node. This is what stops adjacent switchback legs fusing
# and letting the router cut straight up a mountain.
SNAP_METERS = 4.0

# Trails from different agencies almost never share exact coordinates where they
# meet, so tight snapping alone leaves the network disconnected. A separate pass
# stitches *endpoints* that are close together — endpoints only, because joining
# mid-trail vertices is precisely the fusion the tight snap exists to prevent.
STITCH_METERS = 35.0

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
        # (a, b) -> the trail's real coordinates between those nodes. Without this a
        # route can only be drawn node-to-node, which renders a trail with no
        # junctions as a single straight line across the mountain.
        self.edge_shape: dict[tuple, list[list[float]]] = {}
        self.node_coord: dict[tuple[int, int], tuple[float, float]] = {}
        self.node_ele: dict[tuple[int, int], float] = {}
        self.trail_names: dict[str, str] = {}
        # Nodes that begin or end a trail line — the only ones eligible for stitching.
        self._endpoints: set = set()

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
            print(f"  {len(self.adjacency)} nodes, {edges} edges (pre-stitch)")

        self._stitch_endpoints(verbose=verbose)
        self._sample_node_elevations(verbose=verbose)
        self._apply_elevation_costs()

        if verbose:
            edges = sum(len(v) for v in self.adjacency.values()) // 2
            print(f"  final: {len(self.adjacency)} nodes, {edges} edges")
        return self

    def _stitch_endpoints(self, verbose: bool = True) -> None:
        """Join trail endpoints that are close but not identical.

        Tight snapping keeps switchbacks distinct but leaves the network in pieces,
        because agencies do not share coordinates where their trails meet. Only
        *endpoints* are eligible: stitching mid-trail vertices would reintroduce the
        fusion that tight snapping exists to prevent.
        """
        cell = STITCH_METERS
        buckets: dict[tuple[int, int], list] = defaultdict(list)
        for node in self._endpoints:
            coord = self.node_coord.get(node)
            if coord:
                buckets[_snap(coord[0], coord[1], cell)].append(node)

        added = 0
        for key, nodes in buckets.items():
            # Compare against this bucket and its neighbours so pairs are not missed
            # at a cell boundary.
            candidates = []
            for dlat in (-1, 0, 1):
                for dlng in (-1, 0, 1):
                    candidates.extend(buckets.get((key[0] + dlat, key[1] + dlng), ()))
            for a in nodes:
                ca = self.node_coord[a]
                for b in candidates:
                    if a >= b:
                        continue
                    cb = self.node_coord[b]
                    distance = haversine_miles(ca[1], ca[0], cb[1], cb[0])
                    if distance * 1609.34 > STITCH_METERS:
                        continue
                    self.adjacency[a].append((b, distance, 0.0, "__stitch__"))
                    self.adjacency[b].append((a, distance, 0.0, "__stitch__"))
                    added += 1

        if verbose:
            print(f"  stitched {added} endpoint pairs within {STITCH_METERS:.0f} m")

    def _sample_node_elevations(self, verbose: bool = True) -> None:
        """Elevation per node, straight from the DEM.

        Reading climb off each trail's 250-point downsampled profile made gain
        accumulate noise as edges multiplied — the same failure the DEM sampler
        itself had before it grew a hysteresis threshold. One authoritative sample
        per node removes the double approximation.
        """
        from concurrent.futures import ThreadPoolExecutor

        from .elevation import METERS_TO_FEET, sample_elevation_m

        nodes = list(self.node_coord.items())
        if verbose:
            print(f"  sampling DEM for {len(nodes)} nodes")

        def work(item):
            node, (lng, lat) = item
            metres = sample_elevation_m(lat, lng)
            return node, (metres * METERS_TO_FEET) if metres is not None else None

        with ThreadPoolExecutor(max_workers=16) as pool:
            for node, feet in pool.map(work, nodes, chunksize=256):
                if feet is not None:
                    self.node_ele[node] = feet

        if verbose:
            print(f"  elevation known for {len(self.node_ele)} of {len(nodes)} nodes")

    def _apply_elevation_costs(self) -> None:
        """Rewrite edge climb from node elevations, directionally."""
        for node, edges in self.adjacency.items():
            here = self.node_ele.get(node)
            rebuilt = []
            for neighbour, miles, _old_gain, trail_id in edges:
                there = self.node_ele.get(neighbour)
                climb = max(0.0, there - here) if (here is not None and there is not None) else 0.0
                rebuilt.append((neighbour, miles, climb, trail_id))
            self.adjacency[node] = rebuilt

    def _add_line(
        self,
        trail_id: str,
        line: list[list[float]],
        junctions: set,
        profile: list[dict],
    ) -> None:
        """Split one line into edges that run junction-to-junction."""
        # Both ends of the line are stitch candidates.
        self._endpoints.add(_snap(line[0][0], line[0][1]))
        self._endpoints.add(_snap(line[-1][0], line[-1][1]))

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

            # Climb is filled in later from per-node DEM samples.
            self.adjacency[a].append((b, seg_miles, 0.0, trail_id))
            self.adjacency[b].append((a, seg_miles, 0.0, trail_id))

            # Keep the vertices this edge actually follows, thinned a little: a
            # drawn route must trace the trail, not cut between its endpoints.
            shape = line[start : i + 1]
            if len(shape) > 60:
                step = len(shape) / 60
                shape = [shape[int(k * step)] for k in range(60)] + [shape[-1]]
            self.edge_shape.setdefault((a, b), shape)
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
        # Cell radius must cover max_miles, not a fixed count: with 4 m snapping a
        # hardcoded +/-3 cells searched only 12 m, so a trailhead coordinate a short
        # way off the line found nothing and the route silently had no start node.
        span = max(1, int(math.ceil((max_miles * 1609.34) / SNAP_METERS)))
        for dlat in range(-span, span + 1):
            for dlng in range(-span, span + 1):
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
        path: list = [target]
        # The trail each hop was walked on, aligned so edge_trails[i] is the edge
        # path[i] -> path[i + 1]. The collapsed `trails` list below loses where one
        # trail stops and the next starts, which is exactly what a caller needs to
        # draw the composition rather than a single anonymous line.
        edge_trails: list[str] = []
        node = target
        while node in previous:
            node, trail_id = previous[node]
            path.append(node)
            edge_trails.append(trail_id)
            if trail_id != "__stitch__" and (not trails or trails[-1] != trail_id):
                trails.append(trail_id)
        trails.reverse()
        path.reverse()
        edge_trails.reverse()

        gain_ft, loss_ft = self._path_gain(path)

        return {
            "miles_one_way": round(distances[target], 2),
            "gain_ft_one_way": gain_ft,
            "loss_ft_one_way": loss_ft,
            "trail_ids": trails,
            "trail_names": [self.trail_names.get(t, t) for t in trails],
            "node_count": len(path),
            # Drawable geometry that follows the trail, assembled from the edge
            # shapes rather than joining node positions.
            "coordinates": self._path_shape(path),
            # The same route split by trail, so each part can be drawn and named.
            "legs": self._split_into_legs(path, edge_trails, distances),
            "profile": [
                {"ft": round(self.node_ele[n])} for n in path if n in self.node_ele
            ],
        }

    def _split_into_legs(self, path: list, edge_trails: list[str], distances: dict) -> list[dict]:
        """Group a node path into runs that stay on one trail.

        A composed hike is only legible if you can see *which* trail each part of it
        is — "13.9 mi" says nothing about the fact that it is four trails in a row.
        One merged line cannot show that, so each run carries its own drawable
        geometry and the trail it belongs to.

        Runs are keyed by trail *name*, not trail id. Sources overlap: Happy Isles
        to Half Dome alternates between two distinct records both called "John Muir
        Trail", which by id is 90 legs and by name is 4. The id split is real but it
        describes who published the geometry, not which trail a walker is on, and
        the walker's question is the one worth answering here.

        Leg mileage is read off the Dijkstra distances rather than re-measured, so
        the legs sum to the route total exactly.
        """
        def key(trail_id: str) -> str:
            return self.trail_names.get(trail_id) or trail_id

        runs: list[dict] = []
        for i, trail_id in enumerate(edge_trails):
            # A stitch is a metres-long join between two agencies' endpoints, not a
            # trail anyone walks as a named thing. It extends the run it lands in.
            if runs and (trail_id == "__stitch__" or runs[-1]["name"] == key(trail_id)):
                runs[-1]["nodes"].append(path[i + 1])
            elif runs and runs[-1]["trail_id"] == "__stitch__":
                # A route that opened on a stitch has no name yet; the first real
                # trail claims it.
                runs[-1].update(trail_id=trail_id, name=key(trail_id))
                runs[-1]["nodes"].append(path[i + 1])
            else:
                runs.append(
                    {
                        "trail_id": trail_id,
                        "name": key(trail_id),
                        "nodes": [path[i], path[i + 1]],
                    }
                )

        # Concurrent routes are the reason this pass exists. A long-distance
        # relation ("Bay Area Ridge Trail") is mapped over the same ground as the
        # local trail it follows, so consecutive edges flip between two records and
        # a 17 mi hike came back as 80 legs alternating every 0.02 mi. Absorbing
        # runs shorter than a stride into their larger neighbour reports what the
        # walker experiences: one trail, which happens to carry two names.
        MIN_LEG_MILES = 0.15

        legs: list[dict] = []
        for run in runs:
            coordinates = self._path_shape(run["nodes"])
            if len(coordinates) < 2:
                continue
            start_mi = distances.get(run["nodes"][0], 0.0)
            end_mi = distances.get(run["nodes"][-1], start_mi)
            legs.append(
                {
                    "trail_id": run["trail_id"],
                    "name": "connector" if run["trail_id"] == "__stitch__" else run["name"],
                    "miles": round(max(0.0, end_mi - start_mi), 2),
                    "coordinates": coordinates,
                }
            )

        return self._absorb_short_legs(legs, MIN_LEG_MILES)

    @staticmethod
    def _absorb_short_legs(legs: list[dict], minimum: float) -> list[dict]:
        """Merge sub-stride legs into the larger neighbour, shortest first.

        Shortest-first matters: absorbing in path order lets one long trail swallow
        a genuine short connector that a later, larger neighbour should have taken.
        A single leg is never absorbed — a 0.1 mi walk is still the whole walk.
        """
        legs = [dict(leg) for leg in legs]

        while len(legs) > 1:
            index = min(range(len(legs)), key=lambda i: legs[i]["miles"])
            if legs[index]["miles"] >= minimum:
                break

            before = legs[index - 1] if index > 0 else None
            after = legs[index + 1] if index + 1 < len(legs) else None
            # Prefer the bigger neighbour; it is the trail the walker is really on.
            target = before if after is None else after if before is None else (
                before if before["miles"] >= after["miles"] else after
            )
            victim = legs.pop(index)
            target["miles"] = round(target["miles"] + victim["miles"], 2)
            if target is before:
                target["coordinates"] = target["coordinates"] + victim["coordinates"]
            else:
                target["coordinates"] = victim["coordinates"] + target["coordinates"]

        # Adjacent legs can share a name once the flips between them are gone.
        merged: list[dict] = []
        for leg in legs:
            if merged and merged[-1]["name"] == leg["name"]:
                merged[-1]["miles"] = round(merged[-1]["miles"] + leg["miles"], 2)
                merged[-1]["coordinates"] = merged[-1]["coordinates"] + leg["coordinates"]
                continue
            merged.append(leg)
        return merged

    def _path_shape(self, path: list) -> list[list[float]]:
        """Trace the real trail vertices along a node path.

        Joining node positions draws a straight line wherever a trail has no
        junction between its ends — a 2.5 mi trail became one segment cutting
        across the terrain. Each edge carries the vertices it actually follows, so
        they are stitched here, reversed when the edge is walked backwards.
        """
        shape: list[list[float]] = []
        for a, b in zip(path, path[1:]):
            piece = self.edge_shape.get((a, b))
            if piece is None:
                piece = self.edge_shape.get((b, a))
                if piece is not None:
                    piece = list(reversed(piece))
            if not piece:
                # A stitch edge, or geometry we never stored: fall back to the
                # straight hop between the two nodes rather than dropping it.
                piece = [
                    list(self.node_coord[n]) for n in (a, b) if n in self.node_coord
                ]
            for point in piece:
                point = list(point)
                if not shape or shape[-1] != point:
                    shape.append(point)
        return shape

    def _path_gain(self, path: list) -> tuple[int, int]:
        """Ascent and descent along a node path, with the DEM noise threshold.

        Summing max(0, delta) per edge inflates badly: nodes are metres apart, so
        every few feet of DEM noise is counted as climb. Whitney came out at 14,232
        ft against a true 6,100. Running the same hysteresis walk the DEM sampler
        uses, once over the whole path, removes it.
        """
        from .elevation import compute_gain

        series = [self.node_ele[n] for n in path if n in self.node_ele]
        if len(series) < 2:
            # Unknown, not flat. Callers must be able to tell the difference —
            # reporting 0 ft for a Half Dome ascent is worse than saying nothing.
            return None, None
        gain, loss = compute_gain(series)
        return int(round(gain)), int(round(loss))

    def compose_hike(self, source, target, out_and_back: bool = True) -> dict | None:
        """A complete hike: out to the destination and back."""
        leg = self.shortest_path(source, target)
        if not leg:
            return None
        # Walking back reverses the profile, so the return leg climbs exactly what
        # the outbound leg descended.
        out_gain = leg["gain_ft_one_way"]
        out_loss = leg.get("loss_ft_one_way")
        if out_gain is None:
            total_gain = None
        elif out_and_back:
            total_gain = out_gain + (out_loss or 0)
        else:
            total_gain = out_gain
        return {
            "miles": round(leg["miles_one_way"] * (2 if out_and_back else 1), 2),
            "gain_ft": total_gain,
            "one_way_miles": leg["miles_one_way"],
            "gain_ft_one_way": leg["gain_ft_one_way"],
            "loss_ft_one_way": leg.get("loss_ft_one_way", 0),
            "trail_names": leg["trail_names"],
            "trail_ids": leg["trail_ids"],
            "segments_used": len(leg["trail_ids"]),
            "route_type": "out-and-back" if out_and_back else "point-to-point",
            "coordinates": leg.get("coordinates") or [],
            "legs": leg.get("legs") or [],
            "node_count": leg.get("node_count", 0),
        }


def load_graph(verbose: bool = True) -> TrailGraph:
    index = json.loads((_BASE_DIR / "data" / "trails_index.json").read_text())
    geometries = json.loads((_BASE_DIR / "data" / "trails_geom.json").read_text())
    return TrailGraph().build(index["trails"], geometries, verbose=verbose)


if __name__ == "__main__":
    graph = load_graph()
    print(f"\nnodes: {len(graph.adjacency):,}")
