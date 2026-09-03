"""Pull hiking trails out of OpenStreetMap for an arbitrary region.

Why this replaces what was here: the previous `magic.py` drove Playwright at
AllTrails to see whether DataDome would block it. It does, reliably, and even when
it does not the data is licensed such that we cannot keep it. OSM publishes the same
class of data under ODbL, exposes it through a real query API, and does not need to
be scraped at all. So this module points at Overpass.

The one thing browser automation is still good for here is transport. The public
Overpass mirrors rate-limit and occasionally 403 plain HTTP clients from datacenter
IPs; issuing the same POST from inside a real browser context gets through. That is
what `--transport playwright` and `--transport camoufox` are for. They are a
fallback, not the default: Overpass is a public API and the polite thing is to call
it directly, with backoff, from `urllib`.

Region input takes three forms:

    python magic.py --place "Boulder, Colorado"
    python magic.py --bbox 39.95,-105.35,40.10,-105.20        # S,W,N,E
    python magic.py --place "Marin County" --tile-deg 0.25

Large regions are split into a grid of tiles, queried one at a time with a pause
between them, and de-duplicated by OSM id on the way back. That keeps any single
Overpass request inside the server's memory/time budget, which is the actual reason
statewide queries fail — not rate limiting.

What comes out: `<prefix>.geojson` (LineString/MultiLineString features, ready for
QGIS, Google Earth, or Mapbox) and `<prefix>.csv` (one row per trail, flat columns
plus the raw tag dict as JSON).

Data is ODbL. This project is personal-use and undistributed, so share-alike does
not trigger; attribute OpenStreetMap contributors if that ever changes.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

USER_AGENT = os.environ.get(
    "OSM_USER_AGENT",
    "Backcountry/1.0 (personal trail research; +https://github.com/)",
)

# Rotated on failure, in preference order. de is canonical but the most contended;
# kumi is fast and tolerant; private.coffee is the backstop. overpass.osm.jp is
# deliberately absent — it serves a certificate that does not match its hostname, so
# every attempt against it burns a retry slot on an SSL error.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Overpass counts server-side seconds, not wall clock; 180 is the usual ceiling
# before a mirror starts refusing the query outright.
QUERY_TIMEOUT = int(os.environ.get("OVERPASS_TIMEOUT", "180"))

# Nominatim's published limit is 1 req/s and they mean it.
NOMINATIM_SLEEP = 1.1

# A tile this size holds roughly a city's worth of paths without tripping the
# server's memory guard. Dense alpine areas may still need --tile-deg 0.25.
DEFAULT_TILE_DEG = 0.5

# Way-level highway values that carry foot traffic. `track` and `bridleway` are in
# because a large share of US forest-service trail mileage is tagged that way;
# `steps` is in because it is frequently the connecting piece of an urban trail.
FOOT_HIGHWAYS = ["path", "footway", "track", "bridleway", "steps", "cycleway"]

EARTH_RADIUS_KM = 6371.0088

# ~11 m at the equator. Used to decide whether two vertices are "the same place"
# when detecting closed loops and doubled-back segments.
SNAP_DEG = 1e-4

# Endpoint gap below which two runs are treated as the same continuous trail.
# `trail_graph.py` uses 35 m for the same job; the reasoning there applies here.
STITCH_METRES = float(os.environ.get("OSM_STITCH_METRES", "35"))

# Relation member roles that are part of the through-route. Anything else
# (alternative, approach, excursion) splices variants in and inflates length.
USABLE_ROLES = {"", "main", "forward", "backward"}


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lon, lat) pairs, in kilometres."""
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def line_length_km(coords: Sequence[tuple[float, float]]) -> float:
    return sum(haversine_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def _snap(pt: tuple[float, float]) -> tuple[int, int]:
    return (round(pt[0] / SNAP_DEG), round(pt[1] / SNAP_DEG))


def chain_lines(
    lines: list[list[tuple[float, float]]],
    stitch_m: float = STITCH_METRES,
) -> list[list[tuple[float, float]]]:
    """Join lines that share an endpoint into as few continuous runs as possible.

    Relation members arrive in editing order, not walking order, and some are
    digitised against the direction of travel. Greedily extending a run from both
    ends, flipping candidates as needed, recovers the through-line; whatever cannot
    be attached comes back as its own run and marks the route as disconnected.

    Exact endpoint matching alone under-joins. Members contributed by different
    mappers meet at coordinates that are close but not identical, so a second pass
    stitches runs whose endpoints fall within `stitch_m`. Big Basin's Skyline To The
    Sea relation is the case that forced this: two of its runs sit 23 m apart, while
    its genuine gaps are 600-2800 m, so a tolerance in the tens of metres separates
    sloppy mapping from a route that is actually incomplete.
    """
    remaining = [list(ln) for ln in lines if len(ln) >= 2]
    runs: list[list[tuple[float, float]]] = []

    while remaining:
        run = remaining.pop(0)
        extended = True
        while extended:
            extended = False
            head, tail = _snap(run[0]), _snap(run[-1])
            for i, cand in enumerate(remaining):
                c0, c1 = _snap(cand[0]), _snap(cand[-1])
                if c0 == tail:
                    run.extend(cand[1:])
                elif c1 == tail:
                    run.extend(list(reversed(cand))[1:])
                elif c1 == head:
                    run[:0] = cand[:-1]
                elif c0 == head:
                    run[:0] = list(reversed(cand))[:-1]
                else:
                    continue
                remaining.pop(i)
                extended = True
                break
        runs.append(run)

    return _stitch(runs, stitch_m) if stitch_m > 0 else runs


def _stitch(
    runs: list[list[tuple[float, float]]], stitch_m: float
) -> list[list[tuple[float, float]]]:
    """Join runs whose endpoints are within stitch_m, closest pair first."""
    tol_km = stitch_m / 1000.0
    runs = [list(r) for r in runs]

    merged = True
    while merged and len(runs) > 1:
        merged = False
        best: tuple[float, int, int, bool, bool] | None = None
        for i in range(len(runs)):
            for j in range(i + 1, len(runs)):
                for a_end in (False, True):  # False = head of i, True = tail of i
                    for b_end in (False, True):
                        pa = runs[i][-1] if a_end else runs[i][0]
                        pb = runs[j][-1] if b_end else runs[j][0]
                        d = haversine_km(pa, pb)
                        if d <= tol_km and (best is None or d < best[0]):
                            best = (d, i, j, a_end, b_end)
        if best is not None:
            _, i, j, a_end, b_end = best
            a, b = runs[i], runs.pop(j)
            if not a_end:
                a.reverse()
            if b_end:
                b.reverse()
            runs[i] = a + b
            merged = True

    return runs


def largest_gap_m(runs: list[list[tuple[float, float]]]) -> float:
    """Nearest-neighbour endpoint gap for the worst-connected run, in metres.

    Zero for a single continuous line. For a relation that came back in pieces this
    says whether the pieces are a mapping artefact or a genuinely missing stretch.
    """
    if len(runs) < 2:
        return 0.0
    worst = 0.0
    for i, run in enumerate(runs):
        nearest = min(
            min(
                haversine_km(pa, pb)
                for pa in (run[0], run[-1])
                for pb in (other[0], other[-1])
            )
            for j, other in enumerate(runs)
            if j != i
        )
        worst = max(worst, nearest)
    return round(worst * 1000, 1)


def classify_route_type(runs: list[list[tuple[float, float]]]) -> str:
    """loop | out-and-back | lollipop | point-to-point | network

    Out-and-back is not a property OSM records — it is a property of how the trail
    is *walked*. It is inferred here from geometry: a route that traverses the same
    ground twice reads as doubled segments once vertices are snapped to a grid. A
    route whose ends meet without that doubling is a true loop; one that does both
    is a lollipop.
    """
    if not runs:
        return "unknown"
    if len(runs) > 1:
        return "network"

    coords = runs[0]
    if len(coords) < 2:
        return "unknown"

    total = 0.0
    doubled = 0.0
    seen: dict[frozenset[tuple[int, int]], int] = {}
    for i in range(len(coords) - 1):
        seg = haversine_km(coords[i], coords[i + 1])
        total += seg
        key = frozenset((_snap(coords[i]), _snap(coords[i + 1])))
        if len(key) < 2:  # zero-length after snapping
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            doubled += seg * 2
        elif seen[key] > 2:
            doubled += seg

    dup_frac = doubled / total if total else 0.0
    closed = haversine_km(coords[0], coords[-1]) < 0.05  # 50 m

    # An out-and-back returns to its start, so "closed" alone cannot separate it
    # from a loop; the amount of doubled ground is what distinguishes them. A
    # lollipop is the middle case: a stem walked twice, a loop walked once.
    if dup_frac >= 0.85:
        return "out-and-back"
    if closed:
        return "lollipop" if dup_frac >= 0.15 else "loop"
    if dup_frac >= 0.35:
        return "out-and-back"
    return "point-to-point"


def bounds_of(runs: list[list[tuple[float, float]]]) -> dict[str, float]:
    lons = [c[0] for r in runs for c in r]
    lats = [c[1] for r in runs for c in r]
    if not lons:
        return {}
    return {
        "min_lon": min(lons),
        "min_lat": min(lats),
        "max_lon": max(lons),
        "max_lat": max(lats),
        "center_lon": sum(lons) / len(lons),
        "center_lat": sum(lats) / len(lats),
    }


# ---------------------------------------------------------------------------
# region resolution
# ---------------------------------------------------------------------------


class BBox(tuple):
    """(south, west, north, east) in degrees."""

    def __new__(cls, south: float, west: float, north: float, east: float) -> "BBox":
        return super().__new__(cls, (south, west, north, east))

    @property
    def area_deg2(self) -> float:
        return (self[2] - self[0]) * (self[3] - self[1])

    def as_overpass(self) -> str:
        return f"{self[0]:.6f},{self[1]:.6f},{self[2]:.6f},{self[3]:.6f}"

    def tiles(self, tile_deg: float) -> list["BBox"]:
        """Split into a grid of tiles no larger than tile_deg on a side.

        The epsilon is not cosmetic. Degree spans are rarely exact in binary — a
        0.4 deg span comes out of the subtraction as 0.40000000000000568, and
        dividing by a 0.2 deg tile gives 2.0000000000000284, which `ceil` rounds up
        to a third column of tiles that covers nothing. Every such tile is a real
        Overpass request, so on a large region the noise buys a spurious row *and*
        column of queries against a rate-limited public API.
        """
        south, west, north, east = self
        rows = max(1, math.ceil((north - south) / tile_deg - 1e-9))
        cols = max(1, math.ceil((east - west) / tile_deg - 1e-9))
        dlat = (north - south) / rows
        dlon = (east - west) / cols
        out = []
        for r in range(rows):
            for c in range(cols):
                out.append(
                    BBox(
                        south + r * dlat,
                        west + c * dlon,
                        south + (r + 1) * dlat,
                        west + (c + 1) * dlon,
                    )
                )
        return out


def parse_bbox(text: str) -> BBox:
    parts = [p.strip() for p in text.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("--bbox needs four numbers: south,west,north,east")
    south, west, north, east = (float(p) for p in parts)
    if south > north:
        south, north = north, south
    if west > east:
        west, east = east, west
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("latitudes must be within -90..90")
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("longitudes must be within -180..180")
    return BBox(south, west, north, east)


def geocode_place(place: str, session: "Fetcher") -> tuple[BBox, str]:
    """Resolve a free-text place name to a bounding box via Nominatim."""
    params = urllib.parse.urlencode(
        {"q": place, "format": "jsonv2", "limit": 1, "polygon_geojson": 0}
    )
    body = session.get(f"{NOMINATIM_URL}?{params}")
    time.sleep(NOMINATIM_SLEEP)
    try:
        results = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Nominatim returned non-JSON for {place!r}") from exc
    if not results:
        raise RuntimeError(f"No place found for {place!r}")

    hit = results[0]
    south, north, west, east = (float(v) for v in hit["boundingbox"])
    return BBox(south, west, north, east), hit.get("display_name", place)


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


class Fetcher:
    """HTTP via urllib. Overpass is a public API; a plain client is the polite one."""

    name = "http"

    def __init__(self, timeout: int = 300) -> None:
        self.timeout = timeout

    def get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def post(self, url: str, data: str) -> str:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode({"data": data}).encode(),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def close(self) -> None:
        pass


class BrowserFetcher(Fetcher):
    """Issue the same requests from inside a real browser context.

    Some mirrors refuse plain clients from datacenter ranges. Rather than dress up
    urllib with fake headers — which is what actually gets a client banned — this
    navigates to the mirror's own origin and calls `fetch` from that page, so the
    request carries a genuine browser's TLS fingerprint, header order, and origin.

    `camoufox` is the hardened Firefox build; `playwright` drives stock Chromium.
    Both are imported lazily so the module runs with neither installed.
    """

    def __init__(
        self,
        engine: str = "playwright",
        timeout: int = 300,
        headless: bool = True,
        slow_mo: int = 0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.name = engine
        self.engine = engine
        self.headless = headless
        self.slow_mo = slow_mo
        self._ctx = None
        self._browser = None
        self._page = None
        self._origin: str | None = None
        self._start()

    def _start(self) -> None:
        # Both engines fail two ways — the package missing, and the package present
        # with its browser binary not yet downloaded. The second is the common one
        # and its native error does not say which command fixes it.
        if self.engine == "camoufox":
            hint = "pip install 'camoufox[geoip]' && python -m camoufox fetch"
            try:
                from camoufox.sync_api import Camoufox

                self._ctx = Camoufox(headless=self.headless, slow_mo=self.slow_mo)
                self._browser = self._ctx.__enter__()
                self._page = self._browser.new_page()
            except Exception as exc:
                raise RuntimeError(
                    f"camoufox transport unavailable ({exc.__class__.__name__}: "
                    f"{exc}).\n  Fix: {hint}\n  Or use --transport http."
                ) from exc
        else:
            hint = "pip install playwright && playwright install chromium"
            try:
                from playwright.sync_api import sync_playwright

                self._ctx = sync_playwright()
                pw = self._ctx.__enter__()
                self._browser = pw.chromium.launch(
                    headless=self.headless, slow_mo=self.slow_mo
                )
                context = self._browser.new_context(user_agent=USER_AGENT)
                self._page = context.new_page()
            except Exception as exc:
                raise RuntimeError(
                    f"playwright transport unavailable ({exc.__class__.__name__}: "
                    f"{exc}).\n  Fix: {hint}\n  Or use --transport http."
                ) from exc

    def _ensure_origin(self, url: str) -> None:
        """Same-origin navigation first, so the fetch is not a CORS preflight."""
        origin = urllib.parse.urlsplit(url)._replace(path="/", query="", fragment="")
        origin_url = urllib.parse.urlunsplit(origin)
        if self._origin != origin_url:
            # "load", not "domcontentloaded": a headed window keeps loading
            # subresources and can still redirect afterwards, and an evaluate that
            # starts during that navigation dies with "Execution context was
            # destroyed". Headless finishes fast enough to hide the race.
            self._page.goto(origin_url, wait_until="load")
            self._origin = origin_url
            if not self.headless:
                self._install_overlay()

    def _install_overlay(self) -> None:
        """Draw a status panel into the page.

        Only useful in headed mode, and only because the queries themselves are
        invisible: they go out through `fetch`, so without this the window just
        shows the mirror's home page while the run happens behind it.
        """
        self._page.evaluate(
            """() => {
                if (document.getElementById('bc-status')) return;
                const el = document.createElement('div');
                el.id = 'bc-status';
                el.style.cssText = [
                    'position:fixed', 'top:0', 'right:0', 'z-index:2147483647',
                    'width:440px', 'max-height:100vh', 'overflow:auto',
                    'background:rgba(12,18,14,.94)', 'color:#b6f2c0',
                    'font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace',
                    'padding:12px 14px', 'border-left:3px solid #2f9e59',
                    'box-shadow:-4px 0 18px rgba(0,0,0,.4)', 'white-space:pre-wrap',
                ].join(';');
                el.textContent = 'Backcountry / Overpass';
                document.body.appendChild(el);
            }"""
        )

    def _log(self, line: str, colour: str = "#b6f2c0") -> None:
        if self.headless:
            return
        try:
            self._page.evaluate(
                """([line, colour]) => {
                    const el = document.getElementById('bc-status');
                    if (!el) return;
                    const row = document.createElement('div');
                    row.style.color = colour;
                    row.textContent = line;
                    el.appendChild(row);
                    el.scrollTop = el.scrollHeight;
                }""",
                [line, colour],
            )
        except Exception:
            pass  # the overlay is decoration; never let it break a run

    def _eval(self, script: str, arg: Any) -> Any:
        """Evaluate in the page, re-navigating once if the context was torn down.

        The origin page is a real site that may redirect or reload under us; that
        invalidates the execution context and is retryable, not fatal.
        """
        try:
            return self._page.evaluate(script, arg)
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc):
                raise
            self._origin = None
            self._page.wait_for_load_state("load")
            return self._page.evaluate(script, arg)

    def get(self, url: str) -> str:
        self._ensure_origin(url)
        return self._eval(
            """async (url) => {
                const r = await fetch(url, {headers: {'Accept': 'application/json'}});
                return await r.text();
            }""",
            url,
        )

    def post(self, url: str, data: str) -> str:
        self._ensure_origin(url)
        bbox = re.search(r"\(([-\d.]+,[-\d.]+,[-\d.]+,[-\d.]+)\)", data)
        host = urllib.parse.urlsplit(url).netloc
        self._log(f"-> {host}\n   {bbox.group(1) if bbox else 'query'}")
        body = self._eval(
            """async ([url, body]) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: new URLSearchParams({data: body}).toString(),
                });
                if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + await r.text());
                return await r.text();
            }""",
            [url, data],
        )
        try:
            n = len(json.loads(body).get("elements", []))
            self._log(f"   {n} elements", "#7fd6ff")
        except Exception:
            self._log("   non-JSON response", "#ffb86b")
        return body

    def close(self) -> None:
        try:
            if not self.headless and self._page is not None:
                self._log("done - closing in 5s", "#ffd479")
                time.sleep(5)
            if self._browser is not None:
                self._browser.close()
            if self._ctx is not None:
                self._ctx.__exit__(None, None, None)
        except Exception:
            pass


def make_fetcher(
    transport: str, timeout: int, headed: bool = False, slow_mo: int = 0
) -> Fetcher:
    if transport in ("playwright", "camoufox"):
        return BrowserFetcher(
            engine=transport, timeout=timeout, headless=not headed, slow_mo=slow_mo
        )
    if headed:
        print(
            "--headed only applies to --transport playwright/camoufox; ignoring",
            file=sys.stderr,
        )
    return Fetcher(timeout=timeout)


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------


def build_query(
    bbox: BBox,
    *,
    include_ways: bool = True,
    include_relations: bool = True,
    named_only: bool = False,
) -> str:
    """Overpass QL for every foot-usable way and hiking route in the bbox.

    `out geom` returns coordinates inline, including per-member geometry for
    relations, which avoids a second round trip to resolve node references. It must
    not be paired with the `tags` output mode: `tags` suppresses the member list, so
    every relation comes back geometry-less. `geom` alone already implies body mode,
    which carries the tags anyway.
    """
    box = bbox.as_overpass()
    name_filter = '["name"]' if named_only else ""
    clauses: list[str] = []

    if include_ways:
        highway_re = "|".join(FOOT_HIGHWAYS)
        clauses.append(f'  way["highway"~"^({highway_re})$"]{name_filter}({box});')
        # sac_scale is the hiking-difficulty scale; anything carrying it is a trail
        # regardless of how the highway tag was filled in.
        clauses.append(f'  way["sac_scale"]({box});')
        clauses.append(f'  way["highway"]["trail_visibility"]({box});')

    if include_relations:
        clauses.append(f'  relation["route"~"^(hiking|foot)$"]({box});')

    body = "\n".join(clauses)
    return f"[out:json][timeout:{QUERY_TIMEOUT}];\n(\n{body}\n);\nout geom qt;"


class OverpassError(RuntimeError):
    pass


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OverpassError)):
        return True
    # Browser transports surface HTTP status inside the message string.
    return bool(re.search(r"HTTP (429|50[0-4])", str(exc)))


def overpass_query(
    query: str,
    fetcher: Fetcher,
    *,
    max_retries: int = 5,
    pause: float = 1.5,
    mirrors: Sequence[str] = OVERPASS_MIRRORS,
    verbose: bool = True,
) -> dict[str, Any]:
    """POST a query, rotating mirrors and backing off exponentially on failure.

    Overpass signals overload three different ways — an HTTP 429, an HTTP 200 whose
    body is an HTML error page, and an HTTP 200 whose JSON carries a `remark` about
    memory or timeout. All three are treated as retryable here; only a genuine
    syntax error (400 with a parse message) stops the run.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        url = mirrors[attempt % len(mirrors)]
        try:
            body = fetcher.post(url, query)
        except Exception as exc:  # noqa: BLE001 - transport-agnostic by design
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 400:
                detail = _extract_html_error(exc.read().decode("utf-8", "replace"))
                raise OverpassError(f"Overpass rejected the query: {detail}") from exc
            if not _is_retryable(exc):
                raise
            last_exc = exc
        else:
            stripped = body.lstrip()
            if stripped.startswith("<"):
                last_exc = OverpassError(_extract_html_error(body))
            else:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as exc:
                    last_exc = OverpassError(f"non-JSON response from {url}")
                else:
                    remark = data.get("remark", "")
                    if "error" in remark.lower():
                        last_exc = OverpassError(remark.strip())
                    else:
                        if remark and verbose:
                            print(f"    note: {remark.strip()}", file=sys.stderr)
                        return data

        # Exponential backoff with jitter, so parallel runs do not resynchronise.
        delay = min(60.0, pause * (2**attempt)) + random.uniform(0, pause)
        if verbose:
            host = urllib.parse.urlsplit(url).netloc
            print(
                f"    {host} failed ({last_exc}); retrying in {delay:.1f}s "
                f"[{attempt + 1}/{max_retries}]",
                file=sys.stderr,
            )
        time.sleep(delay)

    raise OverpassError(f"all {max_retries} attempts failed; last error: {last_exc}")


def _extract_html_error(body: str) -> str:
    """Pull the human-readable line out of an Overpass HTML error page."""
    msgs = re.findall(r"<p><strong[^>]*>Error</strong>:\s*(.*?)</p>", body, re.S)
    if msgs:
        return html.unescape(re.sub(r"<[^>]+>", "", msgs[0])).strip()[:300]
    return "HTML error page returned"


def fetch_region(
    bbox: BBox,
    fetcher: Fetcher,
    *,
    tile_deg: float,
    max_tiles: int,
    pause: float,
    max_retries: int,
    include_ways: bool,
    include_relations: bool,
    named_only: bool,
    cache_dir: Path | None,
    verbose: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Query the region tile by tile, de-duplicating elements by (type, id).

    Returns the elements and the bboxes of any tiles that could not be fetched, so
    the caller can record in the output that coverage is partial.
    """
    tiles = bbox.tiles(tile_deg)
    if len(tiles) > max_tiles:
        raise SystemExit(
            f"Region splits into {len(tiles)} tiles at --tile-deg {tile_deg}, over the "
            f"--max-tiles limit of {max_tiles}. Raise the tile size, raise the limit, "
            f"or query a smaller region."
        )

    if verbose:
        print(
            f"Region {bbox.as_overpass()}  "
            f"({bbox.area_deg2:.2f} deg^2, {len(tiles)} tile(s))"
        )

    elements: dict[tuple[str, int], dict[str, Any]] = {}

    def run_tiles(batch: list[BBox], label: str) -> list[BBox]:
        failed: list[BBox] = []
        for i, tile in enumerate(batch, 1):
            query = build_query(
                tile,
                include_ways=include_ways,
                include_relations=include_relations,
                named_only=named_only,
            )
            if verbose:
                print(
                    f"  {label}[{i}/{len(batch)}] {tile.as_overpass()}",
                    end="",
                    flush=True,
                )

            try:
                data = overpass_query(
                    query,
                    fetcher,
                    max_retries=max_retries,
                    pause=pause,
                    verbose=verbose,
                )
            except OverpassError as exc:
                failed.append(tile)
                print(f"\n    tile failed: {exc}", file=sys.stderr)
                continue

            found = data.get("elements", [])
            for el in found:
                key = (el.get("type", ""), int(el.get("id", 0)))
                # A relation carries its members' geometry, so prefer it over the
                # bare way when the same id shows up in two tiles.
                if key not in elements or len(str(el)) > len(str(elements[key])):
                    elements[key] = el

            if cache_dir is not None:
                cache_dir.mkdir(parents=True, exist_ok=True)
                tile_name = tile.as_overpass().replace(",", "_")
                (cache_dir / f"tile_{tile_name}.json").write_text(json.dumps(data))

            if verbose:
                print(f"  -> {len(found)} elements ({len(elements)} unique)")

            if i < len(batch):
                time.sleep(pause)
        return failed

    failed = run_tiles(tiles, "")

    # A tile lost to a busy mirror is missing data, not an error, and the mirrors
    # that were saturated at the start of a long run are usually free by the end.
    # One sweep at the end recovers most of them; whatever still fails is reported
    # loudly, because silently short output is the worst outcome here.
    if failed:
        print(
            f"  retrying {len(failed)} failed tile(s) after a {pause * 10:.0f}s pause",
            file=sys.stderr,
        )
        time.sleep(pause * 10)
        failed = run_tiles(failed, "retry ")

    if failed:
        print(
            f"\n  WARNING: {len(failed)} of {len(tiles)} tiles could not be fetched. "
            f"Output is INCOMPLETE for:\n    "
            + "\n    ".join(t.as_overpass() for t in failed),
            file=sys.stderr,
        )

    return list(elements.values()), [t.as_overpass() for t in failed]


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

# Tags carried through to flat CSV columns. Everything else survives in tags_json.
FLAT_TAGS = [
    "name",
    "alt_name",
    "ref",
    "operator",
    "network",
    "surface",
    "tracktype",
    "smoothness",
    "sac_scale",
    "trail_visibility",
    "mtb:scale",
    "difficulty",
    "incline",
    "width",
    "ascent",
    "descent",
    "distance",
    "ele",
    "access",
    "foot",
    "bicycle",
    "horse",
    "dog",
    "wheelchair",
    "fee",
    "seasonal",
    "highway",
    "route",
    "symbol",
    "osmc:symbol",
    "website",
    "wikidata",
    "wikipedia",
    "description",
]

CSV_COLUMNS = [
    "osm_type",
    "osm_id",
    "name",
    "alt_name",
    "ref",
    "length_mi",
    "length_km",
    "route_type",
    "segments",
    "max_gap_m",
    "surface",
    "tracktype",
    "smoothness",
    "sac_scale",
    "sac_scale_label",
    "difficulty",
    "trail_visibility",
    "mtb:scale",
    "incline",
    "ascent",
    "descent",
    "distance",
    "ele",
    "highway",
    "route",
    "network",
    "operator",
    "access",
    "foot",
    "bicycle",
    "horse",
    "dog",
    "wheelchair",
    "fee",
    "seasonal",
    "symbol",
    "osmc:symbol",
    "website",
    "wikidata",
    "wikipedia",
    "description",
    "center_lat",
    "center_lon",
    "min_lat",
    "min_lon",
    "max_lat",
    "max_lon",
    "osm_url",
    "tags_json",
]

# OSM's hiking scale, spelled out. The raw values are opaque to anyone who has not
# read the wiki, and the whole point of the CSV is to be readable.
SAC_LABELS = {
    "hiking": "T1 - easy, well-cleared trail",
    "mountain_hiking": "T2 - continuous trail, some exposure",
    "demanding_mountain_hiking": "T3 - exposed sections, sure-footedness needed",
    "alpine_hiking": "T4 - trail may be absent, hands occasionally needed",
    "demanding_alpine_hiking": "T5 - scrambling, exposed",
    "difficult_alpine_hiking": "T6 - climbing, glacier travel",
}


def _geometry_of(el: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Extract [(lon, lat), ...] runs from an `out geom` element."""
    if el.get("type") == "way":
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
        return [pts] if len(pts) >= 2 else []

    lines: list[list[tuple[float, float]]] = []
    for member in el.get("members") or []:
        if member.get("type") != "way":
            continue
        if (member.get("role") or "") not in USABLE_ROLES:
            continue
        pts = [(p["lon"], p["lat"]) for p in member.get("geometry") or [] if p]
        if len(pts) >= 2:
            lines.append(pts)
    return chain_lines(lines)


def parse_element(el: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Overpass element into a flat trail record, or None if unusable."""
    runs = _geometry_of(el)
    if not runs:
        return None

    tags = el.get("tags") or {}
    length_km = sum(line_length_km(r) for r in runs)
    osm_type = el.get("type", "")
    osm_id = int(el.get("id", 0))

    rec: dict[str, Any] = {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "length_km": round(length_km, 4),
        "length_mi": round(length_km * 0.621371, 4),
        "route_type": classify_route_type(runs),
        "segments": len(runs),
        "max_gap_m": largest_gap_m(runs),
        "osm_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
    }
    for tag in FLAT_TAGS:
        value = tags.get(tag)
        if value is not None:
            rec[tag] = value

    rec["sac_scale_label"] = SAC_LABELS.get(tags.get("sac_scale", ""), "")
    rec.update({k: round(v, 6) for k, v in bounds_of(runs).items()})
    rec["tags_json"] = json.dumps(tags, sort_keys=True, ensure_ascii=False)
    rec["_runs"] = runs
    return rec


def to_feature(rec: dict[str, Any]) -> dict[str, Any]:
    runs = rec["_runs"]
    props = {k: v for k, v in rec.items() if k != "_runs"}
    if len(runs) == 1:
        geometry = {"type": "LineString", "coordinates": [list(c) for c in runs[0]]}
    else:
        geometry = {
            "type": "MultiLineString",
            "coordinates": [[list(c) for c in r] for r in runs],
        }
    return {
        "type": "Feature",
        "id": f"{rec['osm_type']}/{rec['osm_id']}",
        "properties": props,
        "geometry": geometry,
    }


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def write_geojson(
    records: list[dict[str, Any]],
    path: Path,
    source: str,
    incomplete_tiles: Sequence[str] = (),
) -> None:
    fc = {
        "type": "FeatureCollection",
        "metadata": {
            "source": "OpenStreetMap via Overpass API",
            "license": "ODbL 1.0 - (c) OpenStreetMap contributors",
            "region": source,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(records),
            "incomplete_tiles": list(incomplete_tiles),
        },
        "features": [to_feature(r) for r in records],
    }
    path.write_text(json.dumps(fc, ensure_ascii=False))


def write_csv(records: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in CSV_COLUMNS})


def summarise(records: list[dict[str, Any]]) -> str:
    if not records:
        return "no trails found"
    total_mi = sum(r["length_mi"] for r in records)
    named = sum(1 for r in records if r.get("name"))
    by_type: dict[str, int] = {}
    for r in records:
        by_type[r["route_type"]] = by_type.get(r["route_type"], 0) + 1
    surfaces: dict[str, int] = {}
    for r in records:
        surfaces[r.get("surface", "untagged")] = (
            surfaces.get(r.get("surface", "untagged"), 0) + 1
        )
    top_surface = sorted(surfaces.items(), key=lambda kv: -kv[1])[:5]
    lines = [
        f"{len(records)} trails, {total_mi:,.1f} mi total, {named} named "
        f"({named / len(records):.0%})",
        "  route types: "
        + ", ".join(
            f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
        ),
        "  surfaces:    " + ", ".join(f"{k} {v}" for k, v in top_surface),
    ]
    sac = sum(1 for r in records if r.get("sac_scale"))
    if sac:
        lines.append(f"  {sac} carry a sac_scale difficulty rating")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="magic.py",
        description="Extract hiking trails from OpenStreetMap for a region.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python magic.py --place 'Boulder, Colorado'\n"
            "  python magic.py --bbox 39.95,-105.35,40.10,-105.20 --prefix boulder\n"
            "  python magic.py --place 'Marin County, CA' --tile-deg 0.25\n"
            "  python magic.py --place 'Zion National Park' --transport camoufox\n"
        ),
    )
    region = p.add_mutually_exclusive_group(required=True)
    region.add_argument("--place", help="free-text place name, resolved via Nominatim")
    # A bbox starting with a negative latitude looks like an option to argparse,
    # hence the =-form in the help text.
    region.add_argument(
        "--bbox",
        help="south,west,north,east in decimal degrees "
        "(use --bbox=-33.9,151.1,-33.7,151.3 when south is negative)",
    )

    p.add_argument("--out-dir", default="data/osm_trails", help="output directory")
    p.add_argument("--prefix", help="output filename stem (default: slug of region)")
    p.add_argument(
        "--transport",
        choices=["http", "playwright", "camoufox"],
        default="http",
        help="how to reach Overpass; browser transports are a fallback for blocks",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (browser transports only). The queries go out "
        "via fetch, so a status overlay is drawn into the page to make the run "
        "visible; pair with --slow-mo to slow it down",
    )
    p.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="milliseconds to pause between browser operations, for watching",
    )
    p.add_argument("--tile-deg", type=float, default=DEFAULT_TILE_DEG)
    p.add_argument("--max-tiles", type=int, default=400)
    p.add_argument("--pause", type=float, default=1.5, help="seconds between requests")
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--timeout", type=int, default=300, help="client socket timeout")
    p.add_argument(
        "--min-length",
        type=float,
        default=0.0,
        help="drop trails shorter than this many miles",
    )
    p.add_argument("--named-only", action="store_true", help="require a name tag")
    p.add_argument("--no-ways", action="store_true", help="skip individual paths")
    p.add_argument("--no-relations", action="store_true", help="skip hiking routes")
    p.add_argument("--limit", type=int, help="keep only the N longest trails")
    p.add_argument("--cache-dir", help="save raw Overpass responses here")
    p.add_argument("--dry-run", action="store_true", help="print the query and exit")
    p.add_argument("--quiet", action="store_true")
    return p


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] or "region"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = not args.quiet

    if args.no_ways and args.no_relations:
        print("--no-ways and --no-relations leaves nothing to query", file=sys.stderr)
        return 2

    try:
        fetcher = make_fetcher(
            args.transport, args.timeout, headed=args.headed, slow_mo=args.slow_mo
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        if args.bbox:
            bbox = parse_bbox(args.bbox)
            label = f"bbox {bbox.as_overpass()}"
        else:
            if verbose:
                print(f"Geocoding {args.place!r}...")
            bbox, label = geocode_place(args.place, fetcher)
            if verbose:
                print(f"  -> {label}")

        if args.dry_run:
            print(
                build_query(
                    bbox,
                    include_ways=not args.no_ways,
                    include_relations=not args.no_relations,
                    named_only=args.named_only,
                )
            )
            return 0

        elements, incomplete = fetch_region(
            bbox,
            fetcher,
            tile_deg=args.tile_deg,
            max_tiles=args.max_tiles,
            pause=args.pause,
            max_retries=args.max_retries,
            include_ways=not args.no_ways,
            include_relations=not args.no_relations,
            named_only=args.named_only,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            verbose=verbose,
        )
    finally:
        fetcher.close()

    records = [r for r in (parse_element(el) for el in elements) if r]
    if args.min_length:
        records = [r for r in records if r["length_mi"] >= args.min_length]
    if args.named_only:
        records = [r for r in records if r.get("name")]
    records.sort(key=lambda r: -r["length_mi"])
    if args.limit:
        records = records[: args.limit]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix or slugify(args.place or bbox.as_overpass())
    geojson_path = out_dir / f"{prefix}.geojson"
    csv_path = out_dir / f"{prefix}.csv"

    write_geojson(records, geojson_path, label, incomplete)
    write_csv(records, csv_path)

    if verbose:
        print()
        print(summarise(records))
        if incomplete:
            print(f"  INCOMPLETE: {len(incomplete)} tile(s) failed; rerun to fill in")
        print(f"  wrote {geojson_path}")
        print(f"  wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
