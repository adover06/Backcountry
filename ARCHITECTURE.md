# Architecture

OpenTrails is a California trail discovery app: **13,786 trails**, map-first search,
and a routing graph that composes trail segments into whole hikes.

For where the data comes from, see [`SOURCES.md`](SOURCES.md). For current state and
what to work on next, see [`HANDOFF.md`](HANDOFF.md).

---

## 1. The shape of the problem

Two facts drive nearly every design decision here.

**No single free trail dataset is good enough.** Each source holds one piece: USFS
has geometry and allowed-use but no national parks; NPS has parks but no forests;
neither has regional or county land, where most Californians actually walk; nobody
has elevation. The product is the **conflation**. That is also why "there is no good
free trail data" feels true — the pieces exist, but nobody hands you them joined.

**No dataset models a hike.** Every source publishes trail *segments*, because
segments are what agencies administer and budget against. "Half Dome is a 14-mile
hike from Happy Isles" is a visitor-facing composition no land manager has an
operational reason to record — which is why OSM also models Half Dome Trail as
2.00 mi. The absence is structural, so composition has to be **computed**. That is
what the routing graph is for.

A third fact governs how everything reports itself:

> **Missing is never rendered as a value.** Every check reports ok/unavailable. The
> risk engine has an `incomplete` state that cannot render green. A gain filter
> excludes a trail whose elevation is unknown rather than treating it as flat.
> Unknown gain returns `None`, never `0`. Most bugs found in this codebase have been
> violations of this rule.

---

## 2. System overview

```
   ┌─────────────── BUILD (offline, minutes to hours) ───────────────┐
   │  agency GeoJSON ─┐                                              │
   │  ArcGIS layers  ─┼─→ pipeline/  ─→ trails_index.json  (28 MB)   │
   │  Overpass       ─┤               ─→ trails_geom.json  (179 MB)  │
   │  DEM tiles      ─┘               ─→ trails_geom.sqlite (233 MB) │
   └─────────────────────────────────────────────────────────────────┘
                                  │  bind-mounted, read-only
   ┌──────────────── SERVE (request time, milliseconds) ─────────────┐
   │  FastAPI ─ discovery_api ─ planner/discover  (index in memory)  │
   │          ├ graph_service ─ pipeline/trail_graph (lazy, 1.8 GB)  │
   │          └ planner/checks ─ live weather / AQI / fire / snow    │
   │                                                                 │
   │  nginx ─ SPA (React + Mapbox) + /api proxy, gzip                │
   └─────────────────────────────────────────────────────────────────┘
                                  │
                          PostgreSQL — users, trips, saved trails
```

The split is deliberate: **trail data is a build artefact, user data is a database.**
Trail data is read-only, regenerated wholesale, and versioned with the index;
Postgres holds the things that are genuinely relational and mutable. Putting the
index in Postgres would make every rebuild a data migration.

---

## 3. The build pipeline

`python -m pipeline.build_index` — stages are skippable and resumable
(`--resume`, `--skip-elevation`, `--skip-osm`, …).

| stage | module | what it does |
|---|---|---|
| `normalize` | `normalize.py` | 12,315 USFS segments → 8,162 trails, grouped by `TRAIL_CN` |
| `nps` | `nps.py` | park trails + SEKI fallback |
| `osm-routes` | `osm_routes.py` | long-distance `route=hiking` relations ≥ 5 mi |
| `osm-trails` | `osm_trails.py` | named OSM ways → trails on non-federal land |
| `elevation` | `elevation.py` | DEM sampling, gain via hysteresis walk |
| `scenery` | `enrich_osm.py` + `gnis.py` | named features joined by proximity |
| `access` | `access.py` + `usfs_rec.py` + `osm_access.py` | trailheads, water, camping |
| `technical` | `technical.py` | `sac_scale` / `trail_visibility` badges |
| `permits` | `permits.py` | Recreation.gov facilities |
| `wilderness` | `wilderness.py` | point-in-polygon land status |
| `markers` | `markers.py` | the point to draw for each trail |

Three sweeps are **not** build stages, because each takes ~1 h against rate-limited
public mirrors and must never hide inside a build:

```bash
python -m pipeline.osm_access     # access points  → data/osm_access_points.json
python -m pipeline.osm_trails     # named ways     → data/osm_ways.json
python -m pipeline.geom_store     # LOD geometry   → data/trails_geom.sqlite
```

Their output is cached per tile and resumes cleanly. `access.py` compares **mtimes**
against the sweep output, so a later sweep invalidates the access cache instead of
being silently ignored — a version check alone was not enough, because the cache gets
written before the sweep exists.

### Ordering constraints that are not obvious

- `markers` must run **after** `access`, because a joined trailhead is the preferred
  marker.
- `osm-trails` must run **before** `elevation`, so new trails get a DEM pass like any
  other, and before access/wilderness so they are enriched identically.
- `wilderness` costs ~265 s over the full index and is the slowest non-network stage.

---

## 4. Data model

### Two files, split deliberately

| file | size | contents |
|---|---|---|
| `trails_index.json` | 28 MB | searchable records, **no geometry** |
| `trails_geom.sqlite` | 233 MB | geometry at 4 levels of detail + elevation profiles |

The index stays small enough to hold in memory and scan in well under a millisecond.
Geometry is fetched per trail, on demand.

`trails_geom.json` (179 MB) is still the build's output format but is no longer on
the read path — it exists to be converted by `geom_store`.

### Coverage of the current index

| field | coverage |
|---|---|
| elevation, marker | 100% |
| activities (allowed use) | 81.1% |
| surface | 78.6% |
| scenery features | 52.3% |
| campground in reach | 28.6% |
| permits (advisory) | 22.3% |
| trailhead | 19.9% |
| drinking water | 13.0% |
| wilderness | 12.7% |

### Points that mean different things

- **`center`** — bounding-box midpoint. Correct for fitting a viewport, and *wrong*
  for drawing: 13.2% sit more than 0.25 mi off their own trail, the worst 4.71 mi.
- **`marker`** — what the map draws. The joined trailhead where one exists (19.9%),
  otherwise the vertex at half the trail's length. Median distance from the trail: 0.

### Activity is partly published, partly derived

`hiking: allowed` is true for **11,150 of the 11,150** trails carrying use data, so
it separates nothing. What separates a trail from a road is whether motors are
allowed. `ACTIVITY_PREDICATES` in `planner/discover.py`:

| value | definition | count |
|---|---|---|
| `hiking` | not known-motorised | 11,184 |
| `backpacking` | not motorised, and (in wilderness **or** ≥ 8 mi) — *derived* | 2,043 |
| `bike` / `horse` | published allowed-use | 5,688 / 2,203 |
| `motorized` | 4WD, ATV, motorcycle or snowmobile allowed | 2,602 |

Silence is not evidence: 18.9% of the index has no allowed-use data, and treating
that as motorised would drop most of the NPS set out of every hiking search.

---

## 5. Serving

### Search and facets

`planner/discover.py` holds the index in memory (~125 MB resident). Each filter is a
**named predicate**, and facet counts are computed over a pool with that filter's own
dimension excluded — so a chip shows "how many results choosing this would give", not
"how many survive the choices already made". Counting facets over the fully-filtered
set makes selecting one filter grey out every other.

Two projections:

- **`_LIST_FIELDS`** — what a result card renders. The full projection shipped 290 KB
  for 60 results, 203 KB of it `nearby`/`permits`/`access` that no card displays.
- **`_PUBLIC_FIELDS`** — the detail projection. It is a **whitelist**: a field missing
  from it never reaches the client no matter what the pipeline computed.

### The map: dots, then lines

| zoom | endpoint | payload (dense viewport) |
|---|---|---|
| < 11 | `/api/discover/dots` | 73 KB — one point per trail, clustered |
| ≥ 11 | `/api/discover/map?detail=z12` | 265 KB gzipped |
| hover | `/api/discover/trail/{id}?detail=z10` | 27 KB |

Dots are the important case. `marker` lives in the search index, so the dots path
**never calls `load_geometry()`** — and dots are fetched once per filter set rather
than per pan, so panning and zooming are pure client work.

Geometry comes from SQLite at a level of detail matched to zoom (`z10` ≈ 55 m ≈ 1.5
screen pixels at that zoom, 28× smaller than full resolution and indistinguishable).

### Performance, measured

| | before | after |
|---|---|---|
| cold page load, total | ~10.4 MB | **1.09 MB** |
| dense viewport | 7.9 MB / 5.47 s | **73 KB / 0.02 s** |
| one trail's geometry | 4.68 s / 948 MB RSS | **7 ms / flat** |
| backend RSS | ~950 MB | **125 MB cold, ~250 MB in use** |

What mattered, in order: **nginx had no gzip configured at all** (the JS bundle
shipped 2.27 MB raw); the list projection; per-pan refetching; and reading the 167 MB
JSON sidecar to answer single-key lookups.

---

## 6. The routing graph

`pipeline/trail_graph.py` — the answer to "no dataset models a hike".

1. Snap every trail vertex to a **4 m** grid. Any snapped point shared by two trails
   is a junction.
2. Split trails at junctions → edges meeting only at nodes.
3. Weight edges by length and directional gain.
4. **Dijkstra** (`shortest_path`) over 214,852 nodes / 441,213 edges.

A hike is then a path: trailhead → destination → back.

### Validated

| route | computed | published |
|---|---|---|
| Half Dome (Happy Isles) | 13.88 mi / 5,245 ft | ~15 mi / 4,800 ft |
| Nevada Fall (Happy Isles) | 5.5 mi / 2,176 ft | ~5.4 mi / 1,900 ft |
| Ryan Mountain | 2.8 mi / 1,054 ft | ~3 mi / 1,050 ft |

Half Dome indexes as a **2.00 mi segment** and composes to a **13.88 mi hike**, which
is the entire point.

### Four things that had to be right, each wrong first

1. **Node identity must be tight (4 m).** Loose snapping fused adjacent switchback
   legs and let the router cut straight up Whitney — 8.6 mi against a true 22.
2. **Tight snapping alone disconnects the network**, because agencies do not share
   coordinates where their trails meet. A separate pass stitches *endpoints* within
   35 m; stitching mid-trail vertices would reintroduce the fusion.
3. **Gain is computed once along the finished path** with the DEM hysteresis
   threshold, never summed per edge. Nodes are metres apart, so per-edge accumulation
   counted DEM noise as climb and reported Whitney at 14,232 ft.
4. **`nearest_node` must size its cell search from `max_miles`.** A hardcoded ±3
   cells spanned 60 m at 20 m snapping but only 12 m at 4 m, so trailheads silently
   resolved to no node and routes returned "no path".

### Legs, and concurrent routes

A composed hike is only legible if you can see *which* trail each part is. Legs are
keyed by trail **name**, not id — Happy Isles to Half Dome alternates between two
records both called "John Muir Trail", which by id is 90 legs and by name is 4.

Long-distance relations are mapped over the same ground as the local trails they
follow, so consecutive edges flip between two records: a 17.8 mi hike came back as
**80 legs alternating every 0.02 mi**. Collapsing consecutive duplicates cannot help
when the sequence is A,B,A,B. Legs shorter than a stride (0.15 mi) are absorbed into
their larger neighbour, shortest first — 80 legs → 8, mileage preserved exactly.

### The cost, and it is real

The graph is built **lazily on first request** and held for the process lifetime:

- **~55 s to build**, during which it **blocks every other request** — it is
  CPU-bound Python in one worker, so the sidebar sits on "Searching…".
- **~1.8 GB resident** once loaded, more than the rest of the process combined.
- Warm queries: **~25 ms**.

`GRAPH_PREWARM=1` moves the stall into container startup. It is **off by default**
because on a 1 GB VPS the memory alone is fatal. Precomputing the graph to disk is
the real fix and is the top open item.

---

## 7. Frontend

`frontend/src/pages/DiscoverPage.jsx` is the product surface: filters and results in
a sidebar, Mapbox map, overlay panels.

- **Dots below zoom 11, lines above.** Dots cluster with counts, size by trail length,
  colour by difficulty, and carry a white ring when the point is a surveyed trailhead
  rather than a computed midpoint.
- **Hover** shows a card built from the dot's own properties — so it renders on the
  first mousemove without waiting — and fetches that trail's line separately.
- **"Plan a hike"** toggles route mode: click a start, click a destination, get a
  composed hike drawn with its legs listed.
- **Responsive** below `md`: the layout stacks (map on top at 42vh, list beneath),
  the header wraps, and overlay panels go full-width with a gutter. This is "not
  squished", not a mobile design — React Native is the honest answer if mobile
  becomes a real target.

Trail ids are **non-numeric strings** (`nps:YOSE:…`), which Mapbox feature-state does
not accept; the invalid expression silently prevented the whole layer from drawing.
Highlighting uses a filtered overlay layer instead.

---

## 8. Deployment

Two compose files, and the difference matters:

| | `docker-compose.local.yml` | `docker-compose.yml` |
|---|---|---|
| backend port | published on 6767 | not published (nginx proxies) |
| `./data` | **bind-mounted** | **not mounted** |

`.dockerignore` deliberately excludes `data/` from the image — the index plus source
is ~330 MB and has no business inside a layer. **`docker-compose.yml` therefore has
no path to an index and 503s every discovery route.** Use the local file:

```bash
docker compose -f docker-compose.local.yml up -d --build
```

App source and the frontend bundle are **baked into images**, so code changes need
`--build`, not a restart. Data is bind-mounted, so an index rebuild needs only a
restart — but `load_index()` caches into a module global, so a restart is required.

nginx gzips (`gzip_proxied any` matters — API responses come back through
`proxy_pass` and are otherwise skipped).

---

## 9. Known limits

- **Segmentation is not a data problem.** Verified across RIDB, NPS, OSM, Hiking
  Project and GPX corpora. Hence the graph.
- **The routing graph is not persisted** — 55 s build, 1.8 GB, blocks other requests.
- **OSM way coverage is Bay Area only.** Other metros need the same sweep.
- **`surface` missing on 21.4%.** Filling it needs way-level matching via a 1.2 GB
  Geofabrik extract — disproportionate so far.
- **Fire filtering is by centroid/points**, not true spatial intersection.
- **Weather and AQI are point lookups**, not route-aware.
- **The conditions cache is process-local** — no persistence, no sharing across
  workers.
- **`sac_scale` is a badge, never a facet** — 0.3% coverage.
- **`CLAUDE.md` is stale** and marked as such; `HANDOFF.md` is maintained.

## 10. Testing

`venv/bin/python -m pytest tests/ -q` — **200 tests**, ~0.2 s, no network and no
index load. They encode the traps above rather than the happy path: DEM noise not
becoming gain, `"No Data"` not becoming a value, an unmappable trail not being
reported as truncation, dots never loading geometry, a jeep road never being a
backpacking route.

`tests/test_api_wiring.py` walks the AST of every route handler and asserts each name
it reads is actually bound — added after a global string replace introduced
`NameError: name 'detail' is not defined` into the photos endpoint, a 500 that no
test touched because those endpoints need the real index to exercise.
