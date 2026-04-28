"""
Planner API endpoints for trip readiness checks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import requests as _requests

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from planner.route_parser import parse_gpx_bytes
from planner.trail_matcher import match_trail, suggest_trails
from planner.checks.weather import get_weather_summary
from planner.checks.aqi import get_aqi_summary
from planner.checks.fire import get_fire_summary
from planner.checks.snow import get_snow_summary
from planner.checks.water import get_water_summary
from planner.risk_engine import evaluate_risk
from planner.report_ai import build_ai_report
from planner.map_layers import build_map_layers
from planner.itinerary_ai import generate_itinerary


router = APIRouter()


@router.post("/api/route/parse")
async def parse_route(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Only GPX uploads are supported for now.")
    data = await file.read()
    route = parse_gpx_bytes(data)
    return {"route": route}


@router.get("/api/trail/suggest")
async def trail_suggest(q: str = ""):
    """Lightweight typeahead: returns up to 8 trail name+area+id matches for query string `q`."""
    if not q or not q.strip():
        return []
    return suggest_trails(q.strip(), limit=8)


@router.post("/api/trail/match")
async def trail_match(payload: dict):
    route = payload.get("route") or {}
    name_hint = (payload.get("name_hint") or "").strip()
    # Allow name-only search without route points
    match = match_trail(route, name_hint=name_hint)
    return match


@router.post("/api/checks/weather")
async def check_weather(payload: dict):
    lat = payload.get("lat")
    lng = payload.get("lng")
    if not lat or not lng:
        raise HTTPException(status_code=400, detail="lat and lng required")
    logger.info(f"Weather check for {lat}, {lng}")
    weather = get_weather_summary(lat, lng)
    logger.info(f"Weather result: {weather}")
    return weather


@router.post("/api/checks/aqi")
async def check_aqi(payload: dict):
    lat = payload.get("lat")
    lng = payload.get("lng")
    if not lat or not lng:
        raise HTTPException(status_code=400, detail="lat and lng required")
    logger.info(f"AQI check for {lat}, {lng}")
    aqi = get_aqi_summary(lat, lng)
    logger.info(f"AQI result: {aqi}")
    return aqi


@router.post("/api/checks/fire")
async def check_fire(payload: dict):
    lat = payload.get("lat")
    lng = payload.get("lng")
    radius = payload.get("radius", 50.0)
    if not lat or not lng:
        raise HTTPException(status_code=400, detail="lat and lng required")
    logger.info(f"Fire check for {lat}, {lng} (radius: {radius}mi)")
    fire = get_fire_summary({"midpoint": [lat, lng]}, radius_miles=radius)
    logger.info(f"Fire result: {fire.get('count', 'N/A')} perimeters within {radius}mi")
    return fire


@router.post("/api/checks/snow")
async def check_snow(payload: dict):
    lat = payload.get("lat")
    lng = payload.get("lng")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    radius = payload.get("radius", 5.0)
    if not lat or not lng:
        raise HTTPException(status_code=400, detail="lat and lng required")
    logger.info(f"Snow check for {lat}, {lng} (radius: {radius}mi)")
    snow = get_snow_summary(lat, lng, start_date or "", end_date or "", radius_miles=radius)
    logger.info(f"Snow result: {snow.get('message')}")
    return snow


@router.post("/api/plan")
async def plan_trip(
    file: UploadFile = File(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    name_hint: Optional[str] = Form(None),
    selected_trail_id: Optional[str] = Form(None),
):
    if not file.filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Only GPX uploads are supported for now.")

    data = await file.read()
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
    
    logger.info("Fetching weather...")
    weather = get_weather_summary(midpoint[0], midpoint[1])
    logger.info(f"Weather done: {list(weather.keys())}")
    
    logger.info("Fetching AQI...")
    aqi = get_aqi_summary(midpoint[0], midpoint[1])
    logger.info(f"AQI done: {list(aqi.keys())}")
    
    logger.info("Fetching fire...")
    fire = get_fire_summary(route)
    logger.info(f"Fire done: {list(fire.keys())}")
    
    logger.info("Fetching snow...")
    snow = get_snow_summary(midpoint[0], midpoint[1], start_date, end_date, route=route)
    logger.info(f"Snow done: {list(snow.keys())}")

    checks = {
        "weather": weather,
        "aqi": aqi,
        "fire": fire,
        "snow": snow,
    }

    risk = evaluate_risk(checks)
    report = build_ai_report(route, selected, checks, risk)
    map_layers = build_map_layers(route, fire)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "route": route,
        "trail_match": match_result,
        "selected_trail": selected,
        "checks": checks,
        "risk": risk,
        "report": report,
        "map_layers": map_layers,
    }


@router.post("/api/checks/water")
async def check_water(payload: dict):
    lat = payload.get("lat")
    lng = payload.get("lng")
    radius = payload.get("radius", 0.5)
    route_points = payload.get("route_points") or None
    if not lat or not lng:
        raise HTTPException(status_code=400, detail="lat and lng required")
    logger.info(f"Water check for {lat}, {lng} (radius: {radius}mi, {len(route_points or [])} route pts)")
    water = get_water_summary(lat, lng, radius_miles=radius, route_points=route_points)
    logger.info(f"Water result: {water.get('message')}")
    return water


@router.post("/api/plan/itinerary")
async def plan_itinerary(payload: dict):
    trail_name = payload.get("trail_name") or "Unknown Trail"
    area = payload.get("area") or ""
    total_miles = float(payload.get("total_miles") or 0)
    trip_type = payload.get("trip_type") or "out-and-back"
    num_days = int(payload.get("num_days") or 1)
    days = payload.get("days") or []
    checks = payload.get("checks") or {}
    logger.info(f"AI itinerary for {trail_name} ({num_days} days, {total_miles} mi)")
    result = generate_itinerary(trail_name, area, total_miles, trip_type, num_days, days, checks)
    return result


NOHRSC_WMS = "https://mapservices.weather.noaa.gov/raster/services/snow/NOHRSC_Snow_Analysis/MapServer/WMSServer"

@router.get("/api/proxy/snow")
async def proxy_snow_tile(bbox: str, width: int = 256, height: int = 256):
    """Proxy NOHRSC WMS tiles to avoid browser CORS restrictions."""
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
        r = _requests.get(NOHRSC_WMS, params=params, timeout=10, headers={"User-Agent": "BackcountryPlanner/1.0"})
        r.raise_for_status()
        return Response(
            content=r.content,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception:
        return Response(content=b"", media_type="image/png", status_code=204)
