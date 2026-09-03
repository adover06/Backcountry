# OpenTrails Repo Handoff

> **STALE — read `HANDOFF.md` first.** This file describes an earlier version of the
> product: a 7-step GPX planning wizard whose sidebar/report layout has since been
> replaced by the map-first Discover app. Specific things below that are no longer
> true:
>
> - `alltrails_manual_collection/` **does not exist**. There is no AllTrails
>   ingestion path; AllTrails has no public API and its terms forbid scraping.
> - `data.py` loading `National_Forest_System_Trails_(Feature_Layer) (3).geojson`
>   directly has been replaced by the build pipeline in `pipeline/`, which writes
>   `data/trails_index.json` and `data/trails_geom.json`.
> - The API surface below (`/api/route/parse`, `/api/plan`, ...) is the planner
>   wizard's. Current discovery routes live in `discovery_api.py`.
> - The "Important Known Bug" about snow `depth_in` vs `max_depth_in` predates the
>   risk-engine work; verify against the code before acting on it.
>
> The conventions section and the external-API notes are still broadly accurate.
> `HANDOFF.md` is the maintained document.

This is the working context file for the entire repository. It is meant to let another model or harness take over without re-discovering the project structure, conventions, or current implementation state.

## One-Line Summary

OpenTrails is a California trail discovery app with a Python backend, a React/Mapbox frontend, and a separate LangGraph CLI advisor. The web app ingests GPX routes or trail-name searches, runs weather/AQI/fire/snow checks, and renders a full-screen map report with overlay telemetry.

## Current Product Shape

- Frontend: React + Vite + Mapbox GL JS + Tailwind.
- Backend: FastAPI with local GPX parsing and multiple check endpoints.
- Data: local GeoJSON trail dataset for California trails.
- External sources:
  - NWS weather
  - AirNow AQI
  - WFIGS / NIFC fire perimeters
  - Open-Meteo snow depth/snowfall sampling
- Web UX: top progress bar, step chips, full-screen map report, overlay cards, legend, no visible chat sidebar.
- CLI UX: LangGraph-based trail advisor that can gather preferences and recommend a trail.

## Repository Layout

Top-level files and directories that matter:

- `server.py` - FastAPI entrypoint for the web backend.
- `planner_api.py` - all web API routes.
- `planner/` - backend domain logic.
- `frontend/` - Vite React frontend.
- `data.py` - trail dataset loading and normalization.
- `main.py` - CLI entrypoint.
- `agent.py` - LangGraph orchestration for the CLI advisor.
- `tools.py` - tool functions used by the CLI advisor.
- `alltrails_manual_collection/` - manual batch workflow for trail data collection.
- `National_Forest_System_Trails_(Feature_Layer) (3).geojson` - canonical local trail source used by `data.py`.
- `claude.md` - this handoff file.

Generated / non-source items you should usually ignore:

- `frontend/dist/`
- `planner/__pycache__/`
- `planner/checks/__pycache__/`
- `.env` files with secrets

## How To Run

### Backend

Typical dev command:

```bash
python server.py
```

This starts FastAPI on `0.0.0.0:6767` via `uvicorn`.

### Frontend

From `frontend/`:

```bash
npm install
npm run dev
```

Production build:

```bash
npm run build
```

### CLI advisor

```bash
python main.py
```

The CLI assumes the LangGraph / Ollama setup in `agent.py` is reachable.

## Environment Variables

### Root `.env`

- `AIRNOW_API_KEY` - required for AQI checks.
- `FIRE_CACHE_TTL_SECONDS` - TTL for cached fire perimeter feed. Default: `21600`.
- `SNOW_CACHE_TTL_SECONDS` - TTL for cached Open-Meteo snow results. Default: `3600`.
- `WEATHER_CACHE_TTL_SECONDS` - TTL for cached NWS weather results. Default: `1800`.
- `AQI_CACHE_TTL_SECONDS` - TTL for cached AirNow results. Default: `1800`.
- `TRAILS_SOURCE` - optional path override for the trail GeoJSON source.

### Frontend `frontend/.env`

- `VITE_MAPBOX_TOKEN` - required for Mapbox rendering.

### Example files

- Root example: `.env.example`
- Frontend example: `frontend/.env.example`

## Architecture Overview

There are three major subsystems:

1. Web planner app
2. CLI trail advisor
3. Trail data / batch tooling

### 1. Web Planner App

The web app is the active product surface. It has these stages:

- Upload GPX route
- Set trip dates
- Match trail
- Run checks
- View report

Important behavior:

- GPX upload flow calls `/api/route/parse`, then `/api/trail/match`, then `/api/plan`.
- Name-search flow calls `/api/trail/match` with `name_hint`, then directly calls `/api/checks/*`.
- The report view is map-first and uses a full-screen map area with overlay panels.
- The sidebar chat UI has been removed from the active layout.

### 2. CLI Advisor

The CLI is a LangGraph-driven assistant that:

- collects preferences conversationally
- calls tools to search trails and fetch weather/permit/photo info
- transitions from a gather phase to a recommendation phase

It is not the same as the web planner, but it shares the same trail dataset and some conceptual logic.

### 3. Trail Data / Batch Tooling

The repo also contains a manual AllTrails collection workflow under `alltrails_manual_collection/`. This is a separate pipeline for producing and merging trail records.

## Backend API Surface

All routes live in `planner_api.py`.

### `POST /api/route/parse`

Input:

- multipart form field `file` containing a `.gpx`

Output:

```json
{
  "route": {
    "points": [{"lat": 0, "lng": 0, "ele": 0}],
    "length_miles": 0,
    "elev_gain_ft": 0,
    "midpoint": [0, 0]
  }
}
```

### `POST /api/trail/match`

Input JSON:

```json
{
  "route": {},
  "name_hint": "optional string"
}
```

Behavior:

- supports GPX route matching if route midpoint exists
- supports name-only fuzzy search if `name_hint` exists and route is empty

Output:

```json
{
  "shortlist": [],
  "auto_selected": null,
  "confidence": "low"
}
```

### `POST /api/checks/weather`

Input JSON:

```json
{ "lat": 0, "lng": 0 }
```

Output:

- `forecast` array of short summaries
- optional `error`

### `POST /api/checks/aqi`

Input JSON:

```json
{ "lat": 0, "lng": 0 }
```

Output:

- `observations` array
- optional `error`

### `POST /api/checks/fire`

Input JSON:

```json
{ "lat": 0, "lng": 0, "radius": 50.0 }
```

Output:

- `perimeters` GeoJSON `FeatureCollection`
- `count`
- optional `error`

### `POST /api/checks/snow`

Input JSON:

```json
{ "lat": 0, "lng": 0, "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "radius": 5.0 }
```

Output:

- `provider` = `Open-Meteo`
- `message`
- `max_depth_in`
- `avg_depth_in`
- `max_snowfall_in`
- `snow_detected_samples`
- `samples`
- `geojson`

### `POST /api/plan`

Input: multipart form:

- `file` (GPX)
- `start_date`
- `end_date`
- `name_hint` optional
- `selected_trail_id` optional

Output keys:

- `generated_at`
- `route`
- `trail_match`
- `selected_trail`
- `checks`
- `risk`
- `report`
- `map_layers`

This is the combined GPX planning endpoint used by the frontend.

## Web App State Flow

Main source file: `frontend/src/App.jsx`

### Core states

- `activeStep`
- `selectedFile`
- `inputMode`
- `trailName`
- `gpxRoute`
- `route`
- `trailMatch`
- `selectedTrail`
- `selectedTrailId`
- `checks`
- `planResult`
- `loading`

### GPX flow

1. User uploads GPX.
2. Frontend calls `/api/route/parse`.
3. Parsed route is stored in both `route` and `gpxRoute`.
4. User sets dates.
5. Frontend calls `/api/trail/match`.
6. User confirms match or continues.
7. Frontend calls `/api/plan`.
8. Report view renders map and telemetry.

### Name search flow

1. User enters trail name / region.
2. Frontend calls `/api/trail/match` with `route: { points: [] }` and `name_hint`.
3. Auto-selected trail is stored in `selectedTrail` and `selectedTrailId`.
4. Frontend skips GPX-only planning and runs checks directly.
5. Checks are fetched via `/api/checks/weather`, `/api/checks/aqi`, `/api/checks/fire`, `/api/checks/snow`.
6. Report view renders selected trail geometry, fire polygons, snow points, and telemetry.

## Frontend Layout / UI Notes

The current UI is intentionally map-forward.

Important characteristics:

- No visible left chat sidebar in the active app shell.
- Sticky top header with:
  - app title
  - step progress bar
  - clickable step chips
- Report step uses a full-screen map area below the header.
- Map overlays appear on top of the map:
  - route summary card
  - telemetry / briefing card
  - legend in lower-left
- The report step is the main visual surface.

Map behavior:

- Mapbox style: `mapbox://styles/mapbox/outdoors-v12`
- Terrain: enabled via `mapbox://mapbox.terrain-rgb`
- Pitch: 45 degrees
- Route: dark teal line with a glow
- Fire: orange polygons + labels + click popups
- Snow: blue/purple sample points + labels

## Frontend Map Logic

In `ReportStep`:

- `routeToFeature(route)` converts parsed GPX points into a GeoJSON LineString.
- `toLineFeature(layer)` normalizes existing route GeoJSON Feature/LineString data.
- The map uses GPX route first, then trail geometry, then selected trail coordinates.
- Fire perimeters come from `planResult.checks.fire.perimeters` or `map_layers.fire_perimeters`.
- Snow points come from `planResult.checks.snow.geojson`.

Important current map behaviors:

- Fire polygons are labeled using `IncidentName`, `poly_IncidentName`, `IRWINID`, or fallback `Fire perimeter`.
- Clicking a fire polygon opens a popup.
- Snow points are rendered as circles sized by `max_depth_in`.
- Snow labels show inches.
- Route and snow/fire layers are reordered so route stays visually on top where needed.

## Trail Data Model

Primary source: `National_Forest_System_Trails_(Feature_Layer) (3).geojson`

`data.py` does the following:

- finds the source GeoJSON file via `TRAILS_SOURCE` or the default local file
- groups segments by `TRAIL_CN`
- aggregates trail metadata into normalized trail records
- preserves full geometry in `geometry`

Fields produced per trail:

- `id`
- `name`
- `area`
- `city`
- `lat`
- `lng`
- `length_miles`
- `elev_gain_ft`
- `difficulty`
- `route_type`
- `avg_rating`
- `num_reviews`
- `popularity`
- `features`
- `activities`
- `visitor_usage`
- `slug`
- `geometry`

Notes:

- `lat` / `lng` are trail centroid-like averages derived from first coordinates.
- `geometry` is a GeoJSON LineString in `[lng, lat]` order.
- Difficulty is inferred from USFS trail class.
- Feature tags are inferred from name/area/surface keywords.

## Checks Implementations

All check modules live under `planner/checks/`.

### `weather.py`

- Uses NWS `/points/{lat},{lng}` then the returned forecast URL.
- Returns the first 6 periods.
- Cached server-side by rounded lat/lng.

### `aqi.py`

- Uses AirNow current observation API.
- Requires `AIRNOW_API_KEY`.
- Cached server-side by rounded lat/lng.

### `fire.py`

- Uses WFIGS / NIFC `WFIGS_Interagency_Perimeters` ArcGIS endpoint.
- Pulls GeoJSON and filters locally to a radius around the request midpoint.
- Adds recency tags where possible.
- Cached server-side for the raw perimeter feed.

### `snow.py`

- Uses Open-Meteo forecast API.
- Samples a 3x3 grid around the relevant point.
- The sample center is based on the highest-elevation point in the route if a route is provided.
- Returns per-sample point GeoJSON.
- Cached server-side by location/elevation/date/radius.

## Caching Strategy

Current caching is in-memory TTL caching.

Files:

- `planner/checks/cache.py`

Behavior:

- `TTLCache` is a simple process-local cache.
- It is shared across requests handled by the same backend process.
- It is not client-side.
- It does not persist across restarts.
- It is not shared across multiple backend workers/instances.

Cache TTL env vars:

- `FIRE_CACHE_TTL_SECONDS` default `21600`
- `SNOW_CACHE_TTL_SECONDS` default `3600`
- `WEATHER_CACHE_TTL_SECONDS` default `1800`
- `AQI_CACHE_TTL_SECONDS` default `1800`

Current recommendation:

- keep TTL caching for now
- move to Redis or disk-backed cache only if you need persistence or multi-worker sharing

## Risk / Report Logic

### `planner/risk_engine.py`

Current logic is deterministic and simple:

- AQI >= 150 => `no-go`
- AQI >= 100 => `caution`
- any fire perimeters => at least `caution`
- snow should also influence caution, but see caveat below

### `planner/report_ai.py`

Despite the filename, this is currently deterministic bullet construction, not an LLM call.

It returns:

```json
{
  "format": "bullets",
  "bullets": []
}
```

Bullets include:

- status
- route / selected trail summary
- risk reasons

## Important Known Bug / Mismatch

There is a current mismatch between risk scoring and the snow check payload:

- `risk_engine.py` checks `checks["snow"]["depth_in"]`
- `snow.py` returns `max_depth_in`, `avg_depth_in`, `max_snowfall_in`

This means snow may not currently affect risk the way it was intended unless that field is reconciled.

If fixing this, decide whether to:

- rename snow output to include `depth_in`
- or update `risk_engine.py` to read `max_depth_in`

## CLI / LangGraph Advisor

Files:

- `agent.py`
- `tools.py`
- `main.py`

### `agent.py`

This is a LangGraph workflow with two phases:

1. Gather preferences
2. Recommend one trail

It uses an Ollama-hosted model through a ChatOpenAI-compatible endpoint:

- base URL: `http://100.86.195.79:11434/v1`
- model: `qwen3`

### Gather phase

The assistant collects:

- region
- difficulty
- length range
- desired features
- route type

It can call tool functions to store preferences incrementally.

### Recommend phase

Once preferences are gathered, it searches the trail index and writes a single trail recommendation.

### `tools.py`

Important functions:

- `set_preferences`
- `search_trails_raw`
- `search_trails`
- `get_trail_details`
- `get_weather`
- `get_aqi`

Note:

- `tools.py` contains older string-returning helper implementations.
- These are distinct from the newer structured web API modules under `planner/checks/`.

### `main.py`

CLI entrypoint. It:

- prints a banner
- shows trail stats
- shows env status for key integrations
- runs the LangGraph agent loop in a terminal

## Trail Search Logic

### `planner/trail_matcher.py`

This module supports both:

- midpoint-based GPX matching
- name-only fuzzy search

Libraries used:

- `rapidfuzz`

Behavior:

- If route midpoint exists, score trails by distance to midpoint.
- If `name_hint` exists without a midpoint, use fuzzy matching on trail name, area, and city.
- Returns shortlist + auto_selected + confidence.

## Data / Import Dependencies

Key Python packages used directly by the web app:

- `fastapi`
- `uvicorn`
- `requests`
- `gpxpy`
- `python-dotenv`
- `rapidfuzz`
- `mapbox-gl` is frontend-only

`requirements.txt` is large because the repo also carries LangChain / LangGraph / CLI dependencies.

## Current Frontend Source Files

Most important files:

- `frontend/src/App.jsx` - main app shell and all steps
- `frontend/src/index.css` - includes Mapbox CSS and global styles
- `frontend/src/main.jsx` - React mount point
- `frontend/index.html` - Vite entry HTML

Build output exists in:

- `frontend/dist/`

Do not treat `dist/` as source of truth.

## Current UX Details to Preserve

The active UX was intentionally changed away from the sidebar chat layout.

Keep in mind:

- top progress bar is part of the current layout
- report view should remain map-first
- overlay cards are how telemetry is shown
- route/fire/snow legends are intentionally present
- GPX route should be visible on the report map whenever a GPX route exists

## External API Notes

### NWS

- used for weather forecast
- no API key required
- requests should include a User-Agent

### AirNow

- requires `AIRNOW_API_KEY`
- if missing, AQI should degrade gracefully

### WFIGS / NIFC

- fire perimeter feed can be large
- currently fetched as GeoJSON and filtered locally
- caching is important to avoid repeated heavy requests

### Open-Meteo

- used for snow depth and snowfall sampling
- response semantics matter:
  - `snow_depth` is meters
  - `snowfall_sum` is not an image or polygon, it is point forecast data

## Current Practical Caveats

- Fire filtering is based on geometry points/centroids, not true spatial intersection.
- Snow is sampled, not rasterized.
- Weather and AQI are point lookups, not route-aware.
- The cache is process-local.
- Large real-time geospatial APIs can still be slow or flaky.
- `risk_engine.py` snow logic needs reconciliation with snow output fields.
- `report_ai.py` is not truly AI-driven yet; it is deterministic bullet generation.
- The repo contains generated build artifacts and compiled bytecode because of the current working tree state.

## Suggested Next Improvements

If continuing work on the web app, the highest-value next steps are:

1. Fix snow risk scoring to use the actual snow summary field names.
2. Add route segment stats if you want snow/fire by elevation band.
3. Expand the report overlays to show exact numeric telemetry with color-coded statuses.
4. Consider a shared cache (Redis) if backend concurrency grows.
5. Add route-fire proximity and route-snow summary metrics if the app needs more than point sampling.
6. If AI is added later, keep it for explanation/summarization, not source-of-truth calculations.

## Important Files By Purpose

### Web backend

- `server.py`
- `planner_api.py`
- `planner/route_parser.py`
- `planner/trail_matcher.py`
- `planner/map_layers.py`
- `planner/risk_engine.py`
- `planner/report_ai.py`
- `planner/checks/weather.py`
- `planner/checks/aqi.py`
- `planner/checks/fire.py`
- `planner/checks/snow.py`
- `planner/checks/cache.py`

### Web frontend

- `frontend/src/App.jsx`
- `frontend/src/index.css`
- `frontend/src/main.jsx`

### CLI / advisor

- `main.py`
- `agent.py`
- `tools.py`

### Data / tooling

- `data.py`
- `alltrails_manual_collection/README.md`
- `alltrails_manual_collection/*.py`

## Notes For Future Models

- Prefer small, deterministic changes unless a larger UI or architecture change is explicitly requested.
- Preserve existing behavior unless the user asks to replace it.
- For map work, validate the geometry shape before assuming it is renderable by Mapbox.
- For external data, always consider caching and API semantics before changing output logic.
- Do not assume the CLI advisor and web app share the same runtime path; they are related but separate.

## Last Verified State

- Frontend builds successfully with `npm run build`.
- Backend Python syntax checks have been passing for the touched modules.
- The web app currently uses the full-screen map report layout with overlays and a top progress bar.
