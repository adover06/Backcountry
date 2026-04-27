"""AI report builder (bullet format only)."""

from __future__ import annotations

import json


def build_ai_report(route: dict, selected_trail: dict | None, checks: dict, risk: dict) -> dict:
    # Deterministic bullet construction for MVP; replace with LLM if desired.
    bullets = []
    status = risk.get("status", "go")
    bullets.append(f"Status: {status.upper()}")

    if selected_trail:
        bullets.append(
            f"Route: {selected_trail.get('name')} in {selected_trail.get('area')}"
        )
    else:
        bullets.append(f"Route length: {route.get('length_miles')} mi")

    for r in risk.get("reasons", []):
        bullets.append(r)

    return {
        "format": "bullets",
        "bullets": bullets,
    }
