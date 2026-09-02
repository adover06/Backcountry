"""Request-time access to the trail routing graph.

The graph takes ~14s and a few hundred MB to build from 10,694 trails, so it is
constructed once on first use and held for the process lifetime. Building it per
request would be absurd; precomputing it to disk is the eventual answer, but a
lazy singleton is enough while the graph is still being validated.

Loading is guarded by a lock so two concurrent first-requests cannot both start a
build, and failures are recorded rather than retried on every call.
"""

from __future__ import annotations

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

    # Collapse the repeated trail names a path picks up when it crosses the same
    # trail several times: ["JMT", "JMT", "Mist", "JMT"] -> ["JMT", "Mist", "JMT"].
    names: list[str] = []
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
        "segments_used": hike["segments_used"],
        "node_count": hike.get("node_count", 0),
        "geometry": {"type": "LineString", "coordinates": hike["coordinates"]},
        "snapped": {
            "start": list(graph.node_coord.get(start_node, ())),
            "end": list(graph.node_coord.get(end_node, ())),
        },
    }
