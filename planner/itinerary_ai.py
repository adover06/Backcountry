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

# Provider selection:
#   - If OPENAI_API_KEY is set, talk to OpenAI directly (model from LLM_MODEL).
#   - Otherwise fall back to an OpenAI-compatible endpoint (e.g. a local Ollama).
# Both paths use the `openai` SDK; only base_url / api_key / model differ.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# No third-party host default: trip data should not be sent anywhere the operator
# did not explicitly configure. Unset means the brief is skipped, not silently
# shipped to someone else's server.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
# A 4B model previously wrote the go/no-go assessment. The brief is now explanatory
# only, but the default is still a model capable of following the constraints.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def _client_and_model(timeout: int):
    """Return (OpenAI client, model name) for whichever provider is configured."""
    from openai import OpenAI

    if OPENAI_API_KEY:
        # Real OpenAI: default base_url (api.openai.com).
        return OpenAI(api_key=OPENAI_API_KEY, timeout=timeout), LLM_MODEL
    if not OLLAMA_BASE_URL:
        raise RuntimeError(
            "No LLM configured. Set OPENAI_API_KEY, or OLLAMA_BASE_URL for a "
            "self-hosted OpenAI-compatible endpoint."
        )
    return OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=timeout), OLLAMA_MODEL


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _weather_lines(forecast: list, num_days: int) -> str:
    """Render forecast periods using their own NWS labels.

    NWS alternates daytime and overnight periods. Indexing them as "Day 1, Day 2,
    Day 3" labelled every other period as a day, so overnight lows were presented
    to the model as daytime highs.
    """
    if not forecast:
        return "NO FORECAST AVAILABLE — do not claim any weather conditions"

    lines = []
    for period in forecast[: num_days * 2]:
        name = period.get("name") or "Period"
        temp = period.get("temp")
        unit = period.get("temp_unit") or "F"
        temp_text = f"{temp}°{unit}" if temp is not None else "temp unknown"
        lines.append(f"{name}: {period.get('short','')} {temp_text} {period.get('wind','')}".strip())
    return "; ".join(lines)


def _build_prompt(
    trail_name: str,
    area: str,
    total_miles: float,
    trip_type: str,
    num_days: int,
    checks: dict,
    risk: dict | None = None,
) -> str:
    forecast = checks.get("weather", {}).get("forecast", [])
    weather_block = _weather_lines(forecast, num_days)

    snow = checks.get("snow", {})
    snow_depth = snow.get("max_depth_in")
    if snow.get("status") == "unavailable" or snow.get("error") or snow_depth is None:
        # Previously a failed snow check rendered as "None detected", stating a
        # safety fact that had never been measured.
        snow_line = "UNAVAILABLE — snow could not be checked, do not claim it is clear"
    elif snow_depth >= 1:
        snow_line = f"{snow_depth} in at highest point"
    else:
        snow_line = "measured, none present"

    fire = checks.get("fire") or {}
    if fire.get("status") == "unavailable" or fire.get("error"):
        fire_line = "UNAVAILABLE — fire perimeters could not be checked"
    else:
        fire_feats = ((fire.get("perimeters") or {}).get("features")) or []
        # Unknown distance or age is kept, not coerced to 999 and filtered out.
        nearby = [
            f for f in fire_feats
            if (lambda d, days: (d is None or d <= 5) and (days is None or days <= 60))(
                (f.get("properties") or {}).get("distance_mi"),
                (f.get("properties") or {}).get("days_since_update"),
            )
        ]
        fire_line = (
            f"{len(nearby)} active fire perimeter(s) within 5 mi"
            if nearby
            else "none within 5 mi"
        )

    aqi_obs = (checks.get("aqi") or {}).get("observations") or []
    aqi_line = (
        f"{aqi_obs[0]['aqi']} — {aqi_obs[0].get('category', '')}"
        if aqi_obs
        else "UNAVAILABLE — no monitor in range, this is not a clean-air reading"
    )

    water_line = checks.get("water", {}).get("message", "No data")

    risk = risk or {}
    status = risk.get("status", "unknown")
    reasons = "; ".join(r.get("message", "") for r in (risk.get("reasons") or [])) or "none recorded"
    missing = ", ".join(risk.get("unavailable_checks") or []) or "none"

    return f"""You are a backcountry conditions writer. Output ONLY valid JSON — no prose, no markdown, no code fences.

The go/no-go decision has ALREADY been made by a deterministic risk engine. Your job is
to EXPLAIN that verdict in plain language. You must NOT reach your own verdict, and you
must NOT contradict, soften, or upgrade the status below.

VERDICT (authoritative, do not change): {status}
Reasons the engine gave: {reasons}
Checks that could NOT be run: {missing}

Trip: {trail_name}, {area}
Distance: {total_miles:.1f} mi ({trip_type}) · {num_days} day{'s' if num_days != 1 else ''}

Conditions:
  Weather: {weather_block}
  Snow: {snow_line}
  Fire: {fire_line}
  AQI: {aqi_line}
  Water: {water_line}

CRITICAL RULES:
- Any value marked UNAVAILABLE means that check FAILED. Never state or imply that an
  unavailable condition is clear, safe, fine, or absent. Say it is unknown.
- Cite only numbers that appear above. Never invent a temperature, depth, or distance.
- If the verdict is "incomplete", your brief must lead with what could not be checked.

Output this exact JSON structure:
{{
  "situation_brief": "2-3 sentences explaining the {status} verdict, citing the specific numbers above. If checks are unavailable, say so explicitly.",
  "gear_adds": ["3-6 items justified by the conditions above; cite the number that justifies each"],
  "timing_notes": ["2-4 notes on weather windows, early starts, or hazard timing based only on the forecast above"],
  "unknowns": ["one entry for each check that could not be run, naming what is not known"]
}}

Only valid JSON, nothing else."""


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
    risk: dict | None = None,
    timeout: int = 60,
) -> dict:
    try:
        client, model = _client_and_model(timeout)
    except ImportError:
        return {"error": "openai package not installed", "sections": None}
    except RuntimeError as exc:
        return {"error": str(exc), "sections": None}

    prompt = _build_prompt(trail_name, area, total_miles, trip_type, num_days, checks, risk)
    logger.info(f"Generating situation brief for {trail_name} ({num_days}d) via {model}")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        raw = response.choices[0].message.content
        parsed = _parse_json(raw)
        if parsed:
            return {"sections": parsed, "model": model}
        # If JSON parse failed, return raw for debugging
        logger.warning(f"Could not parse JSON from AI response: {raw[:200]}")
        return {"error": "AI response was not valid JSON", "raw": raw[:500], "sections": None}
    except Exception as exc:
        logger.warning(f"Report AI call failed: {exc}")
        return {"error": str(exc), "sections": None}
