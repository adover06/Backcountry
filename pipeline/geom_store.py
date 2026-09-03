"""Trail geometry in SQLite, at several levels of detail.

The problem this solves, measured on the live backend:

    cold start                      117 MB RSS
    after a dots request            201 MB          browse path, no geometry
    after ONE trail detail request  948 MB, 4.68s   parsed all 167 MB for one trail

`load_geometry()` reads the whole sidecar into memory to answer a single-key lookup.
That is 5.7x the file size in Python objects and most of the backend's footprint,
and on a 1 GB VPS it is the difference between running and not.

**Why SQLite rather than the Postgres already running.** This is not a rejection of
the database idea, it is a choice of which database:

* The lookup is `SELECT ... WHERE trail_id = ?`. There is nothing relational about
  it and no spatial query — the image is `postgres:16-alpine`, which has no PostGIS.
* `get_geometry()` is synchronous and called from synchronous code. The app's
  Postgres layer is async SQLAlchemy, so routing this through it would mean making
  the map, detail, photo and graph paths async for a primary-key read.
* Geometry is build output, versioned with `trails_index.json`. Keeping it in a file
  the build writes means the two cannot drift; keeping it in Postgres would make
  every index rebuild a data migration.
* Postgres holds the things that genuinely need it — users, trips, saved trails.
  Read-only build artefacts are not that.

SQLite reads a single row through the OS page cache, so resident memory stays flat
no matter how many trails are requested.

**Levels of detail.** The same pass stores simplified copies. Measured on a dense
Sierra viewport of 320 trails:

    full     206,311 coords   7.90 MB
    z14       38,213 coords   1.47 MB    ~5 m
    z12       17,476 coords   0.68 MB    ~17 m
    z10        7,125 coords   0.28 MB    ~55 m   (1.5 screen pixels at that zoom)

Serving `z10` where the map draws at z10 is 28x less data and indistinguishable.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
GEOM_DB_PATH = Path(os.environ.get("TRAILS_GEOM_DB", _BASE_DIR / "data" / "trails_geom.sqlite"))
GEOM_JSON_PATH = _BASE_DIR / "data" / "trails_geom.json"

# Tolerance in degrees per tier. A degree of longitude is ~85 km at 37 N, so these
# are roughly 5 m, 17 m and 55 m — each under two screen pixels at its zoom.
TIERS = {"z14": 5e-5, "z12": 1.5e-4, "z10": 5e-4}

# Requesting detail finer than stored falls back to `full`.
TIER_ORDER = ["z10", "z12", "z14", "full"]


def simplify(points: list, epsilon: float) -> list:
    """Ramer-Douglas-Peucker.

    Iterative rather than recursive: a 4,000-point trail recurses deeper than
    CPython's default limit, and the crash would only appear on the longest trails
    in the index — the ones most worth simplifying.
    """
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    eps_sq = epsilon * epsilon

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x1, y1 = points[start][0], points[start][1]
        x2, y2 = points[end][0], points[end][1]
        dx, dy = x2 - x1, y2 - y1
        denom = dx * dx + dy * dy

        worst, worst_i = -1.0, -1
        for i in range(start + 1, end):
            x0, y0 = points[i][0], points[i][1]
            if denom == 0:
                dist = (x0 - x1) ** 2 + (y0 - y1) ** 2
            else:
                t = ((x0 - x1) * dx + (y0 - y1) * dy) / denom
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                px, py = x1 + t * dx, y1 + t * dy
                dist = (x0 - px) ** 2 + (y0 - py) ** 2
            if dist > worst:
                worst, worst_i = dist, i

        if worst > eps_sq:
            keep[worst_i] = True
            stack.append((start, worst_i))
            stack.append((worst_i, end))

    return [p for p, k in zip(points, keep) if k]


def simplify_geometry(geometry: dict, epsilon: float) -> dict:
    """Simplify a LineString or MultiLineString, dropping parts that collapse.

    A part reduced below two points is not a line any more. It is dropped rather
    than emitted as a degenerate one-point "line", and if every part collapses the
    geometry is returned unsimplified — better a heavy trail than a missing one.
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []

    if gtype == "LineString":
        out = simplify(coords, epsilon)
        return {"type": "LineString", "coordinates": out} if len(out) >= 2 else geometry

    if gtype == "MultiLineString":
        parts = [simplify(part, epsilon) for part in coords]
        parts = [p for p in parts if len(p) >= 2]
        if not parts:
            return geometry
        return {"type": "MultiLineString", "coordinates": parts}

    return geometry


def build(
    source: Path = GEOM_JSON_PATH,
    target: Path = GEOM_DB_PATH,
    verbose: bool = True,
) -> Path:
    """Write the SQLite store from the JSON sidecar."""
    if verbose:
        print(f"  reading {source.name}…")
    entries = json.loads(source.read_text())

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()

    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(
        """CREATE TABLE geometry (
            trail_id TEXT PRIMARY KEY,
            full     TEXT NOT NULL,
            z14      TEXT,
            z12      TEXT,
            z10      TEXT,
            profile  TEXT
        )"""
    )

    rows = []
    for i, (trail_id, entry) in enumerate(entries.items(), start=1):
        entry = entry or {}
        geometry = entry.get("geometry")
        if not geometry:
            continue
        row = [trail_id, json.dumps(geometry, separators=(",", ":"))]
        for tier in ("z14", "z12", "z10"):
            row.append(
                json.dumps(simplify_geometry(geometry, TIERS[tier]), separators=(",", ":"))
            )
        profile = entry.get("profile")
        row.append(json.dumps(profile, separators=(",", ":")) if profile else None)
        rows.append(tuple(row))

        if len(rows) >= 500:
            conn.executemany("INSERT INTO geometry VALUES (?,?,?,?,?,?)", rows)
            rows.clear()
            if verbose:
                print(f"    {i}/{len(entries)}")

    if rows:
        conn.executemany("INSERT INTO geometry VALUES (?,?,?,?,?,?)", rows)

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp.replace(target)

    if verbose:
        print(f"  wrote {target.name} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


_conn: sqlite3.Connection | None = None


def _connection() -> sqlite3.Connection | None:
    """Open the store read-only, once. Returns None when it has not been built."""
    global _conn
    if _conn is not None:
        return _conn
    if not GEOM_DB_PATH.exists():
        return None
    # check_same_thread=False: the connection is read-only and SQLite serialises
    # access itself, and the API serves requests from a thread pool.
    _conn = sqlite3.connect(
        f"file:{GEOM_DB_PATH}?mode=ro", uri=True, check_same_thread=False
    )
    return _conn


def available() -> bool:
    return _connection() is not None


def get(trail_id: str, detail: str = "full") -> dict | None:
    """One trail's geometry and profile, or None if the store has no such trail.

    `detail` picks a level; a tier that was not stored falls back to `full` rather
    than returning nothing.
    """
    conn = _connection()
    if conn is None:
        return None
    if detail not in ("full", "z14", "z12", "z10"):
        detail = "full"

    row = conn.execute(
        f"SELECT {detail}, full, profile FROM geometry WHERE trail_id = ?", (trail_id,)
    ).fetchone()
    if row is None:
        return None

    geometry_json = row[0] or row[1]
    return {
        "geometry": json.loads(geometry_json),
        "profile": json.loads(row[2]) if row[2] else None,
    }


def detail_for_zoom(zoom: float | None) -> str:
    """Level of detail appropriate to a map zoom."""
    if zoom is None:
        return "full"
    if zoom < 11:
        return "z10"
    if zoom < 13:
        return "z12"
    if zoom < 15:
        return "z14"
    return "full"


if __name__ == "__main__":
    build()
