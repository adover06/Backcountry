"""Weather summary using NWS."""

from __future__ import annotations

import requests


def get_weather_summary(lat: float, lng: float) -> dict:
    try:
        point_url = f"https://api.weather.gov/points/{lat},{lng}"
        r = requests.get(point_url, timeout=8, headers={"User-Agent": "trip-planner/1.0"})
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]

        r2 = requests.get(forecast_url, timeout=8, headers={"User-Agent": "trip-planner/1.0"})
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"][:6]

        summary = {
            "forecast": [
                {
                    "name": p["name"],
                    "short": p["shortForecast"],
                    "temp": p["temperature"],
                    "temp_unit": p["temperatureUnit"],
                    "wind": f"{p['windSpeed']} {p['windDirection']}",
                }
                for p in periods
            ]
        }
        return summary
    except Exception as e:
        return {"error": str(e), "forecast": []}
