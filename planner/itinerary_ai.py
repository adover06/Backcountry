"""AI situation brief generator using Ollama (OpenAI-compatible endpoint).

Single focused purpose: synthesize all check results (weather, snow, fire, AQI, water)
into structured, actionable output — no terrain narration, no generic hiking prose.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://chinky.gerardconsuelo.com/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _weather_lines(forecast: list, num_days: int) -> str:
    if not forecast:
        return "No forecast available"
    return "; ".join(
        f"Day {i+1}: {p.get('short','')} {p.get('temp','')}°{p.get('temp_unit','')} {p.get('wind','')}"
        for i, p in enumerate(forecast[:num_days])
    )


def _build_prompt(
    trail_name: str,
    area: str,
    total_miles: float,
    trip_type: str,
    num_days: int,
    checks: dict,
) -> str:
    forecast = checks.get("weather", {}).get("forecast", [])
    weather_block = _weather_lines(forecast, num_days)

    snow = checks.get("snow", {})
    snow_depth = snow.get("max_depth_in")
    snow_line = f"{snow_depth} in at highest point" if snow_depth else "None detected"

    fire_feats = checks.get("fire", {}).get("perimeters", {}).get("features", [])
    nearby = [
        f for f in fire_feats
        if (f.get("properties", {}).get("distance_from_midpoint_mi") or 999) <= 5
        and (f.get("properties", {}).get("days_since_update") or 999) <= 365
    ]
    fire_line = (
        f"{len(nearby)} fire perimeter(s) within 5 mi (updated past year)"
        if nearby
        else "None within 5 mi in the past year"
    )

    aqi_obs = checks.get("aqi", {}).get("observations", [])
    aqi_line = (
        f"{aqi_obs[0]['aqi']} — {aqi_obs[0].get('category', '')}"
        if aqi_obs
        else "Unavailable"
    )

    water_line = checks.get("water", {}).get("message", "No data")

    return f"""You are a backcountry trip safety analyst. Output ONLY valid JSON — no prose, no markdown, no code fences.

Trip: {trail_name}, {area}
Distance: {total_miles:.1f} mi ({trip_type}) · {num_days} day{'s' if num_days != 1 else ''}

Current conditions:
  Weather: {weather_block}
  Snow: {snow_line}
  Fire: {fire_line}
  AQI: {aqi_line}
  Water: {water_line}

Output this exact JSON structure:
{{
  "situation_brief": "2-3 sentences synthesizing ALL conditions into a clear go/caution/no-go assessment. Cite specific numbers. Focus entirely on what this hiker needs to know before leaving the trailhead.",
  "gear_adds": ["3-6 specific items directly justified by the conditions above, e.g. \\"gaiters — {snow_depth or 0} in of snow at summit\\""],
  "timing_notes": ["2-4 practical notes about weather windows, early starts, hazard timing, or schedule adjustments based on the forecast"]
}}

Rules: cite actual numbers from the conditions. No generic hiking advice. No terrain descriptions. Only valid JSON, nothing else."""


def _parse_json(raw: str) -> dict | None:
    raw = _strip_think(raw)
    # Try to extract JSON object even if the model added surrounding text
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def generate_report(
    trail_name: str,
    area: str,
    total_miles: float,
    trip_type: str,
    num_days: int,
    days: list[dict],
    checks: dict,
    timeout: int = 180,
) -> dict:
    try:
        from openai import OpenAI
    except ImportError:
        return {"error": "openai package not installed", "sections": None}

    prompt = _build_prompt(trail_name, area, total_miles, trip_type, num_days, checks)
    logger.info(f"Generating situation brief for {trail_name} ({num_days}d)")

    try:
        client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=timeout)
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        raw = response.choices[0].message.content
        parsed = _parse_json(raw)
        if parsed:
            return {"sections": parsed, "model": OLLAMA_MODEL}
        # If JSON parse failed, return raw for debugging
        logger.warning(f"Could not parse JSON from AI response: {raw[:200]}")
        return {"error": "AI response was not valid JSON", "raw": raw[:500], "sections": None}
    except Exception as exc:
        logger.warning(f"Report AI call failed: {exc}")
        return {"error": str(exc), "sections": None}
