# Trail data pipeline

Turns raw agency data into the index that powers discovery. It is a **build step**,
not request-time work — the API loads a prebuilt index and never parses source data
on a request.

## Why a pipeline at all

No single free trail dataset is good enough. Each source holds one piece:

| Source | Contributes | License |
|---|---|---|
| USFS National Forest System Trails | geometry, official names, trail class, typical grade, surface, seasonal access windows, allowed uses | Public domain |
| AWS Terrain Tiles (Terrarium) | elevation → gain, min/max, profile | Public, no key |
| OpenStreetMap | scenery features (peaks, waterfalls, lakes, hot springs), plus trails in national and state parks that USFS omits | ODbL |
| USFS INFRA recreation sites | trailheads, campgrounds, day-use and visitor facilities, with fee / water / restroom / season | Public domain |
| USFS EDW wilderness | designated wilderness boundaries, for land status and permit rules | Public domain |
| OpenStreetMap access | trailheads, drinking water, backcountry camping, shelters | ODbL |

The product is the **conflation**. That is also why "there is no good free trail data"
feels true — the pieces exist, but nobody hands you them joined.

## Stages

```bash
python -m pipeline.build_index                 # all stages
python -m pipeline.build_index --skip-osm      # normalize + elevation only
python -m pipeline.build_index --limit 200     # quick smoke run
python -m pipeline.build_index --resume        # reuse the last build, fill gaps
```

### 1. `normalize.py`

Groups the raw feed's 12,315 **segments** into 8,162 hikeable **trails**.

Notable behavior:

- Reads **lowercase** property names (`trail_cn`, `trail_name`, `segment_length`).
  The original loader read uppercase and therefore returned zero trails.
- Keeps geometry as a **MultiLineString** and chains segments that actually share
  endpoints. Flattening disjoint segments into one LineString draws trail across
  ground the trail does not cross.
- Collapses dual-encoded values (`12-20%` and `TG05 - +12-20%`; `NATIVE MATERIAL`
  and `NAT - NATIVE MATERIAL`).
- Treats `N/A`, `None`, and `""` as **missing**, never as a value.

### 2. `elevation.py`

Samples AWS Terrarium DEM tiles along each trail.

- Sampling is **distance-based** (one reading per 0.05 mi), not a fixed point
  budget. A fixed count under-samples long trails and averages real climbs away.
- Gain uses a **hysteresis walk with a 15 ft threshold**. Summing raw positive
  deltas turns DEM noise into thousands of feet of phantom climbing — in testing,
  ±8 ft of noise over 400 samples produced 1,050 ft of fake gain.
- Tiles cache to `.dem_cache/`, so re-runs are cheap.
- Validated against Mt. Whitney: sampled 14,477 ft vs. the true 14,505 ft.

### 3. `enrich_osm.py`

Joins OSM scenery POIs to trail geometry via a grid index (`spatial.py`).

- Fetches POIs once per bbox tile and caches them, then joins offline. Querying
  per-trail would be thousands of requests; this is a few dozen, once.
- **Rotates across Overpass mirrors.** The public instances rate-limit and return
  504s or HTML error pages unpredictably; a single busy instance must never cause a
  tile to be silently recorded as empty.
- `fetch_hiking_ways()` pulls named OSM paths to cover national and state parks the
  USFS feed omits.

For a full rebuild, a Geofabrik extract (`california-latest.osm.pbf`, ~1.2 GB)
removes Overpass rate limits entirely and is the better path at scale.

### 4. `usfs_rec.py`

USFS developed recreation sites. There is no trailheads layer in EDW — trailheads
are a `site_type` inside `EDW_RecInfraRecreationSites_02`, which is why looking for
one by name finds nothing. California holds 3,056 sites, 2,714 of them relevant:
625 trailheads, 1,310 camping sites, plus day-use, picnic and visitor facilities.

Absence arrives as the **string** `"No Data"`, so cleaning is not optional; and
`site_type` is mapped through a whitelist, so a new upstream type is dropped rather
than entering the index as an unknown kind.

### 5. `wilderness.py`

Designated wilderness boundaries, joined by point-in-polygon rather than proximity.
Land status changes the rules of a trip — permits, group size, no bicycles — and
none of it is derivable from geometry.

Point-in-polygon is hand-rolled to keep shapely and GEOS out of the install. The
stage costs ~265s over the full index; two pre-filter optimizations were measured
and reverted (see the class docstring) because bbox rejection was never the cost.

Trails that only clip a boundary are recorded as partly inside, with the fraction,
rather than inheriting the area's permit implications.

### 6. `osm_access.py`

Trailheads, drinking water, backcountry camping and shelters from OSM — the access
infrastructure the federal layers do not cover, because most trailheads are on
state, regional and county land. Run separately from the build:

```bash
python -m pipeline.osm_access      # ~1h, caches per tile, resumes cleanly
```

It writes `data/osm_access_points.json`, which `access.py` merges when present. The
build never triggers this implicitly — a statewide Overpass sweep is too slow to
hide inside another stage.

`natural=spring` is deliberately excluded despite 17,384 statewide: a spring is
untreated and may be seasonal, and promoting it to drinking water would claim there
is water where there may be none.

### 7. `osm_trails.py`

The largest coverage gap, and a structural one. Both trail sources are federal, and
most Californians walk on regional, county and state park land that neither USFS nor
NPS publishes. Measured on the South Bay: **15 trails in the index against 4,415
named OSM ways**.

```bash
python -m pipeline.osm_trails          # sweep + assemble (slow; writes data/osm_ways.json)
```

OSM models a trail as however many fragments mappers split it into, so ways are
grouped by name, chained where they touch, and clustered spatially — two "Ridge
Trail"s fifty miles apart are two trails, not one with a gap. Named sidewalks,
crossings and driveways are filtered out; `highway=footway` is pavement as often as
trail. Trails an agency already publishes are dropped on a name + bbox-overlap
match, because the agency record carries trail class, grade and season that OSM
does not.

First Bay Area sweep: 12,065 ways → 3,198 trails → 3,097 added after dedupe.

### 8. `markers.py`

Which point represents a trail on the map. `center` is a coordinate average (USFS)
or a bounding-box midpoint (NPS), so on any trail that bends it is not on the trail:
13.2% of centres sit more than 0.25 mi off, the worst 4.71 mi. The marker is the
joined trailhead where there is one, otherwise the vertex at half the trail's
length — always exactly on the line. `center` is left alone; it is still the right
thing for fitting a viewport.

### 9. `geom_store.py`

Geometry in SQLite at four levels of detail, replacing whole-file reads of the JSON
sidecar. Serving one trail from the monolith cost 4.68s and 948 MB RSS; from the
store it is 7 ms and the process stays flat. Rebuild with:

```bash
python -m pipeline.geom_store
```

Run it after any build that changes geometry — it is not yet a build stage.

## Output

```
data/trails_index.json   ~12 MB   searchable records, no geometry
data/trails_geom.json   ~123 MB   geometry + elevation profiles, keyed by trail id
```

Split deliberately: the search index stays small enough to scan in memory in well
under a millisecond, while geometry is only loaded when a map needs it.

## The rule that governs every stage

**Missing is never rendered as a value.** A trail whose elevation could not be
computed carries `elevation: None`, and a gain filter *excludes* it rather than
treating it as 0 and implying the trail is flat. `index_status()` reports coverage
so the UI can say "scenery data not built yet" instead of showing an empty result
set as though nothing matched.

## Attribution

OSM-derived fields are ODbL. Any public distribution must credit
"© OpenStreetMap contributors" and share alike on the derived database.
