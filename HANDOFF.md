# OpenTrails — handoff

California trail **discovery** app. Map-first search over 10,694 trails, with a
routing graph that composes segments into real hikes.

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
| Wikimedia Commons | photos, resolved lazily per trail |

Rebuild: `python -m pipeline.build_index --resume` (stages are skippable and
resumable; `--skip-elevation`, `--skip-osm`, `--skip-permits`, ...).

## What to work on next

**1. Wire the routing graph into the product.** It works (`/graph` inspector proves
it: Half Dome 13.9 mi / 5,245 ft vs the 2.0 mi segment) but discovery still shows
segment lengths. Needs: persist the graph so it is not rebuilt for 36s per process;
pick destinations (GNIS summits/lakes already in the index); decide whether composed
hikes are a new entity or an action on a trail. **This is the central goal.**

**2. USFS trailheads.** Only 990 of 10,694 trails have a trailhead, because only NPS
publishes them here. USFS EDW has a trailheads point layer, not yet ingested. Blocks
both "where do I park" and the graph, which needs start points.

**3. Retire the planner wizard.** Its condition checks (NWS + alerts, AQI, fire,
snow, water, risk engine) are the best code in the repo. Its 7-step GPX wizard
predates the pivot. Move conditions into the Discover panel and delete most of
App.jsx's 2,800 lines.

**4. Frontend tests — currently zero.** Every real bug this session was frontend:
collapsed map container, an abort loop, feature-state rejecting string ids, appMode
never leaving "dashboard", facets counting their own dimension, straight-line routes.

**5. Mobile.** Zero breakpoints; fixed 380px sidebar.

**Smaller:** campgrounds from RIDB (579 in CA, same join as permits); RIDB
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
