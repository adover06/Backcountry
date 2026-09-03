# Data sources

Every external source this project reads, what it actually contributes, what it
costs, and where it fails. Ordered by role.

Counts are from the current build: **13,786 trails**.

| source | contributes | licence | key |
|---|---|---|---|
| USFS National Forest System Trails | 8,162 trails | Public domain (US federal work) | no |
| OpenStreetMap ways | 3,092 trails | ODbL | no |
| NPS Public Trails | 1,863 trails | Public domain | no |
| SEKI fallback (third-party rehost) | 345 trails | Unconfirmed | no |
| OpenStreetMap `route=hiking` relations | 324 long-distance routes | ODbL | no |
| AWS Terrarium DEM | elevation for 100% | Public, no key | no |
| USGS GNIS | named features → scenery | Public domain | no |
| OpenStreetMap POIs | scenery GNIS lacks | ODbL | no |
| USFS INFRA recreation sites | trailheads, campgrounds | Public domain | no |
| USFS EDW Wilderness | land status for 12.7% | Public domain | no |
| OpenStreetMap access points | water, trailheads, camping | ODbL | no |
| Recreation.gov (RIDB) | permits for 22.3% | US federal | **yes** |
| Wikimedia Commons | trail photos | CC, per-image | no |
| Mapbox | basemap, terrain | Commercial | **yes** |
| NWS / AirNow / WFIGS / Open-Meteo / NOHRSC | live conditions | Public | AirNow only |

---

## Trail geometry and identity

### USFS National Forest System Trails — 8,162 trails
`data.py`, `pipeline/normalize.py`. A local GeoJSON extract, overridable with
`TRAILS_SOURCE`. Public domain as a US federal work.

The backbone of the index. Contributes official names, USFS trail class, typical
grade, surface, seasonal access windows, and — uniquely — **allowed use per trail**,
which is what lets the app tell a foot trail from a jeep road without hand labelling.

Gotchas that shaped the loader: property names are **lowercase** (`trail_cn`,
`trail_name`); an earlier loader read uppercase and returned zero trails. Values are
dual-encoded (`12-20%` and `TG05 - +12-20%`). `N/A`, `None` and `""` all mean
missing and must never become values. 12,315 raw segments group into 8,162 trails by
`TRAIL_CN`.

### NPS Public Trails — 1,863 trails
`pipeline/nps.py` → `mapservices.nps.gov`. Public domain, no key. Covers what USFS
structurally cannot: Yosemite, Redwood, Joshua Tree, Lassen, Point Reyes, Death
Valley. 5,757 features intersect the California bbox; 85% carry a readable `TRLNAME`.

Filtered by a **whitelist** of California park units rather than bbox alone, because
the bbox reaches into Nevada and Oregon.

### SEKI fallback — 345 trails
`pipeline/nps.py`, `SEKI_FALLBACK_URL` → `services.arcgis.com`. **Opt-in.** Sequoia
& Kings Canyon returns zero records from the national NPS layer — verified by both a
unit query and a spatial query — so Mount Whitney is otherwise absent entirely. This
is a third-party rehost with a matching schema; provenance is unconfirmed, so records
are tagged `SEKI fallback (third-party rehost)` and stay distinguishable rather than
being passed off as NPS-authoritative.

### OpenStreetMap ways — 3,092 trails
`pipeline/osm_trails.py` → Overpass. ODbL.

**The largest structural gap closed.** Both agency sources are federal, and most
Californians walk on regional, county and state park land that neither publishes.
Measured on the South Bay before this source existed: **15 trails in the index
against 4,415 named OSM ways.** Sierra Azul, Almaden Quicksilver, Santa Teresa,
Castle Rock and Henry Coe were all missing.

OSM models a trail as however many fragments mappers split it into, so ways are
grouped by name, chained where they touch, and clustered spatially — two "Ridge
Trail"s fifty miles apart are two trails, not one with a gap. Named sidewalks,
crossings and driveways are filtered out. Trails an agency already publishes are
dropped on name + bbox overlap, because the agency record carries trail class, grade
and season that OSM does not.

First Bay Area sweep: 12,065 ways → 3,198 trails → 3,092 after dedupe.
Operators confirm the diagnosis: MROSD, EBRPD, Santa Clara County Parks.

```bash
python -m pipeline.osm_trails                     # default: Bay Area
OSM_TRAILS_BBOX="w,s,e,n" python -m pipeline.osm_trails
```

Other metros (LA, San Diego, Sacramento) have the same gap and need the same sweep.

### OpenStreetMap `route=hiking` relations — 324 routes
`pipeline/osm_routes.py`. Built from a Geofabrik extract with `osmium`, not Overpass.

**This does not fix trail segmentation, and the distinction matters.** OSM models
Half Dome Trail as 2.00 mi — identical to the agency value. "Half Dome is a 14-mile
hike" is a statement about hike *composition*, which no OSM object encodes. Only 9.2%
of trails have a matching relation. What relations *are* good at is long-distance
routes no agency assembles: the JMT (208.7 mi across 94 members), Tahoe Rim, Lost
Coast, Skyline-to-the-Sea. Only routes ≥ 5 mi are kept.

---

## Enrichment

### AWS Terrarium DEM — elevation for 100%
`pipeline/elevation.py` → `s3.amazonaws.com`. Public, no key. ~30 m resolution.

Sampling is **distance-based** (one reading per 0.05 mi), not a fixed point budget,
which under-samples long trails. Gain uses a **hysteresis walk with a 15 ft
threshold**: summing raw positive deltas turns DEM noise into thousands of feet of
phantom climb — in testing, ±8 ft of noise over 400 samples produced 1,050 ft of fake
gain. Validated against Mt Whitney: sampled 14,477 ft against a true 14,505 ft.

Tiles cache to `.dem_cache/`, so re-runs are cheap (3,097 new trails sampled at
145/s).

**Known limit:** the profile's distance axis is measured on the thinned polyline
while the map draws the full one, so the profile ends at a median 94.4% of the drawn
length. The UI maps fraction-to-fraction to compensate.

### USGS GNIS — scenery
`pipeline/gnis.py` → `carto.nationalmap.gov`. Public domain, no key, filters exactly
to California. 18,437 named features. Primary scenery source: peaks, lakes, springs,
ridges, passes, basins, islands, beaches, bays, pillars, cliffs, marshes.

### OpenStreetMap POIs — scenery GNIS lacks
`pipeline/enrich_osm.py` → Overpass. ODbL. 55,041 fetched, merged to 50,681 total
(+32,244 new, 22,797 deduped against GNIS).

Supplies categories GNIS does not record **at all**: `viewpoint` (2,090 joined),
`cave`, `glacier`, `arch`. Waterfalls went 149 → 406 joined trails. Scenery coverage
overall: 33.0% → **52.3%**.

Dedupe is by distance, not rounded coordinates: 36.5785 and 36.57852 are 2 m apart
and land in different grid cells, so Mount Whitney survived twice. Same kind within
0.3 mi, matching names or an unnamed supplement.

> **Control-flow trap:** `stage_osm` fetches only when `pois.json` is absent. Once it
> exists the `--with-osm` branch is unreachable, which is why an OSM supplement never
> entered a resumed build. The sweep is now run explicitly and merged into the file.

### OpenStreetMap technical tags
`pipeline/technical.py`. Requires a local `osmium` extract, not Overpass.
`sac_scale` and `trail_visibility` are the only human-assessed difficulty signals in
any open source, but coverage is **0.3–0.5%** of hikeable ways. Far too sparse to
filter on — a facet dropping 99.7% of the index is worse than none — so they attach
as **badges only** and never drive ranking. The coverage is not random: mappers tag
`sac_scale` exactly where a trail stops being a walk.

---

## Access, land status and permits

### USFS INFRA recreation sites
`pipeline/usfs_rec.py` → `apps.fs.usda.gov` EDW. Public domain, no key.

**There is no trailheads layer in EDW.** Trailheads are a `site_type` inside
`EDW_RecInfraRecreationSites_02`, which is why searching for one finds nothing.
California holds 3,056 sites, 2,714 usable: **625 trailheads, 1,310 camping sites**,
plus day-use, picnic and visitor facilities — with fee, drinking water, restrooms,
season dates and a Recreation.gov link per site.

Absence arrives as the **string** `"No Data"`, not null. `site_type` is mapped
through a whitelist so a new upstream type is dropped rather than entering the index
as an unknown kind.

### OpenStreetMap access points
`pipeline/osm_access.py` → Overpass. ODbL. **11,237 points**: 5,528 drinking water,
3,449 campgrounds, 1,425 trailheads, 811 backcountry sites, 24 shelters.

Most trailheads are not on federal land, so the agency layers miss them. Water is the
starker gap: the index reached 1.4% coverage from 62 NPS points.

`natural=spring` is **deliberately excluded** despite 17,384 statewide — a spring is
untreated and may be seasonal, and promoting it to drinking water would claim there
is water where there may be none. Parking is admitted only when the name says
trailhead, or every supermarket car park qualifies.

Combined with NPS (918) and USFS (2,478): **14,633 access points**.

```bash
python -m pipeline.osm_access     # ~1h, caches per tile, resumes cleanly
```

### USFS EDW Wilderness — 12.7%
`pipeline/wilderness.py`. Public domain, no key. 88 California areas; 1,744 trails
inside, 892 entirely.

Land status, not proximity: point-in-polygon, hand-rolled to keep shapely and GEOS
out of the install. Wilderness changes the rules of a trip — permits, group size, no
bicycles — and none of it is derivable from geometry. Trails that only clip a
boundary are recorded as partly inside, with the fraction.

### Recreation.gov (RIDB) — 22.3%
`pipeline/permits.py` → `ridb.recreation.gov`. **Requires `RIDB_API_KEY`** (free,
from the RIDB profile page). The only stage needing a credential; skips cleanly
without it.

`/permitentrances` looks like the obvious endpoint and is not: 907 nationally but
only 34 in California, named as opaque codes, with no Half Dome or Whitney at all.
Permits are modelled as **facilities** with `FacilityTypeDescription == "Permit"` —
only 7 in California, but precisely the ones that gate California hiking.

A wilderness permit governs a whole forest from one coordinate, so the proximity join
radius is a deliberately wide **35 mi** and the result is advisory. Since wilderness
boundaries exist, an exact name match now supersedes that for the areas it covers
(Desolation, Hoover): 87 trails carry a governing permit rather than a nearby one.

### Wikimedia Commons
`pipeline/photos.py`. Per-image CC licences, resolved lazily per trail and cached.

---

## Live conditions (request time, not build time)

| source | endpoint | key | notes |
|---|---|---|---|
| **NWS** | `api.weather.gov` | no | `/points/{lat},{lng}` then the returned forecast URL; first 6 periods. Requires a User-Agent. |
| **AirNow** | `airnowapi.org` | **`AIRNOW_API_KEY`** | Current AQI observations; degrades gracefully when absent. |
| **WFIGS / NIFC** | `services3.arcgis.com` | no | Fire perimeters. Large feed, fetched whole and filtered locally by radius — caching matters. Filtering is by centroid/points, not true spatial intersection. |
| **Open-Meteo** | `api.open-meteo.com` | no | Snow depth and snowfall, 3×3 grid sample centred on the route's highest point. `snow_depth` is **metres**. |
| **NOHRSC** | `mapservices.weather.noaa.gov` | no | Snow analysis WMS raster overlay. |
| **Nominatim** | `nominatim.openstreetmap.org` | no | Place-name → bbox, in `magic.py` only. 1 req/s limit, enforced. |

All cached in-process with TTLs (`FIRE_CACHE_TTL_SECONDS`, `SNOW_…`, `WEATHER_…`,
`AQI_…`). The cache is **process-local** and does not survive restarts or span
workers.

---

## Frontend and infrastructure

- **Mapbox GL JS** — basemap, `mapbox://mapbox.terrain-rgb` for hillshade. Requires
  `VITE_MAPBOX_TOKEN`. The map is a progressive enhancement: search, filters,
  elevation profiles and comparison all work without it.
- **CalTopo** — referenced in `frontend/src/App.jsx` (legacy planner surface).
- **Firebase** — authentication only (`VITE_FIREBASE_*`, service account server-side).
- **PostgreSQL 16** — user data: accounts, trips, saved trails. Deliberately *not*
  trail data; see `ARCHITECTURE.md`.

### Stubbed, not active

Carrier coverage tiles (T-Mobile, AT&T, Verizon) appear in `planner_api.py` as
**empty strings with commented example URL patterns**. No carrier publishes a
documented tile API; these are placeholders and nothing is fetched.

---

## Licensing position

Everything is public domain, ODbL, or a permissive CC licence.

**ODbL** applies to all OpenStreetMap-derived data — trails, POIs, access points,
route relations. This project is personal-use and undistributed, so **share-alike
does not trigger**. If that changes, attribute OpenStreetMap contributors. The index
already declares its provenance:

```
USFS National Forest + NPS Public Trails + SEKI + OpenStreetMap (California)
```

The one unconfirmed item is the **SEKI rehost**, which is why it is opt-in and
labelled in every record it produces.

## Keys

Only two are needed, and both degrade gracefully:

```bash
RIDB_API_KEY=...        # permits stage; skipped without it
AIRNOW_API_KEY=...      # AQI checks; reports unavailable without it
VITE_MAPBOX_TOKEN=...   # frontend map only
```
