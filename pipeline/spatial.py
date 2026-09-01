"""A small grid-based spatial index.

Deliberately dependency-free. shapely/rtree/geopandas all pull in GEOS and a large
binary install; for point-near-line queries over a few hundred thousand features a
uniform lat/lng grid is fast enough and keeps the pipeline installable anywhere.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

from .normalize import haversine_miles

# Grid cell size in degrees. ~0.05 deg is roughly 3.5 mi of latitude, which comfortably
# exceeds the join radii used for scenery features.
DEFAULT_CELL_DEG = 0.05


class PointGrid:
    """Bucket points into grid cells for fast radius queries."""

    def __init__(self, cell_deg: float = DEFAULT_CELL_DEG):
        self.cell_deg = cell_deg
        self._cells: dict[tuple[int, int], list[dict]] = defaultdict(list)
        self._count = 0

    def _cell(self, lat: float, lng: float) -> tuple[int, int]:
        return (int(math.floor(lat / self.cell_deg)), int(math.floor(lng / self.cell_deg)))

    def add(self, lat: float, lng: float, payload: dict) -> None:
        self._cells[self._cell(lat, lng)].append({"lat": lat, "lng": lng, **payload})
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def near(self, lat: float, lng: float, radius_miles: float) -> list[dict]:
        """All indexed points within `radius_miles` of a location."""
        # One degree of latitude is ~69 mi; longitude shrinks with latitude.
        lat_span = radius_miles / 69.0
        cos_lat = max(0.01, math.cos(math.radians(lat)))
        lng_span = radius_miles / (69.0 * cos_lat)

        lat_cells = range(
            int(math.floor((lat - lat_span) / self.cell_deg)),
            int(math.floor((lat + lat_span) / self.cell_deg)) + 1,
        )
        lng_cells = range(
            int(math.floor((lng - lng_span) / self.cell_deg)),
            int(math.floor((lng + lng_span) / self.cell_deg)) + 1,
        )

        found = []
        for lat_cell in lat_cells:
            for lng_cell in lng_cells:
                for point in self._cells.get((lat_cell, lng_cell), ()):
                    distance = haversine_miles(lat, lng, point["lat"], point["lng"])
                    if distance <= radius_miles:
                        found.append({**point, "distance_mi": round(distance, 3)})
        return found

    def near_path(
        self,
        coordinates: Iterable[list[float]],
        radius_miles: float,
        stride: int = 1,
    ) -> dict[str, dict]:
        """Points near any vertex of a path, keyed by payload `id`, nearest kept.

        `stride` samples every Nth vertex; trail geometries are dense enough that
        sampling keeps the join cheap without missing features.
        """
        best: dict[str, dict] = {}
        for index, coordinate in enumerate(coordinates):
            if index % stride:
                continue
            if not isinstance(coordinate, list) or len(coordinate) < 2:
                continue
            for hit in self.near(coordinate[1], coordinate[0], radius_miles):
                key = str(hit.get("id"))
                if key not in best or hit["distance_mi"] < best[key]["distance_mi"]:
                    best[key] = hit
        return best


def bbox_tiles(bbox: tuple[float, float, float, float], step_deg: float) -> list[tuple[float, float, float, float]]:
    """Split a (min_lng, min_lat, max_lng, max_lat) box into smaller boxes."""
    min_lng, min_lat, max_lng, max_lat = bbox
    tiles = []
    lat = min_lat
    while lat < max_lat:
        lng = min_lng
        while lng < max_lng:
            tiles.append((lng, lat, min(lng + step_deg, max_lng), min(lat + step_deg, max_lat)))
            lng += step_deg
        lat += step_deg
    return tiles
