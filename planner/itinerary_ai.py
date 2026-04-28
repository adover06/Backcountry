"""AI itinerary generator using Ollama (OpenAI-compatible endpoint)."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://100.86.195.79:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3")


def build_itinerary_prompt(
    trail_name: str,
    area: str,
    total_miles: float,
    trip_type: str,
    num_days: int,
    days: list[dict],
    checks: dict,
) -> str:
    weather_summary = ""
    forecast = checks.get("weather", {}).get("forecast", [])
    if forecast:
        lines = [f"  Day {i+1}: {p.get('short', '')} {p.get('temp', '')} {p.get('temp_unit', '')}" for i, p in enumerate(forecast[:num_days])]
        weather_summary = "\nWeather forecast:\n" + "\n".join(lines)

    snow_msg = checks.get("snow", {}).get("message", "")
    fire_count = len(checks.get("fire", {}).get("perimeters", {}).get("features", []))
    water_count = checks.get("water", {}).get("count", 0)
    aqi_obs = checks.get("aqi", {}).get("observations", [])
    aqi_val = aqi_obs[0].get("aqi") if aqi_obs else None

    conditions = []
    if snow_msg:
        conditions.append(f"Snow: {snow_msg}")
    if fire_count:
        conditions.append(f"Fire: {fire_count} active perimeter(s) in the region")
    if aqi_val is not None:
        conditions.append(f"AQI: {aqi_val}")
    if water_count is not None:
        conditions.append(f"Water sources: {water_count} within 0.5 mi of trail")

    day_lines = "\n".join(
        f"  Day {d['day']}: Mile {d['startMile']} → {d['endMile']} ({d['miles']} mi)"
        for d in days
    )

    return f"""You are a practical wilderness trip planner. Generate a concise, helpful day-by-day trail itinerary.

Trail: {trail_name}
Area: {area}
Total distance: {total_miles:.1f} mi ({trip_type})
Duration: {num_days} day{'s' if num_days != 1 else ''}

Daily segments:
{day_lines}
{weather_summary}
Conditions:
{chr(10).join('  ' + c for c in conditions) if conditions else '  No notable hazards'}

Write a brief itinerary with one paragraph per day (2-3 sentences). Focus on:
- Terrain and highlights for each day's segment
- Where to camp each night
- Any weather or condition notes relevant to that day
- Water sourcing tips if relevant

Keep it practical and concise. Do not repeat the mileage already listed. Output plain text only, no markdown headers or bullet points."""


def generate_itinerary(
    trail_name: str,
    area: str,
    total_miles: float,
    trip_type: str,
    num_days: int,
    days: list[dict],
    checks: dict,
    timeout: int = 60,
) -> dict:
    try:
        from openai import OpenAI
    except ImportError:
        return {"error": "openai package not installed", "text": None}

    prompt = build_itinerary_prompt(trail_name, area, total_miles, trip_type, num_days, days, checks)

    try:
        client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=timeout)
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=800,
        )
        text = response.choices[0].message.content.strip()
        # Strip <think>...</think> blocks that qwen3 sometimes emits
        import re
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return {"text": text, "model": OLLAMA_MODEL}
    except Exception as exc:
        logger.warning(f"Itinerary AI call failed: {exc}")
        return {"error": str(exc), "text": None}
