"""
Planner API endpoints for trip readiness checks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from planner.route_parser import parse_gpx_bytes
from planner.trail_matcher import match_trail
from planner.checks.weather import get_weather_summary
from planner.checks.aqi import get_aqi_summary
from planner.checks.fire import get_fire_summary
from planner.checks.snow import get_snow_summary
from planner.risk_engine import evaluate_risk
from planner.report_ai import build_ai_report
from planner.map_layers import build_map_layers


router = APIRouter()


@router.post("/api/route/parse")
async def parse_route(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Only GPX uploads are supported for now.")
    data = await file.read()
    route = parse_gpx_bytes(data)
    return {"route": route}


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
