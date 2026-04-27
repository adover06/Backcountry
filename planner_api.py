"""
Planner API endpoints for trip readiness checks.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

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

    weather = get_weather_summary(midpoint[0], midpoint[1])
    aqi = get_aqi_summary(midpoint[0], midpoint[1])
    fire = get_fire_summary(route)
    snow = get_snow_summary(midpoint[0], midpoint[1], start_date, end_date)

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
