"""AI itinerary/report generator using Ollama (OpenAI-compatible endpoint)."""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://chinky.gerardconsuelo.com/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def _weather_lines(forecast: list, num_days: int) -> str:
    if not forecast:
        return "  No forecast available"
    lines = []
    for i, p in enumerate(forecast[:num_days]):
        lines.append(f"  Day {i+1}: {p.get('short', '')} · {p.get('temp', '')} {p.get('temp_unit', '')} · {p.get('wind', '')}")
    return "\n".join(lines)


def build_report_prompt(
    trail_name: str,
    area: str,
    total_miles: float,
    trip_type: str,
    num_days: int,
    days: list[dict],
    checks: dict,
) -> str:
    forecast = checks.get("weather", {}).get("forecast", [])
    weather_block = _weather_lines(forecast, num_days)

    snow = checks.get("snow", {})
    snow_depth = snow.get("max_depth_in")
    snow_line = f"{snow_depth} in at highest point" if snow_depth else "No snow detected"

    fire_feats = checks.get("fire", {}).get("perimeters", {}).get("features", [])
    nearby_fires = [
        f for f in fire_feats
        if (f.get("properties", {}).get("distance_from_midpoint_mi") or 999) <= 5
        and (f.get("properties", {}).get("days_since_update") or 999) <= 365
    ]
    fire_line = f"{len(nearby_fires)} fire perimeter(s) within 5 mi (past year)" if nearby_fires else "No recent fires within 5 mi"

    aqi_obs = checks.get("aqi", {}).get("observations", [])
    aqi_line = f"{aqi_obs[0]['aqi']} ({aqi_obs[0].get('category', '')})" if aqi_obs else "Unknown"

    water = checks.get("water", {})
    water_line = water.get("message", "No data")

    day_lines = "\n".join(
        f"  Day {d['day']}: Mile {d['startMile']} → {d['endMile']} ({d['miles']} mi)"
        for d in days
    )

    return f"""You are writing a concise pre-trip report for a backpacking trip. Output ONLY the sections below, using exactly these headers. No preamble, no extra explanation.

Trail: {trail_name}
Area: {area}
Distance: {total_miles:.1f} mi ({trip_type})
Duration: {num_days} day{'s' if num_days != 1 else ''}

Daily segments:
{day_lines}

Current conditions:
  Weather: {weather_block}
  Snow: {snow_line}
  Fire: {fire_line}
  AQI: {aqi_line}
  Water: {water_line}

Write the report using EXACTLY these section headers (keep the ALL CAPS labels):

OVERVIEW:
[2-3 sentences: overall trip character, terrain type, what makes it special or challenging. Do not repeat the raw numbers.]

CONDITIONS:
[1-2 sentences: translate the weather/fire/snow/AQI into practical advice for the hiker.]

{chr(10).join(f'DAY {d["day"]}:' + chr(10) + '[1-2 sentences: terrain and highlights for this segment. Mention water sourcing if relevant for this day.]' for d in days)}

Keep each section tight. No bullet points. No markdown. Plain prose only."""


def _parse_report(text: str, num_days: int) -> dict:
    """Split the raw AI output into labelled sections."""
    sections: dict = {"overview": "", "conditions": "", "days": []}
    text = _clean(text)

    def extract(label: str, src: str) -> tuple[str, str]:
        pattern = re.compile(rf"{re.escape(label)}\s*:?\s*(.*?)(?=\n[A-Z][A-Z ]+:|$)", re.DOTALL | re.IGNORECASE)
        m = pattern.search(src)
        if m:
            return m.group(1).strip(), src[:m.start()] + src[m.end():]
        return "", src

    sections["overview"], text = extract("OVERVIEW", text)
    sections["conditions"], text = extract("CONDITIONS", text)
    for i in range(1, num_days + 1):
        note, text = extract(f"DAY {i}", text)
        sections["days"].append(note)

    return sections


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

    prompt = build_report_prompt(trail_name, area, total_miles, trip_type, num_days, days, checks)
    logger.info(f"Generating AI report for {trail_name} ({num_days}d)")

    try:
        client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=timeout)
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=1000,
        )
        raw = response.choices[0].message.content
        sections = _parse_report(raw, num_days)
        return {"sections": sections, "model": OLLAMA_MODEL}
    except Exception as exc:
        logger.warning(f"Report AI call failed: {exc}")
        return {"error": str(exc), "sections": None}


# Keep original itinerary function for the Itinerary step (pre-checks, no conditions data)
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

    forecast = checks.get("weather", {}).get("forecast", [])
    weather_block = _weather_lines(forecast, num_days) if forecast else "  No forecast data at this stage"
    day_lines = "\n".join(f"  Day {d['day']}: Mile {d['startMile']} → {d['endMile']} ({d['miles']} mi)" for d in days)

    prompt = f"""You are a practical wilderness trip planner. Write a brief day-by-day trail itinerary.

Trail: {trail_name} · {area}
Distance: {total_miles:.1f} mi ({trip_type}) over {num_days} day{'s' if num_days != 1 else ''}

Segments:
{day_lines}

Weather context:
{weather_block}

One short paragraph per day (2-3 sentences). Focus on terrain, highlights, camp spots, and water. No headers. Plain text only."""

    try:
        client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=timeout)
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=800,
        )
        text = _clean(response.choices[0].message.content)
        return {"text": text, "model": OLLAMA_MODEL}
    except Exception as exc:
        logger.warning(f"Itinerary AI call failed: {exc}")
        return {"error": str(exc), "text": None}
