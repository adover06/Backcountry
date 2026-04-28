"""Snow summary using NOHRSC National Snow Analyses."""

from __future__ import annotations

import re
import requests


NSA_URL = "https://www.nohrsc.noaa.gov/nsa/"


def _latest_snow_depth_image(html: str) -> str | None:
    # Look for the latest National snow depth image in the NSA page.
    matches = re.findall(r"/snow_model/images/full/National/nsm_depth/\d{6}/nsm_depth_\d{10}_National\.jpg", html)
    if not matches:
        return None
    # Use the last occurrence as the most recent (page typically lists current first)
    return "https://www.nohrsc.noaa.gov" + matches[0]


def get_snow_summary(lat: float, lng: float, start_date: str, end_date: str) -> dict:
    # NOTE: NOHRSC does not provide a simple point query for depth.
    # This returns the latest national snow depth image URL as a proxy signal.
    try:
        r = requests.get(NSA_URL, timeout=8)
        r.raise_for_status()
        img_url = _latest_snow_depth_image(r.text)
        if not img_url:
            return {
                "status": "unavailable",
                "message": "Snow depth image not found.",
                "depth_in": None,
                "image_url": None,
            }
        return {
            "status": "ok",
            "message": "Latest NOHRSC national snow depth image available.",
            "depth_in": None,
            "image_url": img_url,
        }
    except Exception as e:
        return {"status": "unavailable", "message": str(e), "depth_in": None, "image_url": None}
