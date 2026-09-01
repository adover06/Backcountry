"""Deterministic risk scoring.

The single most important property of this module: **a check that failed is not a
check that passed.** Every input is classified as ok / unavailable / missing, and any
unavailable input forces an `incomplete` status that must never render as green.

This is the only risk engine. The frontend renders what this returns; it does not
compute its own verdict, because two engines that disagree are worse than none.

Status values:
    no-go      a hard hazard threshold was crossed
    caution    an elevated but non-disqualifying condition
    incomplete one or more checks could not be evaluated
    go         every check ran and none raised a concern
"""

from __future__ import annotations

import re
from typing import Any

# ── Thresholds ────────────────────────────────────────────────────────────────

AQI_NO_GO = 150
AQI_CAUTION = 100

SNOW_DEPTH_CAUTION_IN = 12.0
SNOWFALL_CAUTION_IN = 6.0

# Fire perimeters are only relevant when close and recent. The old default of 50 mi
# and 10 years flagged decade-old burn scars as "active fire", which trained the
# user to ignore the warning entirely.
FIRE_RADIUS_MI = 10.0
FIRE_RECENT_DAYS = 30

WIND_CAUTION_MPH = 25
WIND_NO_GO_MPH = 40
LOW_TEMP_CAUTION_F = 32
LOW_TEMP_NO_GO_F = 15
HIGH_TEMP_CAUTION_F = 95
HIGH_TEMP_NO_GO_F = 105

# NWS shortForecast phrases that matter. The previous regex missed every winter
# hazard: "Blizzard Conditions" scored as Good.
SEVERE_PATTERNS = re.compile(
    r"blizzard|ice storm|freezing rain|freezing drizzle|heavy snow|"
    r"severe|tornado|hurricane|damaging wind|high wind|hail",
    re.IGNORECASE,
)
CAUTION_PATTERNS = re.compile(
    r"thunder|snow|sleet|wintry|freezing|shower|rain|drizzle|fog|smoke|"
    r"frost|ice|windy|breezy|dust",
    re.IGNORECASE,
)

# Alert headlines that should stop a trip outright.
ALERT_NO_GO = re.compile(
    r"warning", re.IGNORECASE
)
ALERT_CAUTION = re.compile(
    r"watch|advisory|statement", re.IGNORECASE
)


def _severity_rank(status: str) -> int:
    return {"go": 0, "incomplete": 1, "caution": 2, "no-go": 3}.get(status, 0)


class _Verdict:
    """Accumulates reasons and keeps the most severe status seen."""

    def __init__(self) -> None:
        self.status = "go"
        self.reasons: list[dict] = []
        self.unavailable: list[str] = []

    def raise_to(self, status: str, check: str, message: str, detail: Any = None) -> None:
        if _severity_rank(status) > _severity_rank(self.status):
            self.status = status
        self.reasons.append(
            {"check": check, "severity": status, "message": message, "detail": detail}
        )

    def mark_unavailable(self, check: str, message: str) -> None:
        self.unavailable.append(check)
        if _severity_rank("incomplete") > _severity_rank(self.status):
            self.status = "incomplete"
        self.reasons.append(
            {"check": check, "severity": "incomplete", "message": message, "detail": None}
        )


def _check_failed(payload: Any) -> str | None:
    """Return an error message when a check payload represents a failure."""
    if payload is None:
        return "check did not run"
    if not isinstance(payload, dict):
        return "malformed check result"
    if payload.get("status") == "unavailable":
        return payload.get("message") or "data source unavailable"
    if payload.get("error"):
        return str(payload["error"])
    return None


def _parse_wind_mph(wind: Any) -> float | None:
    """Pull the highest number out of an NWS wind string like '10 to 20 mph NW'."""
    if not wind:
        return None
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(wind))]
    return max(numbers) if numbers else None


# ── Individual checks ─────────────────────────────────────────────────────────


def _evaluate_aqi(checks: dict, verdict: _Verdict) -> None:
    payload = checks.get("aqi")
    failure = _check_failed(payload)
    if failure:
        verdict.mark_unavailable("aqi", f"Air quality unavailable ({failure})")
        return

    observations = payload.get("observations") or []
    if not observations:
        # AirNow returns nothing when no monitor is in range. That is not clean air.
        verdict.mark_unavailable("aqi", "No air quality monitor within range")
        return

    worst = max(observations, key=lambda o: o.get("aqi") or 0)
    aqi = worst.get("aqi") or 0
    label = f"AQI {aqi} ({worst.get('category') or 'unknown'})"
    if aqi >= AQI_NO_GO:
        verdict.raise_to("no-go", "aqi", label, aqi)
    elif aqi >= AQI_CAUTION:
        verdict.raise_to("caution", "aqi", label, aqi)


def _evaluate_fire(checks: dict, verdict: _Verdict) -> None:
    payload = checks.get("fire")
    failure = _check_failed(payload)
    if failure:
        verdict.mark_unavailable("fire", f"Fire perimeters unavailable ({failure})")
        return

    perimeters = payload.get("perimeters") or {}
    features = perimeters.get("features")
    if features is None:
        verdict.mark_unavailable("fire", "Fire perimeter data missing")
        return

    if payload.get("truncated"):
        # The upstream feed capped the response, so absence proves nothing.
        verdict.mark_unavailable("fire", "Fire feed truncated — coverage incomplete")

    relevant = []
    for feature in features:
        props = feature.get("properties") or {}
        distance = props.get("distance_mi")
        days = props.get("days_since_update")
        # Unknown distance or age is treated as potentially relevant, never filtered
        # away — dropping a fire because its metadata is incomplete is the wrong
        # direction to fail in.
        near = distance is None or distance <= FIRE_RADIUS_MI
        recent = days is None or days <= FIRE_RECENT_DAYS
        if near and recent:
            relevant.append(props)

    if relevant:
        nearest = min(
            (p for p in relevant),
            key=lambda p: p.get("distance_mi") if p.get("distance_mi") is not None else 1e9,
        )
        distance = nearest.get("distance_mi")
        where = f"{distance} mi away" if distance is not None else "distance unknown"
        verdict.raise_to(
            "caution",
            "fire",
            f"{len(relevant)} active fire perimeter(s) nearby — closest {where}",
            len(relevant),
        )


def _evaluate_snow(checks: dict, verdict: _Verdict) -> None:
    payload = checks.get("snow")
    failure = _check_failed(payload)
    if failure:
        verdict.mark_unavailable("snow", f"Snow data unavailable ({failure})")
        return

    depth = payload.get("max_depth_in")
    snowfall = payload.get("max_snowfall_in")

    if depth is None and snowfall is None:
        verdict.mark_unavailable("snow", "Snow depth not reported for these dates")
        return

    if isinstance(depth, (int, float)) and depth >= SNOW_DEPTH_CAUTION_IN:
        verdict.raise_to(
            "caution", "snow", f"Snow depth ~{round(depth)} in at the high point", depth
        )
    if isinstance(snowfall, (int, float)) and snowfall >= SNOWFALL_CAUTION_IN:
        verdict.raise_to(
            "caution", "snow", f"Forecast snowfall ~{round(snowfall)} in", snowfall
        )


def _evaluate_weather(checks: dict, verdict: _Verdict) -> None:
    """Weather was previously absent from scoring entirely.

    A forecast of 8 F with 45 mph wind produced a green GO, because nothing here
    looked at it.
    """
    payload = checks.get("weather")
    failure = _check_failed(payload)
    if failure:
        verdict.mark_unavailable("weather", f"Forecast unavailable ({failure})")
        return

    periods = payload.get("forecast") or []
    if not periods:
        verdict.mark_unavailable("weather", "No forecast returned")
        return

    if payload.get("covers_trip_dates") is False:
        verdict.mark_unavailable(
            "weather", "Forecast does not reach the trip dates (NWS covers ~7 days)"
        )

    for period in periods:
        name = period.get("name") or "forecast"
        short = period.get("short") or ""
        temp = period.get("temp")
        wind = _parse_wind_mph(period.get("wind"))

        if SEVERE_PATTERNS.search(short):
            verdict.raise_to("no-go", "weather", f"{name}: {short}", short)
        elif CAUTION_PATTERNS.search(short):
            verdict.raise_to("caution", "weather", f"{name}: {short}", short)

        if isinstance(temp, (int, float)):
            if temp <= LOW_TEMP_NO_GO_F:
                verdict.raise_to("no-go", "weather", f"{name}: {temp}F", temp)
            elif temp <= LOW_TEMP_CAUTION_F:
                verdict.raise_to("caution", "weather", f"{name}: {temp}F", temp)
            elif temp >= HIGH_TEMP_NO_GO_F:
                verdict.raise_to("no-go", "weather", f"{name}: {temp}F", temp)
            elif temp >= HIGH_TEMP_CAUTION_F:
                verdict.raise_to("caution", "weather", f"{name}: {temp}F", temp)

        if wind is not None:
            if wind >= WIND_NO_GO_MPH:
                verdict.raise_to("no-go", "weather", f"{name}: wind to {wind:.0f} mph", wind)
            elif wind >= WIND_CAUTION_MPH:
                verdict.raise_to("caution", "weather", f"{name}: wind to {wind:.0f} mph", wind)


def _evaluate_alerts(checks: dict, verdict: _Verdict) -> None:
    """Active NWS alerts — the authoritative hazard feed, previously never called."""
    payload = checks.get("alerts")
    if payload is None:
        verdict.mark_unavailable("alerts", "NWS active alerts not checked")
        return

    failure = _check_failed(payload)
    if failure:
        verdict.mark_unavailable("alerts", f"NWS alerts unavailable ({failure})")
        return

    for alert in payload.get("alerts") or []:
        event = alert.get("event") or "Alert"
        severity = (alert.get("severity") or "").lower()
        if severity in ("extreme", "severe") or ALERT_NO_GO.search(event):
            verdict.raise_to("no-go", "alerts", f"NWS {event}", event)
        elif ALERT_CAUTION.search(event):
            verdict.raise_to("caution", "alerts", f"NWS {event}", event)


def _evaluate_water(checks: dict, verdict: _Verdict) -> None:
    """Water is advisory only.

    Mapped water is not flowing water — an OSM creek can be dry in September — so
    this never produces a green signal, only a caution when nothing was found.
    """
    payload = checks.get("water")
    if payload is None:
        return  # water is optional; silence here is not a failure

    failure = _check_failed(payload)
    if failure:
        verdict.mark_unavailable("water", f"Water data unavailable ({failure})")
        return

    if payload.get("count") == 0:
        verdict.raise_to(
            "caution", "water", "No mapped water sources near the route — plan to carry", 0
        )


_EVALUATORS = (
    _evaluate_weather,
    _evaluate_alerts,
    _evaluate_aqi,
    _evaluate_fire,
    _evaluate_snow,
    _evaluate_water,
)


def evaluate_risk(checks: dict) -> dict:
    """Score a set of check results into a single status with explained reasons."""
    verdict = _Verdict()
    checks = checks or {}

    for evaluate in _EVALUATORS:
        evaluate(checks, verdict)

    # Sort reasons most severe first so the UI leads with what matters.
    verdict.reasons.sort(key=lambda r: -_severity_rank(r["severity"]))

    return {
        "status": verdict.status,
        "reasons": verdict.reasons,
        # Flat strings for legacy consumers that expected a list of sentences.
        "reason_text": [r["message"] for r in verdict.reasons],
        "unavailable_checks": verdict.unavailable,
        "complete": not verdict.unavailable,
        "summary": _summary(verdict),
    }


def _summary(verdict: _Verdict) -> str:
    if verdict.status == "no-go":
        return "Conditions cross a hard threshold. Do not go."
    if verdict.status == "caution":
        return "Go prepared — specific conditions need attention."
    if verdict.status == "incomplete":
        missing = ", ".join(verdict.unavailable)
        return f"Cannot give a verdict: {missing} could not be checked."
    return "All checks ran and none raised a concern."
