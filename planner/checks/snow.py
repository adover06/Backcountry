"""Snow summary using NOHRSC (placeholder)."""

from __future__ import annotations

import requests


def get_snow_summary(lat: float, lng: float, start_date: str, end_date: str) -> dict:
    # TODO: Implement NOHRSC snow depth lookup for point queries.
    # Placeholder for now so pipeline is functional.
    return {
        "status": "unavailable",
        "message": "NOHRSC integration not yet wired; add snow depth feed.",
        "depth_in": None,
    }
