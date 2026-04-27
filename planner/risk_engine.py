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

    if checks.get("fire", {}).get("perimeters"):
        if status == "go":
            status = "caution"
        reasons.append("Active fire perimeters present in region")

    if checks.get("snow", {}).get("depth_in"):
        depth = checks["snow"]["depth_in"]
        if depth >= 12 and status != "no-go":
            status = "caution"
            reasons.append(f"Snow depth ~{depth} in")

    return {
        "status": status,
        "reasons": reasons,
    }
