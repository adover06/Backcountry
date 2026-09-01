"""
Planner API endpoints for trip readiness checks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from planner.auth.deps import get_current_user
from planner.db import get_session
from planner.models import TripPlan, User
from planner.rate_limit import limiter
from planner.storage import write_gpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from planner.route_parser import parse_gpx_bytes
from planner.trail_matcher import match_trail, suggest_trails
from planner.checks.weather import get_active_alerts, get_weather_summary
from planner.checks.aqi import get_aqi_summary
from planner.checks.fire import get_fire_summary
from planner.checks.snow import get_snow_summary
from planner.checks.water import get_water_summary
from planner.risk_engine import evaluate_risk
from planner.report_ai import build_ai_report
from planner.map_layers import build_map_layers
from planner.itinerary_ai import generate_report


router = APIRouter()


def _require_coords(payload: dict) -> tuple[float, float]:
    """Validate lat/lng from a request body.

    Uses an explicit None check: `if not lat` rejects a legitimate 0.0, and
    coercing silently would let a malformed request run checks at Null Island.
    """
    lat, lng = payload.get("lat"), payload.get("lng")
    if lat is None or lng is None:
        raise HTTPException(status_code=400, detail="lat and lng required")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lat and lng must be numeric")
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        raise HTTPException(status_code=400, detail="lat/lng out of range")
    return lat, lng


def _bounded_radius(payload: dict, default: float, maximum: float) -> float:
    """Clamp a client-supplied radius instead of passing it through unvalidated."""
    try:
        radius = float(payload.get("radius", default))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="radius must be numeric")
    return max(0.1, min(radius, maximum))


def _highest_route_point(route: dict | None) -> tuple[float, float] | None:
    """The highest point on the route, used for weather and snow sampling."""
    points = (route or {}).get("points") or []
    usable = [p for p in points if isinstance(p.get("ele"), (int, float))]
    if not usable:
        return None
    best = max(usable, key=lambda p: p["ele"])
    if best.get("lat") is None or best.get("lng") is None:
        return None
    return best["lat"], best["lng"]


@router.post("/api/route/parse")
@limiter.limit("30/minute")
async def parse_route(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Only GPX uploads are supported for now.")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="GPX file too large (20 MB max).")
    route = parse_gpx_bytes(data)
    return {"route": route}


@router.get("/api/trail/suggest")
@limiter.limit("120/minute")
async def trail_suggest(
    request: Request,
    q: str = "",
    user: User = Depends(get_current_user),
):
    """Lightweight typeahead: returns up to 8 trail name+area+id matches for query string `q`."""
    if not q or not q.strip():
        return []
    return suggest_trails(q.strip(), limit=8)


@router.post("/api/trail/match")
@limiter.limit("60/minute")
async def trail_match(request: Request, payload: dict, user: User = Depends(get_current_user)):
    route = payload.get("route") or {}
    name_hint = (payload.get("name_hint") or "").strip()
    # Allow name-only search without route points
    match = match_trail(route, name_hint=name_hint)
    return match


@router.post("/api/checks/weather")
@limiter.limit("60/minute")
async def check_weather(request: Request, payload: dict, user: User = Depends(get_current_user)):
    lat, lng = _require_coords(payload)
    weather = get_weather_summary(
        lat, lng, payload.get("start_date"), payload.get("end_date")
    )
    return weather


@router.post("/api/checks/alerts")
@limiter.limit("60/minute")
async def check_alerts(request: Request, payload: dict, user: User = Depends(get_current_user)):
    """Active NWS hazard alerts — the authoritative safety feed."""
    lat, lng = _require_coords(payload)
    return get_active_alerts(lat, lng)


@router.post("/api/checks/aqi")
@limiter.limit("60/minute")
async def check_aqi(request: Request, payload: dict, user: User = Depends(get_current_user)):
    lat, lng = _require_coords(payload)
    logger.info(f"AQI check for {lat}, {lng}")
    aqi = get_aqi_summary(lat, lng)
    logger.info(f"AQI result: {aqi}")
    return aqi


@router.post("/api/checks/fire")
@limiter.limit("60/minute")
async def check_fire(request: Request, payload: dict, user: User = Depends(get_current_user)):
    lat, lng = _require_coords(payload)
    radius = _bounded_radius(payload, 50.0, 200.0)
    logger.info(f"Fire check for {lat}, {lng} (radius: {radius}mi)")
    fire = get_fire_summary({"midpoint": [lat, lng]}, radius_miles=radius)
    logger.info(f"Fire result: {fire.get('count', 'N/A')} perimeters within {radius}mi")
    return fire


@router.post("/api/checks/snow")
@limiter.limit("60/minute")
async def check_snow(request: Request, payload: dict, user: User = Depends(get_current_user)):
    lat, lng = _require_coords(payload)
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    radius = _bounded_radius(payload, 5.0, 50.0)
    logger.info(f"Snow check for {lat}, {lng} (radius: {radius}mi)")
    snow = get_snow_summary(lat, lng, start_date or "", end_date or "", radius_miles=radius)
    logger.info(f"Snow result: {snow.get('message')}")
    return snow


@router.post("/api/plan")
@limiter.limit("20/minute")
async def plan_trip(
    request: Request,
    file: UploadFile = File(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    name_hint: Optional[str] = Form(None),
    selected_trail_id: Optional[str] = Form(None),
    save: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not file.filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Only GPX uploads are supported for now.")

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="GPX file too large (20 MB max).")
    route = parse_gpx_bytes(data)

    match_result = match_trail(route, name_hint=name_hint or "")
    selected = None
    if selected_trail_id:
        selected = next(
            (c for c in match_result.get("shortlist", []) if c.get("id") == selected_trail_id),
            None,
        )
    if not selected:
        selected = match_result.get("auto_selected")

    midpoint = route.get("midpoint")
    if not midpoint:
        raise HTTPException(status_code=400, detail="Could not compute route midpoint.")

    logger.info(f"Running checks for {midpoint}")

    # Weather is sampled at the route's high point, not its midpoint. Temperature
    # falls roughly 3.5 F per 1000 ft, so a valley reading understates conditions
    # at the pass — the more dangerous place.
    high_point = _highest_route_point(route) or (midpoint[0], midpoint[1])

    weather = get_weather_summary(high_point[0], high_point[1], start_date, end_date)
    alerts = get_active_alerts(high_point[0], high_point[1])
    aqi = get_aqi_summary(midpoint[0], midpoint[1])
    fire = get_fire_summary(route)
    snow = get_snow_summary(midpoint[0], midpoint[1], start_date, end_date, route=route)
    water = get_water_summary(
        midpoint[0], midpoint[1], route_points=(route or {}).get("points")
    )

    checks = {
        "weather": weather,
        "alerts": alerts,
        "aqi": aqi,
        "fire": fire,
        "snow": snow,
        "water": water,
    }
    logger.info(
        "checks complete: "
        + ", ".join(f"{k}={(v or {}).get('status', 'ok' if not (v or {}).get('error') else 'error')}" for k, v in checks.items())
    )

    risk = evaluate_risk(checks)
    report = build_ai_report(route, selected, checks, risk)
    map_layers = build_map_layers(route, fire)

    response = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "route": route,
        "trail_match": match_result,
        "selected_trail": selected,
        "checks": checks,
        "risk": risk,
        "report": report,
        "map_layers": map_layers,
    }

    # Auto-save when authenticated and the client opted in via `save=true`.
    if user and (save or "").lower() in ("1", "true", "yes"):
        try:
            from datetime import date as _date
            sd = _date.fromisoformat(start_date) if start_date else None
            ed = _date.fromisoformat(end_date) if end_date else None
        except ValueError:
            sd = ed = None
        trip_name = (selected or {}).get("name") if selected else None
        trip_name = trip_name or (file.filename or "Untitled trip").rsplit(".", 1)[0]
        trip = TripPlan(
            user_id=user.id,
            name=trip_name[:255],
            start_date=sd,
            end_date=ed,
            route=route,
            selected_trail=selected,
            checks=checks,
            report=report,
        )
        session.add(trip)
        await session.commit()
        await session.refresh(trip)
        try:
            rel = write_gpx(user.id, trip.id, data)
            trip.gpx_path = rel
            await session.commit()
        except Exception as exc:
            logger.warning(f"GPX persist failed for trip {trip.id}: {exc}")
        response["saved_trip_id"] = str(trip.id)

    return response


@router.post("/api/checks/water")
@limiter.limit("60/minute")
async def check_water(request: Request, payload: dict, user: User = Depends(get_current_user)):
    lat, lng = _require_coords(payload)
    radius = _bounded_radius(payload, 0.5, 5.0)
    route_points = payload.get("route_points") or None
    logger.info(f"Water check for {lat}, {lng} (radius: {radius}mi, {len(route_points or [])} route pts)")
    water = get_water_summary(lat, lng, radius_miles=radius, route_points=route_points)
    logger.info(f"Water result: {water.get('message')}")
    return water


@router.post("/api/risk/evaluate")
@limiter.limit("60/minute")
async def risk_evaluate(request: Request, payload: dict, user: User = Depends(get_current_user)):
    """Score an already-fetched set of checks.

    Exists so the browser never computes its own verdict. Two risk engines that
    disagree are worse than one: the same trip previously showed "Caution" or
    "Good to Go" depending only on which flow the user came through.
    """
    checks = payload.get("checks") or {}
    if not isinstance(checks, dict):
        raise HTTPException(status_code=400, detail="checks must be an object")
    risk = evaluate_risk(checks)
    return {
        "risk": risk,
        "report": build_ai_report(
            payload.get("route") or {}, payload.get("selected_trail"), checks, risk
        ),
    }


@router.post("/api/plan/report")
@limiter.limit("10/minute")
async def plan_report(request: Request, payload: dict, user: User = Depends(get_current_user)):
    trail_name = payload.get("trail_name") or "Unknown Trail"
    area = payload.get("area") or ""
    total_miles = float(payload.get("total_miles") or 0)
    trip_type = payload.get("trip_type") or "out-and-back"
    num_days = int(payload.get("num_days") or 1)
    days = payload.get("days") or []
    checks = payload.get("checks") or {}
    logger.info(f"AI report for {trail_name} ({num_days} days) with checks")
    # The brief explains the deterministic verdict; it never produces one.
    risk = evaluate_risk(checks)
    result = generate_report(
        trail_name, area, total_miles, trip_type, num_days, days, checks, risk=risk
    )
    result["risk"] = risk
    return result


NOHRSC_WMS = "https://mapservices.weather.noaa.gov/raster/services/snow/NOHRSC_Snow_Analysis/MapServer/WMSServer"

@router.get("/api/proxy/snow")
@limiter.limit("240/minute")
async def proxy_snow_tile(
    request: Request,
    bbox: str,
    width: int = 256,
    height: int = 256,
    user: User = Depends(get_current_user),
):
    """Proxy NOHRSC WMS tiles to avoid browser CORS restrictions.

    Authenticated and rate limited: without both, this is an open proxy that
    anyone can drive arbitrary outbound requests through.
    """
    # Validate bbox rather than forwarding client text into an outbound request.
    try:
        parts = [float(v) for v in bbox.split(",")]
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be four numbers")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must be 'minX,minY,maxX,maxY'")
    if parts[0] >= parts[2] or parts[1] >= parts[3]:
        raise HTTPException(status_code=400, detail="bbox min must be less than max")
    bbox = ",".join(str(v) for v in parts)
    width = max(1, min(width, 1024))
    height = max(1, min(height, 1024))
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": "0",
        "STYLES": "",
        "FORMAT": "image/png",
        "TRANSPARENT": "TRUE",
        "SRS": "EPSG:3857",
        "BBOX": bbox,
        "WIDTH": width,
        "HEIGHT": height,
    }
    try:
        r = _requests.get(NOHRSC_WMS, params=params, timeout=10, headers={"User-Agent": "OpenTrails/1.0"})
        r.raise_for_status()
        return Response(
            content=r.content,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception:
        return Response(content=b"", media_type="image/png", status_code=204)


# ── Cell coverage tile proxy ──────────────────────────────────────────────────
#
# Carrier tile URLs — fill these in by opening the carrier's coverage map in
# your browser DevTools (Network tab, filter by "tile" or ".png") and copying
# the tile URL pattern. Replace {z}/{x}/{y} placeholders as appropriate.
#
# T-Mobile example pattern (inspect coverage.t-mobile.com to get exact URL):
#   https://howmobileworks.com/coverage/tile/{z}/{x}/{y}.png
#
# AT&T example pattern (inspect att.com/maps/wireless-coverage-map.html):
#   https://oms.att.com/wcs/maps/api/coverage/{z}/{x}/{y}.png
#
# Verizon example pattern (inspect verizon.com/coverage-map/):
#   https://api.verizon.com/coverage/map/tile/{z}/{x}/{y}.png

COVERAGE_TILE_URLS: dict[str, str] = {
    # Swap these for the real tile URL patterns once you capture them from DevTools.
    # The placeholder returns empty so the layer silently shows nothing until configured.
    "tmobile":  "",   # e.g. "https://cdn.coverage.t-mobile.com/{z}/{x}/{y}.png"
    "att":      "",   # e.g. "https://oms.att.com/wcs/maps/coverage/{z}/{x}/{y}.png"
    "verizon":  "",   # e.g. "https://api.verizon.com/coverage-map/tile/{z}/{x}/{y}.png"
}

@router.get("/api/proxy/coverage/{provider}/{z}/{x}/{y}")
@limiter.limit("240/minute")
async def proxy_coverage_tile(
    request: Request,
    provider: str,
    z: int,
    x: int,
    y: int,
    user: User = Depends(get_current_user),
):
    """Proxy carrier cell-coverage raster tiles to avoid CORS.
    Returns 204 (empty) if the provider URL is not yet configured."""
    if not 0 <= z <= 22:
        raise HTTPException(status_code=400, detail="zoom out of range")
    bound = 2 ** z
    if not (0 <= x < bound and 0 <= y < bound):
        raise HTTPException(status_code=400, detail="tile coordinates out of range")
    url_template = COVERAGE_TILE_URLS.get(provider, "")
    if not url_template:
        return Response(content=b"", media_type="image/png", status_code=204)
    url = url_template.format(z=z, x=x, y=y)
    try:
        r = _requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 OpenTrails/1.0"})
        r.raise_for_status()
        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "image/png"),
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception:
        return Response(content=b"", media_type="image/png", status_code=204)
