"""
Tool functions available to the agent.

Weather, AQI, and permits are stubbed — see TODO comments for each.
"""

import json
import requests
from data import get_trails, find_by_name


# ─── Preference Tracker ───────────────────────────────────────────────────────

def set_preferences(
    region: str = "",
    difficulty: str = "",
    min_miles: float = 0,
    max_miles: float = 0,
    features: list[str] = [],
    route_type: str = "",
    trip_days: int = 0,
) -> str:
    """
    Record trip preferences as the user reveals them during conversation.
    Call this every time the user mentions a new preference — region, difficulty,
    distance, features, route type, or trip length. Partial updates are fine;
    only pass the fields the user just revealed.

    Args:
        region:     Area or park name (e.g. 'Yosemite', 'Sierra Nevada').
        difficulty: 'easy', 'moderate', 'hard', or 'very hard'.
        min_miles:  Minimum trip distance in miles (0 = not specified).
        max_miles:  Maximum trip distance in miles (0 = not specified).
        features:   Desired features e.g. ['views', 'lake', 'waterfall'].
        route_type: 'loop', 'out-and-back', or 'point-to-point'.
        trip_days:  Number of days for the trip (0 = not specified).
    """
    prefs = {}
    if region:      prefs["region"] = region
    if difficulty:  prefs["difficulty"] = difficulty
    if min_miles:   prefs["min_miles"] = min_miles
    if max_miles:   prefs["max_miles"] = max_miles
    if features:    prefs["features"] = features
    if route_type:  prefs["route_type"] = route_type
    if trip_days:   prefs["trip_days"] = trip_days

    import json
    return f"PREFS_UPDATED: {json.dumps(prefs)}"


# ─── Trail Search ─────────────────────────────────────────────────────────────

def search_trails(
    region: str = "",
    difficulty: str = "",
    min_miles: float = 0,
    max_miles: float = 999,
    min_elev_ft: int = 0,
    max_elev_ft: int = 99999,
    features: list[str] = [],
    route_type: str = "",
    top_n: int = 3,
) -> str:
    """
    Filter backpacking trails and return the top matches.

    Args:
        region:      Area name or city to filter by (e.g. 'Yosemite', 'Sierra Nevada').
        difficulty:  'easy', 'moderate', 'hard', or 'very hard'.
        min_miles:   Minimum trail length in miles.
        max_miles:   Maximum trail length in miles.
        min_elev_ft: Minimum elevation gain in feet.
        max_elev_ft: Maximum elevation gain in feet.
        features:    List of desired features e.g. ['views', 'water', 'camping'].
        route_type:  'loop', 'out-and-back', or 'point-to-point'.
        top_n:       Number of results to return (default 8).
    """
    trails = get_trails()

    if region:
        region_lower = region.lower()
        trails = [
            t for t in trails
            if region_lower in t["area"].lower() or region_lower in t["city"].lower()
        ]

    if difficulty:
        trails = [t for t in trails if t["difficulty"] == difficulty.lower()]

    trails = [
        t for t in trails
        if min_miles <= t["length_miles"] <= max_miles
        and min_elev_ft <= t["elev_gain_ft"] <= max_elev_ft
    ]

    if route_type:
        trails = [t for t in trails if t["route_type"] == route_type.lower()]

    if features:
        features_lower = [f.lower() for f in features]
        trails = sorted(
            trails,
            key=lambda t: sum(1 for f in features_lower if f in t["features"]),
            reverse=True,
        )

    # Sort by rating × log(reviews) as a quality score
    import math
    trails = sorted(
        trails,
        key=lambda t: t["avg_rating"] * math.log1p(t["num_reviews"]),
        reverse=True,
    )

    results = trails[:top_n]
    if not results:
        return "No trails found matching those criteria. Try broadening the filters."

    lines = [f"Found {len(results)} trails (showing top {min(top_n, len(results))}):\n"]
    for t in results:
        lines.append(
            f"• {t['name']} ({t['area']})\n"
            f"  {t['length_miles']} mi | +{t['elev_gain_ft']} ft | {t['difficulty']} | "
            f"{t['route_type']} | ★ {t['avg_rating']} ({t['num_reviews']} reviews)\n"
            f"  Features: {', '.join(t['features'][:6]) or 'none listed'}\n"
            f"  Coords: {t['lat']}, {t['lng']}\n"
        )
    return "\n".join(lines)


def get_trail_details(name: str) -> str:
    """
    Get full details for a specific trail by name.

    Args:
        name: Full or partial trail name.
    """
    trail = find_by_name(name)
    if not trail:
        return f"No trail found matching '{name}'."
    return json.dumps(trail, indent=2)


# ─── Weather (NWS — no key required) ──────────────────────────────────────────

def get_weather(lat: float, lng: float) -> str:
    """
    Get the 7-day forecast for a trail's coordinates using the free NWS API.

    Args:
        lat: Latitude of the trail.
        lng: Longitude of the trail.
    """
    try:
        # Step 1: get grid point
        point_url = f"https://api.weather.gov/points/{lat},{lng}"
        r = requests.get(point_url, timeout=8, headers={"User-Agent": "trails-agent/1.0"})
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]

        # Step 2: get forecast
        r2 = requests.get(forecast_url, timeout=8, headers={"User-Agent": "trails-agent/1.0"})
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"][:6]  # next 3 days

        lines = ["7-day forecast (next 3 days shown):"]
        for p in periods:
            lines.append(
                f"  {p['name']}: {p['shortForecast']}, "
                f"{p['temperature']}°{p['temperatureUnit']} | "
                f"Wind: {p['windSpeed']} {p['windDirection']}"
            )
        return "\n".join(lines)

    except Exception as e:
        return f"Weather unavailable: {e}"


# ─── AQI (AirNow — requires free API key) ─────────────────────────────────────

def get_aqi(lat: float, lng: float) -> str:
    """
    Get current Air Quality Index for a trail location.

    Args:
        lat: Latitude of the trail.
        lng: Longitude of the trail.
    """
    import os
    api_key = os.environ.get("AIRNOW_API_KEY")
    if not api_key:
        return (
            "Air quality data is not currently available for this trail. "
            "For fire season trips, check airnow.gov manually before heading out."
        )

    try:
        url = (
            f"https://www.airnowapi.org/aq/observation/latLong/current/"
            f"?format=application/json&latitude={lat}&longitude={lng}"
            f"&distance=25&API_KEY={api_key}"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        if not data:
            return "No AQI data available for this location."
        lines = []
        for obs in data:
            cat = obs.get("Category", {}).get("Name", "unknown")
            lines.append(f"  {obs['ParameterName']}: AQI {obs['AQI']} ({cat})")
        return "Current AQI:\n" + "\n".join(lines)
    except Exception as e:
        return f"AQI lookup failed: {e}"


# ─── Permits (recreation.gov — requires free API key) ─────────────────────────

def get_permit_info(area_name: str) -> str:
    """
    Check if a wilderness area requires a permit and if availability exists.

    Args:
        area_name: The trail's area or park name.
    """
    # TODO: Set RECREATION_GOV_API_KEY env var after registering at
    # https://ridb.recreation.gov/docs (free)
    import os
    api_key = os.environ.get("RECREATION_GOV_API_KEY")
    if not api_key:
        return "Permit info unavailable — set RECREATION_GOV_API_KEY env var (free at ridb.recreation.gov)."

    try:
        url = "https://ridb.recreation.gov/api/v1/facilities"
        params = {"query": area_name, "limit": 3, "apikey": api_key}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        facilities = r.json().get("RECDATA", [])
        if not facilities:
            return f"No permit facilities found for '{area_name}'."
        lines = [f"Permit facilities near {area_name}:"]
        for f in facilities:
            lines.append(f"  • {f['FacilityName']} — {f.get('FacilityDescription', 'no description')[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"Permit lookup failed: {e}"


# ─── Trail Photos (Wikipedia — no key required) ───────────────────────────────

def get_trail_photo(lat: float, lng: float, trail_name: str, area_name: str = "") -> str:
    """
    Fetch a representative photo URL for a trail using the Wikipedia pageimages API.
    Tries the trail name first; falls back to area_name if no image is found.
    Returns a PHOTO_URL: <url> string so the frontend can render it,
    or an empty string if no photo is available.

    Args:
        lat:        Latitude from trail search results (unused, kept for signature compatibility).
        lng:        Longitude from trail search results (unused, kept for signature compatibility).
        trail_name: Trail name from search results; used as the primary Wikipedia lookup title.
        area_name:  Park or wilderness area name from search results; used as fallback lookup title.
    """
    headers = {"User-Agent": "trails-agent/1.0"}

    def _fetch_thumbnail(title: str) -> str:
        try:
            r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": title,
                    "prop": "pageimages",
                    "pithumbsize": 600,
                    "format": "json",
                },
                headers=headers,
                timeout=8,
            )
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if "missing" in page:
                    return ""
                url = page.get("thumbnail", {}).get("source", "")
                return url
        except Exception:
            return ""
        return ""

    photo_url = _fetch_thumbnail(trail_name)
    if not photo_url and area_name:
        photo_url = _fetch_thumbnail(area_name)

    if photo_url:
        return f"PHOTO_URL: {photo_url}"
    return ""


# ─── Tool schema for OpenAI-compatible tool calling ───────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_preferences",
            "description": "Call this tool EVERY TIME the user reveals a preference — where they want to go, how hard, how long, what features they want, route type, or number of days. Call it immediately when you learn something new. Partial updates are fine. Do NOT wait until all preferences are known.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region":     {"type": "string", "description": "Area, park, or city name"},
                    "difficulty": {"type": "string", "enum": ["easy", "moderate", "hard", "very hard"]},
                    "min_miles":  {"type": "number", "description": "Min trip length in miles"},
                    "max_miles":  {"type": "number", "description": "Max trip length in miles"},
                    "features":   {"type": "array", "items": {"type": "string"}, "description": "Desired features: views, water, forest, camping, lake, river, wildlife, wildflowers, waterfall"},
                    "route_type": {"type": "string", "enum": ["loop", "out-and-back", "point-to-point"]},
                    "trip_days":  {"type": "integer", "description": "Number of days for the trip"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_trails",
            "description": "Call this tool ONLY after the user has confirmed they are ready to find a trail and you have collected their preferences using set_preferences. Do NOT call this tool speculatively or before the user is ready.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region":      {"type": "string", "description": "Area, park, or city name to filter by. Examples: 'Yosemite', 'Sierra Nevada', 'Big Sur', 'Death Valley'"},
                    "difficulty":  {"type": "string", "enum": ["easy", "moderate", "hard", "very hard"], "description": "Trail difficulty level"},
                    "min_miles":   {"type": "number", "description": "Minimum trail length in miles"},
                    "max_miles":   {"type": "number", "description": "Maximum trail length in miles"},
                    "min_elev_ft": {"type": "integer", "description": "Minimum elevation gain in feet"},
                    "max_elev_ft": {"type": "integer", "description": "Maximum elevation gain in feet"},
                    "features":    {"type": "array", "items": {"type": "string"},
                                    "description": "Desired trail features. Valid values: 'views', 'water', 'forest', 'camping', 'lake', 'river', 'wildlife', 'wildflowers', 'waterfall'"},
                    "route_type":  {"type": "string", "enum": ["loop", "out-and-back", "point-to-point"], "description": "Preferred route shape"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trail_details",
            "description": "Call this tool to get full details about a specific trail when you already know the trail name. Returns all fields including coordinates, features, and ratings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Full or partial trail name from search results"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Call this tool AFTER choosing a trail to get its weather forecast. Use the lat/lng coordinates from the search results. Returns the next 3 days of forecast.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude from trail search results"},
                    "lng": {"type": "number", "description": "Longitude from trail search results"},
                },
                "required": ["lat", "lng"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_aqi",
            "description": "Call this tool to check CURRENT air quality at the trail location. Use the lat/lng coordinates from search results. Returns today's AQI only — this is NOT a forecast. Important for fire season safety.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude from trail search results"},
                    "lng": {"type": "number", "description": "Longitude from trail search results"},
                },
                "required": ["lat", "lng"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_permit_info",
            "description": "Call this tool AFTER choosing a trail to check if the area requires a wilderness permit. Use the area name from search results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area_name": {"type": "string", "description": "The trail's area or park name from search results, e.g. 'Yosemite National Park'"},
                },
                "required": ["area_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trail_photo",
            "description": "Call this tool AFTER picking the best trail to fetch a real photo for it. Use the lat/lng and name from search results. Pass area_name so the tool can fall back to the park or wilderness area if no trail-level image exists. Include the result verbatim in your final response — the frontend will render the image automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":        {"type": "number", "description": "Latitude from trail search results"},
                    "lng":        {"type": "number", "description": "Longitude from trail search results"},
                    "trail_name": {"type": "string", "description": "Trail name from search results"},
                    "area_name":  {"type": "string", "description": "Park or wilderness area name from search results, used as fallback if no trail photo is found"},
                },
                "required": ["lat", "lng", "trail_name"],
            },
        },
    },
]

# Map name → callable for the agent loop
TOOL_MAP = {
    "set_preferences":  set_preferences,
    "search_trails":    search_trails,
    "get_trail_details": get_trail_details,
    "get_weather":      get_weather,
    "get_aqi":          get_aqi,
    "get_permit_info":  get_permit_info,
    "get_trail_photo":  get_trail_photo,
}
