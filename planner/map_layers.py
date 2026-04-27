"""Map layer builder for frontend overlays."""

from __future__ import annotations


def build_map_layers(route: dict, fire: dict) -> dict:
    route_geojson = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "LineString",
            "coordinates": [[p["lng"], p["lat"]] for p in route.get("points", [])],
        },
    }

    return {
        "route": route_geojson,
        "fire_perimeters": fire.get("perimeters"),
    }
