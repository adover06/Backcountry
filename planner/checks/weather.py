"""Weather and active hazard alerts from the National Weather Service.

Three things this module is careful about, all of which were previously wrong:

1. **Failure is reported, not hidden.** Every return carries an explicit
   `status` of "ok" or "unavailable". An empty forecast list is no longer
   indistinguishable from a clear one.

2. **Night periods are kept.** NWS alternates day and night periods. Filtering the
   night out hides the overnight low, which for an overnight trip is the number
   that decides your sleeping bag and your hypothermia margin.

3. **The forecast is matched against the trip dates.** NWS reaches about seven
   days. Asking for weather three weeks out previously returned *today's* forecast,
   which the UI then labelled with the future trip dates. Now the response states
   what range it actually covers and whether that reaches the trip.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import requests

from .cache import TTLCache, env_ttl_seconds

WEATHER_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("WEATHER_CACHE_TTL_SECONDS", 1800))
ALERTS_CACHE = TTLCache(ttl_seconds=env_ttl_seconds("ALERTS_CACHE_TTL_SECONDS", 600))

USER_AGENT = "OpenTrails/1.0 (trail conditions; contact via app)"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}

# NWS publishes roughly seven days of forecast periods.
FORECAST_HORIZON_DAYS = 7

# Number of periods to return: 14 covers a week of day/night pairs.
MAX_PERIODS = 14


def _unavailable(message: str, **extra) -> dict:
    return {"status": "unavailable", "message": message, "forecast": [], **extra}


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def get_weather_summary(
    lat: float,
    lng: float,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Forecast periods for a point, with explicit coverage of the trip dates."""
    cache_key = f"{round(lat, 3)}:{round(lng, 3)}"
    cached = WEATHER_CACHE.get(cache_key)

    if cached is None:
        try:
            points = requests.get(
                f"https://api.weather.gov/points/{lat},{lng}",
                timeout=10,
                headers=_HEADERS,
            )
            points.raise_for_status()
            forecast_url = points.json()["properties"]["forecast"]

            forecast = requests.get(forecast_url, timeout=10, headers=_HEADERS)
            forecast.raise_for_status()
            periods = forecast.json()["properties"]["periods"][:MAX_PERIODS]
        except Exception as exc:
            # Not cached: a transient failure should not be pinned for 30 minutes.
            return _unavailable(f"NWS request failed: {exc}")

        cached = {
            "status": "ok",
            "provider": "NWS",
            "forecast": [
                {
                    "name": period.get("name"),
                    "short": period.get("shortForecast"),
                    "detailed": period.get("detailedForecast"),
                    "temp": period.get("temperature"),
                    "temp_unit": period.get("temperatureUnit"),
                    "wind": f"{period.get('windSpeed', '')} {period.get('windDirection', '')}".strip(),
                    # Kept so the UI can show overnight lows instead of hiding them.
                    "is_daytime": period.get("isDaytime"),
                    "start_time": period.get("startTime"),
                    "end_time": period.get("endTime"),
                    "precip_pct": (period.get("probabilityOfPrecipitation") or {}).get("value"),
                }
                for period in periods
            ],
        }
        WEATHER_CACHE.set(cache_key, cached)

    result = dict(cached)
    result.update(_coverage(result["forecast"], start_date, end_date))
    return result


def _coverage(periods: list[dict], start_date: str | None, end_date: str | None) -> dict:
    """Describe what the forecast actually covers relative to the trip."""
    period_dates = [
        _parse_iso_date(period.get("start_time")) for period in periods
    ]
    period_dates = [d for d in period_dates if d]

    info: dict = {
        "covers_from": period_dates[0].isoformat() if period_dates else None,
        "covers_to": period_dates[-1].isoformat() if period_dates else None,
    }

    trip_start = _parse_iso_date(start_date)
    trip_end = _parse_iso_date(end_date) or trip_start
    if not trip_start:
        info["covers_trip_dates"] = None
        return info

    horizon = datetime.now(timezone.utc).date() + timedelta(days=FORECAST_HORIZON_DAYS)
    last_covered = period_dates[-1] if period_dates else None

    covered = bool(last_covered and trip_start <= last_covered)
    info["covers_trip_dates"] = covered
    info["trip_start"] = trip_start.isoformat()
    info["trip_end"] = trip_end.isoformat() if trip_end else None

    if not covered:
        days_out = (trip_start - datetime.now(timezone.utc).date()).days
        info["message"] = (
            f"Trip starts in {days_out} days; NWS forecasts reach about "
            f"{FORECAST_HORIZON_DAYS} days ({horizon.isoformat()}). "
            "No forecast exists for these dates yet."
        )
        # Periods that do not describe the trip must not be shown as if they do.
        info["forecast_applies_to_trip"] = False
    else:
        info["forecast_applies_to_trip"] = True

    return info


def get_active_alerts(lat: float, lng: float) -> dict:
    """Active NWS alerts for a point.

    This is the authoritative hazard feed — Red Flag, Winter Storm, Flash Flood —
    and it was never called before. Its absence is why a trip during a Winter Storm
    Warning could score "go".
    """
    cache_key = f"alerts:{round(lat, 3)}:{round(lng, 3)}"
    cached = ALERTS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = requests.get(
            "https://api.weather.gov/alerts/active",
            params={"point": f"{lat},{lng}"},
            timeout=10,
            headers=_HEADERS,
        )
        response.raise_for_status()
        features = response.json().get("features") or []
    except Exception as exc:
        return {"status": "unavailable", "message": f"NWS alerts failed: {exc}", "alerts": []}

    alerts = []
    for feature in features:
        props = feature.get("properties") or {}
        alerts.append(
            {
                "event": props.get("event"),
                "severity": props.get("severity"),
                "urgency": props.get("urgency"),
                "certainty": props.get("certainty"),
                "headline": props.get("headline"),
                "description": (props.get("description") or "")[:600],
                "onset": props.get("onset"),
                "expires": props.get("expires"),
            }
        )

    result = {"status": "ok", "provider": "NWS", "alerts": alerts, "count": len(alerts)}
    ALERTS_CACHE.set(cache_key, result)
    return result


def daytime_periods(forecast: list[dict]) -> list[dict]:
    return [p for p in forecast if p.get("is_daytime")]


def overnight_periods(forecast: list[dict]) -> list[dict]:
    return [p for p in forecast if p.get("is_daytime") is False]


def coldest_overnight(forecast: list[dict]) -> dict | None:
    """The coldest night in the forecast — the number that sets your bag rating."""
    nights = [p for p in overnight_periods(forecast) if isinstance(p.get("temp"), (int, float))]
    return min(nights, key=lambda p: p["temp"]) if nights else None
