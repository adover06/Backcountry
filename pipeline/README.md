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
