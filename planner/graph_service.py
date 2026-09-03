"""Request-time access to the trail routing graph.

The graph takes ~14s and a few hundred MB to build from 10,694 trails, so it is
constructed once on first use and held for the process lifetime. Building it per
request would be absurd; precomputing it to disk is the eventual answer, but a
lazy singleton is enough while the graph is still being validated.

Loading is guarded by a lock so two concurrent first-requests cannot both start a
build, and failures are recorded rather than retried on every call.
"""

from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_graph = None
_error: str | None = None
_build_seconds: float | None = None


def get_graph():
    """The shared graph, or None if it could not be built."""
    global _graph, _error, _build_seconds

    if _graph is not None or _error is not None:
        return _graph

    with _lock:
        # Another thread may have finished while this one waited.
        if _graph is not None or _error is not None:
            return _graph
        try:
            from pipeline.trail_graph import load_graph

            started = time.time()
            _graph = load_graph(verbose=False)
            _build_seconds = round(time.time() - started, 1)
        except Exception as exc:
            _error = str(exc)
            return None
    return _graph


def prewarm() -> bool:
    """Build the graph in the background at startup, if the operator asked for it.

    Off by default, and deliberately so. Building takes ~55s on the current index
    and the finished graph holds ~1.8 GB resident — more than the whole rest of the
    process. On a small VPS that is the difference between running and being
    OOM-killed, so paying it eagerly has to be a choice rather than a default.

    Set `GRAPH_PREWARM=1` where there is memory to spare: the build then happens
    while the container is starting instead of inside the first user's request.
    """
    if os.environ.get("GRAPH_PREWARM", "").strip().lower() not in {"1", "true", "yes"}:
        return False
    threading.Thread(target=get_graph, name="graph-prewarm", daemon=True).start()
    return True


def status() -> dict:
    """Graph size and build state, without forcing a build."""
    if _graph is None and _error is None:
        return {"loaded": False, "building": _lock.locked()}
    if _error:
        return {"loaded": False, "error": _error}
    return {
        "loaded": True,
        "nodes": len(_graph.adjacency),
        "edges": sum(len(v) for v in _graph.adjacency.values()) // 2,
        "nodes_with_elevation": len(_graph.node_ele),
        "build_seconds": _build_seconds,
    }


def compose(
    start: tuple[float, float],
    end: tuple[float, float],
    out_and_back: bool = True,
    snap_miles: float = 0.25,
) -> dict:
    """Compose a hike between two coordinates.

    Returns a payload that always says *why* it failed rather than an empty
    result: whether the graph is unavailable, whether a point had no trail near
    it, or whether the two points are genuinely not connected.
    """
    graph = get_graph()
    if graph is None:
        return {"ok": False, "reason": "graph_unavailable", "detail": _error}

    start_node = graph.nearest_node(start[0], start[1], max_miles=snap_miles)
    if not start_node:
        return {
            "ok": False,
            "reason": "no_trail_near_start",
            "detail": f"No mapped trail within {snap_miles} mi of the start point.",
        }

    end_node = graph.nearest_node(end[0], end[1], max_miles=snap_miles)
    if not end_node:
        return {
            "ok": False,
            "reason": "no_trail_near_end",
            "detail": f"No mapped trail within {snap_miles} mi of the destination.",
        }

    hike = graph.compose_hike(start_node, end_node, out_and_back=out_and_back)
    if not hike:
        return {
            "ok": False,
            "reason": "not_connected",
            "detail": (
                "Both points are on the trail network but no route joins them. "
                "Agency datasets often do not meet where their trails do."
            ),
        }

    # Names come from the merged legs, not the raw per-edge list. Collapsing only
    # *consecutive* duplicates is not enough: where a long-distance route is mapped
    # over the same ground as the local trail, the raw list alternates A,B,A,B and
    # a 17 mi hike listed 80 names. The legs have already absorbed those flips.
    legs = hike.get("legs") or []
    names: list[str] = []
    for leg in legs:
        name = leg.get("name")
        if name and name != "connector" and (not names or names[-1] != name):
            names.append(name)
    if not names:
        for name in hike["trail_names"]:
            if not names or names[-1] != name:
                names.append(name)

    return {
        "ok": True,
        "miles": hike["miles"],
        "gain_ft": hike["gain_ft"],
        "one_way_miles": hike["one_way_miles"],
        "gain_ft_one_way": hike.get("gain_ft_one_way"),
        "route_type": hike["route_type"],
        "trail_names": names,
        # The merged legs, not the raw per-edge count. `trail_ids` counts every
        # edge-level trail change including the concurrent-route flips the legs
        # absorb, which reported 80 for a hike made of 8 pieces.
        "segments_used": len(hike.get("legs") or []) or hike["segments_used"],
        "node_count": hike.get("node_count", 0),
        "geometry": {"type": "LineString", "coordinates": hike["coordinates"]},
        # The same route, split where it changes trail. `geometry` above is the
        # whole line and cannot show that a 13.9 mi hike is four trails in a row;
        # these can be drawn and labelled one by one.
        "segments": [
            {
                "trail_id": leg["trail_id"],
                "name": leg["name"],
                "miles": leg["miles"],
                "geometry": {"type": "LineString", "coordinates": leg["coordinates"]},
            }
            for leg in hike.get("legs") or []
        ],
        "snapped": {
            "start": list(graph.node_coord.get(start_node, ())),
            "end": list(graph.node_coord.get(end_node, ())),
        },
    }
