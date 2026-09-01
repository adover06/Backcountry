"""Derive real elevation data for trails by sampling a tiled DEM.

Uses AWS Terrain Tiles (Terrarium encoding), which are public, free, require no API
key, and cover the globe at roughly 30 m resolution in the continental US.

    elevation_m = (R * 256 + G + B / 256) - 32768

Tiles are cached on disk so a rebuild does not refetch. Elevation gain is computed
with a noise threshold: raw DEM sampling along a line accumulates small false
climbs, exactly like unsmoothed GPS elevation, and summing every positive delta
inflates gain by 2-3x. See `GAIN_THRESHOLD_FT`.
"""

from __future__ import annotations

import io
import math
import os
import threading
from pathlib import Path

import requests

from .normalize import haversine_miles

TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_ZOOM = 13  # ~19 m/px at CA latitudes; good balance of detail vs tile count
TILE_SIZE = 256

_CACHE_DIR = Path(os.environ.get("DEM_CACHE_DIR", Path(__file__).resolve().parent.parent / ".dem_cache"))

# Only count a climb once it exceeds this, to reject DEM sampling noise.
GAIN_THRESHOLD_FT = 15.0

# Elevation is sampled at a fixed spacing along the trail, not a fixed point count.
# A fixed count under-samples long trails badly: 300 points over a 145 mi trail is one
# reading every half mile, which averages real climbs away and understates gain.
SAMPLE_SPACING_MI = 0.05
MAX_SAMPLE_POINTS = 2500

# The stored profile is downsampled for charting; gain is always computed on the
# full-resolution series above.
MAX_PROFILE_POINTS = 250

METERS_TO_FEET = 3.28084

_tile_lock = threading.Lock()
_tile_memo: dict[tuple[int, int, int], object] = {}


def _deg2tile(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """Web-mercator tile coordinates (fractional) for a lat/lng."""
    lat = max(-85.05112878, min(85.05112878, lat))
    lat_rad = math.radians(lat)
    n = 2.0**zoom
    x = (lng + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _fetch_tile(z: int, x: int, y: int):
    """Return a Terrarium tile as a PIL image, from memory, disk, or network."""
    key = (z, x, y)
    with _tile_lock:
        if key in _tile_memo:
            return _tile_memo[key]

    from PIL import Image

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{z}_{x}_{y}.png"

    image = None
    if path.exists():
        try:
            image = Image.open(path).convert("RGB")
            image.load()
        except Exception:
            image = None  # corrupt cache entry; refetch below

    if image is None:
        response = requests.get(
            TERRARIUM_URL.format(z=z, x=x, y=y),
            timeout=20,
            headers={"User-Agent": "OpenTrails/1.0"},
        )
        response.raise_for_status()
        payload = response.content
        path.write_bytes(payload)
        image = Image.open(io.BytesIO(payload)).convert("RGB")
        image.load()

    with _tile_lock:
        # Bound the in-memory tile cache; disk still backs every tile.
        if len(_tile_memo) > 400:
            _tile_memo.clear()
        _tile_memo[key] = image
    return image


def sample_elevation_m(lat: float, lng: float, zoom: int = TILE_ZOOM) -> float | None:
    """Elevation in meters at a point, or None if the tile could not be read."""
    try:
        fx, fy = _deg2tile(lat, lng, zoom)
        tx, ty = int(fx), int(fy)
        image = _fetch_tile(zoom, tx, ty)
        px = min(TILE_SIZE - 1, max(0, int((fx - tx) * TILE_SIZE)))
        py = min(TILE_SIZE - 1, max(0, int((fy - ty) * TILE_SIZE)))
        r, g, b = image.getpixel((px, py))
        return (r * 256 + g + b / 256) - 32768
    except Exception:
        return None


def _flatten(geometry: dict) -> list[list[float]]:
    """All coordinates of a Multi/LineString as a flat [[lng, lat], ...] list."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [c for c in coords if isinstance(c, list) and len(c) >= 2]
    if gtype == "MultiLineString":
        return [c for line in coords for c in line if isinstance(c, list) and len(c) >= 2]
    return []


def _resample(points: list[list[float]], limit: int) -> list[list[float]]:
    """Evenly thin a coordinate list to at most `limit` points, keeping the ends."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[int(round(i * step))] for i in range(limit)]


def _sample_points(points: list[list[float]], spacing_mi: float, cap: int) -> list[list[float]]:
    """Thin a coordinate list to roughly one point per `spacing_mi` of travel.

    Keeps vertices where the trail is dense and does not drop detail on long
    trails the way a fixed point budget does.
    """
    if len(points) < 2:
        return points

    kept = [points[0]]
    accumulated = 0.0
    for i in range(1, len(points)):
        previous, current = points[i - 1], points[i]
        accumulated += haversine_miles(previous[1], previous[0], current[1], current[0])
        if accumulated >= spacing_mi:
            kept.append(current)
            accumulated = 0.0
    if kept[-1] is not points[-1]:
        kept.append(points[-1])

    return _resample(kept, cap) if len(kept) > cap else kept


def compute_gain(elevations_ft: list[float], threshold_ft: float = GAIN_THRESHOLD_FT) -> tuple[float, float]:
    """Total ascent and descent, ignoring wiggles smaller than `threshold_ft`.

    Hysteresis walk: a move beyond the threshold from the last committed point is
    counted and sets the current direction; smaller moves that continue the current
    direction extend it. This rejects the sampling noise that makes a naive
    sum-of-positive-deltas overstate gain by 2-3x, while still tracking every real
    climb and descent on an undulating route.
    """
    if len(elevations_ft) < 2:
        return 0.0, 0.0

    gain = loss = 0.0
    anchor = elevations_ft[0]
    direction = 0  # -1 descending, +1 ascending, 0 undecided

    for value in elevations_ft[1:]:
        delta = value - anchor
        if delta >= threshold_ft:
            # Committed climb. Counted regardless of the previous direction, so a
            # route can reverse from descending to ascending any number of times.
            gain += delta
            anchor = value
            direction = 1
        elif delta <= -threshold_ft:
            loss += -delta
            anchor = value
            direction = -1
        elif direction > 0 and value > anchor:
            # Still climbing within the current run: extend without re-anchoring.
            gain += value - anchor
            anchor = value
        elif direction < 0 and value < anchor:
            loss += anchor - value
            anchor = value

    return round(gain, 1), round(loss, 1)


def elevation_for_geometry(geometry: dict) -> dict | None:
    """Sample a trail geometry and return its elevation summary and profile.

    Returns None when the DEM could not be sampled at all, so callers can record
    "unknown" rather than a fabricated zero.
    """
    points = _flatten(geometry)
    if len(points) < 2:
        return None

    sampled = _sample_points(points, SAMPLE_SPACING_MI, MAX_SAMPLE_POINTS)

    elevations_ft: list[float] = []
    distances_mi: list[float] = []
    running = 0.0
    previous: list[float] | None = None
    misses = 0

    for lng, lat in ((p[0], p[1]) for p in sampled):
        if previous is not None:
            running += haversine_miles(previous[1], previous[0], lat, lng)
        previous = [lng, lat]

        meters = sample_elevation_m(lat, lng)
        if meters is None:
            misses += 1
            continue
        elevations_ft.append(meters * METERS_TO_FEET)
        distances_mi.append(round(running, 3))

    # Require a majority of samples to have landed before trusting the result.
    if len(elevations_ft) < 2 or misses > len(sampled) / 2:
        return None

    gain_ft, loss_ft = compute_gain(elevations_ft)

    # Gain is computed on the full-resolution series; only the chart data is thinned.
    profile_indices = range(len(elevations_ft))
    if len(elevations_ft) > MAX_PROFILE_POINTS:
        step = (len(elevations_ft) - 1) / (MAX_PROFILE_POINTS - 1)
        profile_indices = [int(round(i * step)) for i in range(MAX_PROFILE_POINTS)]
    profile = [{"mi": distances_mi[i], "ft": round(elevations_ft[i])} for i in profile_indices]

    return {
        "gain_ft": int(round(gain_ft)),
        "loss_ft": int(round(loss_ft)),
        "min_ft": int(round(min(elevations_ft))),
        "max_ft": int(round(max(elevations_ft))),
        "start_ft": int(round(elevations_ft[0])),
        "end_ft": int(round(elevations_ft[-1])),
        "samples": len(elevations_ft),
        "source": "AWS Terrarium DEM",
        "profile": profile,
    }


if __name__ == "__main__":
    # Sanity check: Mount Whitney summit is 14,505 ft.
    meters = sample_elevation_m(36.5785, -118.2923)
    print(f"Whitney summit sample: {meters * METERS_TO_FEET:.0f} ft (expect ~14,505)")
