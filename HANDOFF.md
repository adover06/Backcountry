# Session handoff — 2026-09-01

State: **working and verified.** 80 tests pass, frontend builds. Index holds **10,370**
California trails — all with real DEM elevation, **3,265 with named scenery tags**,
every one through the scenery join (`scenery_uncomputed: 0`). Photos, dual-axis
difficulty, and Sequoia/Kings Canyon are all in.

## The two bugs that made the app unusable

Both fixed. They are why it never worked, and neither was a trust problem:

1. **No `.env` existed** and `data.py` looked for a root-level GeoJSON that is not in
   the repo. Every trail endpoint returned 500.
2. **`data.py` read UPPERCASE property names** (`TRAIL_CN`, `TRAIL_NAME`) against a
   feed that uses lowercase. Even with the path fixed it loaded **zero** trails.

`data.py` now returns 9,846 trails with real elevation.

## Still required to run it

```bash
# frontend/.env — the map cannot render without this
VITE_MAPBOX_TOKEN=pk....
VITE_API_BASE=http://localhost:6767
```

Root `.env` is optional; see `.env.example` for the new pipeline and check variables.

## What was built

### Pipeline (`pipeline/`) — build step, not request-time

| Module | Job |
|---|---|
| `normalize.py` | USFS segments → 8,162 hikeable trails. Reads the real lowercase fields; keeps MultiLineString so disjoint segments are not joined by a line that does not exist. |
| `nps.py` | **1,684 National Park trails** (Yosemite, Redwood, Death Valley, Joshua Tree…). Public domain, no key. Filtered to a California unit whitelist. |
| `elevation.py` | AWS Terrarium DEM. Validated: Whitney sampled 14,477 ft vs. 14,505 true; Ryan Mountain 1,055 ft vs. ~1,050 actual. |
| `gnis.py` | **18,437 named CA scenery features** from the USGS gazetteer — 5,900 peaks, 3,706 lakes, 3,010 springs, 764 passes, 178 waterfalls, 57 hot springs. Public domain, no key, exact `state_alpha='CA'` filter. **This is the scenery source.** |
| `enrich_osm.py` | Spatial join + optional Overpass supplement (`--with-osm`, off by default — it kept throttling). |
| `photos.py` | Wikimedia Commons photos, resolved **lazily per trail** and cached (bulk would be ~7 h). Ranked by filename relevance, not proximity — proximity alone surfaced marmots and pine-cone macros over the view from the pass. Only CC/PD licences pass; each carries `attribution_required`. |
| `spatial.py` | Dependency-free grid index (avoids the GEOS/GDAL install). |
| `build_index.py` | Orchestrator. Stages are resumable: `--resume`, `--skip-osm`, `--skip-nps`, `--limit`. |

Rebuild: `python -m pipeline.build_index --resume`

### Design system

`frontend/src/index.css` holds the discovery tokens. Both themes are defined there;
components never hardcode colour.

- **Dark is true black (OLED)** — `#000` canvas with surfaces stepping up
  (`#0a0a0b` panel → `#141416` raised). Depth comes from luminance steps plus a
  four-level shadow scale (`--e1`…`--e4`), not a grey wash. Anything floating over
  the map is frosted glass (`--glass` + `backdrop-blur-xl`).
- **Difficulty hues are per-theme** (`--d-easy`…`--d-very`), brightened for dark
  where the light ramp muddies. `readPalette()` reads them from CSS so the chips,
  the legend and the Mapbox line colours can never drift apart.
- Typeface is **Inter**; icons are inline Lucide-style SVG (no emoji — they are
  font-dependent and render inconsistently).
- Focus ring, 150–300ms transitions, and `prefers-reduced-motion` are global.
- Basemap swaps with the theme (outdoors-v12 / dark-v11) and **terrain + hillshade
  + fog** are enabled, so ridgelines read as landscape rather than flat fill.

Theming keys off `data-theme` on `<html>`, not a `.dark` class: Tailwind v4 ignores
the v3 `darkMode: "class"` option, and the planner's existing dark rules already use
that attribute — one switch drives both surfaces.

### Discovery (the pivot)

- `planner/discover.py` — faceted search: bbox/viewport, length, gain, difficulty,
  features, route type, month, activity. Difficulty is the published Shenandoah
  formula `sqrt(2 · gain · miles)`, and is **None** when gain is unknown.
- `discovery_api.py` — `/api/discover/{status,search,map,facets,trail/{id}}`
- `frontend/src/pages/DiscoverPage.jsx` — map-first UI, now at `/`. Planner moved to `/plan`.

### Safety audit — all 23 items addressed

The governing rule: **a check that failed is not a check that passed.** Every check
module returns an explicit `status`; `risk_engine.py` has a fourth state,
`incomplete`, that cannot render green.

Highlights: weather is now scored at all (temp/wind/blizzard-class phrases); NWS
active alerts are fetched; the frontend's duplicate `computeRisk()` is deleted so
there is exactly one engine; fire paginates with truncation detection, measures to
the nearest perimeter vertex, and defaults to 60 days instead of 3,650; the LLM
explains the verdict instead of producing one and is told `UNAVAILABLE` means
unknown; elevation gain is thresholded (±8 ft of noise produced 1,050 ft of phantom
gain before).

## Where it stands

**Done:** foundation repair, 23/23 audit items, DEM elevation for all 9,846 trails,
NPS park coverage, discovery search + API + UI, 71 tests.

**Scenery is done.** GNIS replaced the stalled OSM path. Verified working:
`waterfall + 500ft gain` → 43 matches (Sturtevant Falls on the Gabrielino, Burney
Falls on the PCT); `hot_spring` → 8 (Iva Bell); `lake AND peak, 3-10mi` → 106.
Facet counts: peak 1209, lake 1156, ridge 385, spring 372, pass 332, waterfall 94.

**Not done:**
1. ~~Overnight low~~ — done: shown per-day in the planner plus a "Coldest night"
   telemetry card that highlights below-freezing.
2. Real names for Yosemite's unnamed runs (needs OSM `route=hiking` relations).
3. Old placeholder, kept for reference: is computed (`coldest_overnight()`) but not surfaced in the planner UI.
   ~~Sequoia & Kings Canyon missing~~ — **resolved.** SEKI is genuinely empty in the
   NPS national layer, so `fetch_seki()` now pulls the fallback layer: 345 trails,
   812 mi, including Mount Whitney (8.7 mi, 5,364 ft gain, 13,665 ft at Trail Crest).
   Tagged `source: "SEKI fallback (third-party rehost)"` so it stays distinguishable
   from NPS-authoritative records.

## Difficulty is two axes, not one

Calibrated against Santa Clara County Parks' human ratings — the only ground-truth
difficulty labels in any free California source. Over 217 of their named trails,
DEM-sampled:

| metric | correlation with their 1-5 rating |
|---|---|
| **ft per mile (steepness)** | **+0.568** |
| total gain | +0.363 |
| Shenandoah score (effort) | +0.336 |
| length | +0.152 |

Their median ft/mi ran 109 / 214 / 393 / 539 for ratings 2/3/4/5. So what people call
"difficulty" is mostly **steepness**, and length barely registers.

The app now reports both, and they are filterable and sortable separately:
- **Effort** — Shenandoah `sqrt(2 · gain · miles)`: how big the day is
- **Steepness** — ft/mi, banded at the midpoints between those medians: how hard the
  climbing is

A 1 mi / 1000 ft grind and a 20 mi / 1000 ft stroll are no longer the same label.

## Data strategy

No single free trail dataset is sufficient — the product is the **conflation**.
Confirmed usable: USFS (public domain), NPS (public domain), AWS Terrarium DEM
(free, no key), OpenStreetMap (ODbL — and since this project is personal-use only
and not distributed, share-alike does not trigger).

Two high-value leads from research, not yet integrated:

- **Santa Clara County Parks** — 1,747 trails with real human-assigned difficulty,
  98% populated, 5 levels. This is a **labeled training set**: calibrate a difficulty
  model against ground truth, then extrapolate statewide.
  `https://services1.arcgis.com/4QPaqCJqF1UIaPbN/arcgis/rest/services/Santa_Clara_County_Parks_Trails/FeatureServer/1`
- **MROSD** — 944 trails shipping `Z_MIN`/`Z_MAX`/`AVG_SLOPE` per trail. Small, but an
  independent check on the Terrarium DEM numbers.
  `https://services2.arcgis.com/qmhndvC947rDNl6t/arcgis/rest/services/Trail_Midpen_Only_(public)/FeatureServer/0`

Research: gov-data and commercial-data both reported and their findings are captured
below. osm-data hit its own session limit before reporting. Its last signal: it had a real
Geofabrik PBF loaded and was hitting the "classic pyosmium anti-pattern (Python-level
node lookup)" — i.e. bulk PBF parsing needs the C++-speed osmium path, not per-node
Python callbacks. That research is re-runnable and still owns the Yosemite naming
problem below.

## RESOLVED: Yosemite under-import

Yosemite is the one California park where NPS naming is poor — 589/1308 named (45%),
against 95-100% everywhere else. The CA-wide 86% figure was real but SAMO's 1,997
fully-named records masked it.

The unnamed segments were **not** connectors: `TRLFEATTYPE="Park Trail"`, Class 3,
hiker/pack-stock — the backcountry network, ~62% of the park's mileage. `nps.py`
originally dropped them, so the map showed about a third of Yosemite.

**Fix:** `_unnamed_networks()` chains unnamed segments per park into connected runs
(dropping fragments under 0.25 mi) and keeps them with `named: False` and an explicit
"Unnamed trail (Park)" label.

| | before | after |
|---|---|---|
| Yosemite records | 137 | 299 |
| Yosemite mileage | 373 mi | **981 mi** |

Statewide this added 179 unnamed runs / 620 mi. They carry real scenery: the unnamed
Yosemite runs join to **Nevada Fall**, **Rancheria Falls**, and **Tuolumne Falls**.

**Deliberately not done:** naming them from the nearest GNIS feature. A guessed name
presented as a real one is the exact failure this project has spent the whole session
correcting. Real names need OSM `route=hiking` relations.

## Research results (3 agents, verified against live endpoints)

### Integrate next

**GNIS / TNM Gazetteer** — ✅ **INTEGRATED** (`pipeline/gnis.py`). 18,437 CA features.

One trap worth remembering if you touch this: **layer 5 returns multipoint geometry**
(`{'points': [[lng,lat],...]}`) while layer 7 returns `{'x','y'}`. Reading only x/y
silently dropped all 23,073 landforms — every summit and pass in California — and the
run still "succeeded" with 7,465 hydro features. `_coords()` handles both and counts
what it skips.

**Wikimedia Commons GeoSearch** — no key, CC-BY/CC-BY-SA/PD. Tested against our actual
trails: **68% have ≥1 CC image within 2 km** (median 3, max 40); 32% have none.
Pipeline verified end-to-end (`geosearch` → `imageinfo` → thumbnail + license +
attribution). Sample along the polyline, not the midpoint. This solves photos.

**Popularity, since reviews are unobtainable:** Flickr **photo-user-days** is the
academically validated proxy (Wood et al. 2013, used in InVEST). Combine with Commons
density, Recreation.gov permit demand, and trailhead access into a composite score.

### Verified dead ends — do not spend time here

- **Strava** — ToS forbids displaying other users' data even when public, bans
  competing products, and now bars API data in AI models. No heatmap access.
- **Google Places** — may **not cache** names, ratings, reviews, or photos; only
  `place_id` is exempt. Cannot back a stored popularity DB. $32–40/1k calls.
- **Hiking Project** — API deprecated 2020, onX declining new requests, no archive.
- **Komoot** partner-only · **CalTopo** undocumented internal · **Gaia** none ·
  **FATMAP** shut down Oct 2024.
- **Scraped GitHub/Kaggle trail datasets** (`j-ane/trail-data`, `oschow/take-a-hike`)
  — AllTrails-derived with **no license grant** = all rights reserved. Not a shortcut.
- **USGS TNM Trails** (53,310 CA) — **skip**: a re-aggregation of the USFS data we
  already have, with `trailsurface`/`routetype`/`seasonopen` 100% null and degraded
  names. **PAD-US** — skip for CA; CPAD is better and cleaner.
- **Wikidata** — only ~45 distinct CA trail entities. A notability signal for famous
  trails, not coverage.

### AllTrails — factual

No public API. `robots.txt` disallows `ClaudeBot`, `Claude-User`, `GPTBot`, `CCBot`,
and blocks `/api/`, `/api-v4/`, `/api-v5/`, `/explore/map/` for all agents. ToS bars
automated access and scraping. They also own the dead competitors (EveryTrail,
Trails.com, GPSies). The one legitimate path is **exporting your own recorded
activities** — which is what a personal GPX import should consume. No scraper was
designed or built.

## A product issue worth deciding on

Agency data is **administrative segments, not complete hikes**. "Half Dome Trail"
indexes as 2.0 mi / 1,843 ft — accurate for that segment, but a user expects the full
14-mile round trip. Options: stitch connected segments into named routes, adopt OSM
`route=hiking` relations (which already model complete trails), or present segments
honestly as segments. This is the main thing standing between the current index and
something that feels like AllTrails.
