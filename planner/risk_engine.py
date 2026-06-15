"""Deterministic risk scoring."""

from __future__ import annotations


def evaluate_risk(checks: dict) -> dict:
    reasons = []
    status = "go"

    aqi_obs = (checks.get("aqi") or {}).get("observations") or []
    for obs in aqi_obs:
        aqi = obs.get("aqi") or 0
        if aqi >= 150:
            status = "no-go"
            reasons.append(f"AQI {aqi} ({obs.get('category')})")
        elif aqi >= 100 and status != "no-go":
            status = "caution"
            reasons.append(f"AQI {aqi} ({obs.get('category')})")

    fire = checks.get("fire") or {}
    perimeters = fire.get("perimeters") or {}
    if perimeters.get("features"):
        if status == "go":
            status = "caution"
        reasons.append("Active fire perimeters present in region")

    # Snow: the snow check returns `max_depth_in` (and `max_snowfall_in`), not
    # `depth_in`. Read the real field names and guard against None/unavailable.
    snow = checks.get("snow") or {}
    snow_depth = snow.get("max_depth_in")
    snow_fall = snow.get("max_snowfall_in")
    if isinstance(snow_depth, (int, float)) and snow_depth >= 12 and status != "no-go":
        status = "caution"
        reasons.append(f"Snow depth ~{round(snow_depth)} in at highest point")
    elif isinstance(snow_fall, (int, float)) and snow_fall >= 6 and status != "no-go":
        status = "caution"
        reasons.append(f"Forecast snowfall ~{round(snow_fall)} in")

    return {
        "status": status,
        "reasons": reasons,
    }
