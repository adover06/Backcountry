# OpenTrails — handoff

California trail **discovery** app. Map-first search over 13,791 trails, with a
routing graph that composes segments into real hikes.

**Docs:** [`ARCHITECTURE.md`](ARCHITECTURE.md) — how it fits together and why ·
[`SOURCES.md`](SOURCES.md) — every data source, licence and caveat ·
[`pipeline/README.md`](pipeline/README.md) — build stages

Live: https://opentrails.andrewdover.com  ·  local: `docker compose -f docker-compose.local.yml up`
`main` is current. 88 tests (`venv/bin/python -m pytest tests/ -q`).

## The governing rule

**Missing is never rendered as a value.** Every check reports ok/unavailable, the
risk engine has an `incomplete` state that cannot render green, a gain filter
excludes a trail whose elevation is unknown rather than treating it as flat, and
unknown gain returns None rather than 0. Most bugs found this session were
violations of this: a failed DEM read became "flat", a missing Pillow install
became "no elevation", a rate-limited photo lookup became "no photos".

## Data (all public domain or ODbL; personal-use, so ODbL share-alike does not bind)

| source | contributes |
|---|---|
| USFS National Forest Trails | 8,162 trails, surface, grade, season, allowed uses |
| NPS Public Trails | 1,863 park trails + 448 trailheads / 408 parking |
| SEKI fallback | 345 incl. Mount Whitney (SEKI is empty in the NPS national layer) |
| OSM `route=hiking` relations | 324 long-distance routes (JMT 208.7 mi, PCT sections) |
| AWS Terrarium DEM | elevation for all 10,694 |
| USGS GNIS | 18,437 named features -> scenery tags on 3,526 trails |
| Recreation.gov (RIDB) | permits; 3,196 trails have an advisory |
| USFS INFRA recreation sites | 625 trailheads + 1,310 camping sites, with fee/water/restroom/season |
| USFS EDW wilderness | 88 designated areas; 1,744 trails inside, 892 entirely |
| OpenStreetMap access | 11,237 points: 5,528 drinking water, 3,449 campgrounds, 1,425 trailheads, 811 backcountry sites |
| OpenStreetMap ways | 3,097 trails on regional/county/state park land the federal sources do not publish (Bay Area sweep) |
| Wikimedia Commons | photos, resolved lazily per trail |

Rebuild: `python -m pipeline.build_index --resume` (stages are skippable and
resumable; `--skip-elevation`, `--skip-osm`, `--skip-permits`, ...).

## What to work on next

**1. Persist the routing graph.** It is now reachable in the product — "Plan a hike"
on the Discover map, click two points, `GET /api/discover/graph/route`. Validated:
Happy Isles -> Half Dome 13.88 mi / 5,245 ft in 4 legs; Curry Village -> Nevada Fall
9.18 mi / 2,475 ft in 5. Warm queries are ~25 ms.

What is left is the build. It takes ~55s and holds **1.8 GB resident**, and because
it is CPU-bound Python in one worker it *blocks every other request* while running —
the sidebar shows "Searching..." until it finishes. `GRAPH_PREWARM=1` moves that
stall into container startup, which is better but not a fix, and on a 1 GB VPS the
memory alone rules it out. Precomputing the graph to disk is the answer.

**1b. (done) Wire the routing graph into the product.** It works (`/graph` inspector proves
it: Half Dome 13.9 mi / 5,245 ft vs the 2.0 mi segment) but discovery still shows
segment lengths. Needs: persist the graph so it is not rebuilt for 36s per process;
pick destinations (GNIS summits/lakes already in the index); decide whether composed
hikes are a new entity or an action on a trail. **This is the central goal.**

**2. ~~USFS trailheads~~ — done.** 990 -> 1,974 trails (9.3% -> 18.5%). There is no
trailheads *layer* in EDW; trailheads are a `site_type` inside
`EDW_RecInfraRecreationSites_02`, which is why searching for one found nothing. The
same layer gave 1,892 trails a campground (previously zero) and carries fee, water,
restroom and season per site. See `pipeline/usfs_rec.py`.

The OSM sweep (`python -m pipeline.osm_access`) has since run, taking trailheads to
**2,481 (23.2%)** and drinking water from 1.4% to **7.6%**. Coverage by field now:

| field | before | after |
|---|---|---|
| trailhead | 990 (9.3%) | 2,481 (23.2%) |
| campground | 0 | 3,102 (29.0%) |
| backcountry camping | 0 | 894 (8.4%) |
| drinking water | 148 (1.4%) | 817 (7.6%) |
| wilderness | 0 | 1,744 (16.3%) |

The graph still needs start points for the remaining 77%. The sweep takes ~1h
against congested mirrors and caches per tile, so it resumes cleanly if interrupted.
`access.py` compares mtimes against its output, so a later sweep invalidates the
access cache instead of being silently ignored.

**3. Retire the planner wizard.** Its condition checks (NWS + alerts, AQI, fire,
snow, water, risk engine) are the best code in the repo. Its 7-step GPX wizard
predates the pivot. Move conditions into the Discover panel and delete most of
App.jsx's 2,800 lines.

**4. Frontend tests — currently zero.** Every real bug this session was frontend:
collapsed map container, an abort loop, feature-state rejecting string ids, appMode
never leaving "dashboard", facets counting their own dimension, straight-line routes.

**5. Mobile.** Zero breakpoints; fixed 380px sidebar.

## Map performance

The browse view was shipping ~10.4 MB and holding ~950 MB RSS. Now ~1.09 MB and
~170 MB. What mattered, in order:

| fix | effect |
|---|---|
| nginx had **no gzip at all** | JS 2.27 MB -> 625 KB; all API responses 5-6x smaller |
| search returned `nearby`/`permits`/`access` no card renders | 278 KB -> 9.5 KB per pan |
| dots refetched on every pan | fetched once per filter set, panning is client-only |
| `/map` and `/trail/{id}` read the 167 MB JSON sidecar | SQLite store, 4.68s -> 7 ms |
| full-resolution lines at every zoom | LOD tiers; 7.9 MB -> 265 KB gzipped at z12 |

`center` is a bbox midpoint and 13.2% of them sit >0.25 mi off their own trail, so
the map draws `marker` instead (trailhead, else on-line midpoint). Dots cluster
below z11 and become lines above it.

**Smaller:** ~~campgrounds from RIDB (579 in CA)~~ — superseded; the USFS INFRA layer
gave 1,310 sites with better attributes and no API key. RIDB
historical reservation data FY2006-2025 as a real popularity signal; OSM `surface`
to fill the 19% missing; scenery only covers 33%.

## Known and deliberate

- **Segmentation is not a data problem.** OSM models Half Dome as 2.00 mi exactly as
  the agency data does; no dataset publishes hike compositions because agencies
  record what they administer. Verified across RIDB, NPS, OSM, Hiking Project and
  GPX corpora. Hence the graph.
- **`sac_scale` is a badge, never a facet** — 0.3% coverage; a filter dropping 99.7%
  of the index is worse than no filter.
- **Difficulty is two axes.** Effort (Shenandoah formula) and steepness (ft/mi),
  the latter calibrated against Santa Clara County ratings: r=+0.57 for ft/mi vs
  r=+0.34 for total effort.
- **The "graph" is not a graph database** — an in-memory adjacency dict plus
  hand-written Dijkstra, rebuilt per process. No Neo4j, no persistence.
- Map tiles occasionally do not paint on first load; panning fixes it. Unresolved,
  may be environment-specific.

## Deployment

`docker-compose.traefik.yml` on the VPS at `~/server/backcountry` (**not** jstar).
Traefik network `public`, certresolver `myresolver`. Only the frontend is exposed.

Not in git, must be placed by hand: `.env`, `data/trails_index.json`,
`data/trails_geom.json`, `data/firebase-service-account.json`, and `.dem_cache/`
(799 MB, needed by the routing graph).

**The VPS image predates the Pillow fix**, so graph elevations are unknown there
until it is rebuilt and `.dem_cache` copied over.
