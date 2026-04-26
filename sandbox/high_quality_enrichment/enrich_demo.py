#!/usr/bin/env python3
"""
Isolated enrichment demo.

Builds enriched trail records from canonical trails + mock source candidates,
then emits Chroma-ready JSONL docs and a quality report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent


SOURCE_RANK = {
    "official": 1.0,
    "wikimedia": 0.85,
    "wikipedia": 0.8,
    "community": 0.6,
    "generated": 0.35,
}


@dataclass
class Candidate:
    field: str
    value: Any
    source_type: str
    source_url: str
    license: str
    fetched_at: str
    notes: str = ""


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _score_candidate(c: Candidate, trail: dict) -> float:
    score = SOURCE_RANK.get(c.source_type, 0.3)
    if c.field == "description":
        text = str(c.value or "").strip()
        length_bonus = min(len(text) / 220.0, 0.25)
        score += length_bonus
    if c.field == "image":
        if isinstance(c.value, str) and c.value.startswith("https://"):
            score += 0.1
    if trail.get("name", "").lower() in str(c.notes).lower():
        score += 0.03
    return round(min(score, 1.0), 3)


def _generate_description(trail: dict) -> str:
    feats = ", ".join(trail.get("features") or []) or "mixed scenery"
    return (
        f"{trail['name']} in {trail['area']} is a {trail['difficulty']} "
        f"{trail['route_type']} route of about {trail['length_miles']} miles with "
        f"{trail['elev_gain_ft']} ft of climbing, featuring {feats}."
    )


def _best(candidates: list[Candidate], trail: dict) -> tuple[Candidate, float]:
    scored = [(_score_candidate(c, trail), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_cand = scored[0]
    return top_cand, top_score


def build_enriched() -> tuple[list[dict], dict]:
    trails = _load_json(BASE / "sample_trails.json")
    official_desc = _load_json(BASE / "mock_sources" / "official_descriptions.json")
    image_catalog = _load_json(BASE / "mock_sources" / "image_catalog.json")

    desc_by_id: dict[str, list[Candidate]] = {}
    for row in official_desc:
        desc_by_id.setdefault(row["trail_id"], []).append(
            Candidate(
                field="description",
                value=row.get("description", ""),
                source_type=row.get("source_type", "official"),
                source_url=row.get("source_url", ""),
                license=row.get("license", "unknown"),
                fetched_at=row.get("fetched_at", _now_iso()),
                notes="",
            )
        )

    image_by_id: dict[str, list[Candidate]] = {}
    for row in image_catalog:
        image_by_id.setdefault(row["trail_id"], []).append(
            Candidate(
                field="image",
                value=row.get("image_url", ""),
                source_type=row.get("source_type", "wikimedia"),
                source_url=row.get("source_url", ""),
                license=row.get("license", "unknown"),
                fetched_at=row.get("fetched_at", _now_iso()),
                notes=row.get("attribution", ""),
            )
        )

    enriched: list[dict] = []
    report = {
        "generated_at": _now_iso(),
        "total_trails": len(trails),
        "description_sources": {},
        "image_sources": {},
        "missing_description": 0,
        "missing_image": 0,
    }

    for t in trails:
        tid = t["trail_id"]
        desc_candidates = list(desc_by_id.get(tid, []))
        image_candidates = list(image_by_id.get(tid, []))

        if not desc_candidates:
            desc_candidates.append(
                Candidate(
                    field="description",
                    value=_generate_description(t),
                    source_type="generated",
                    source_url="",
                    license="internal",
                    fetched_at=_now_iso(),
                    notes="generated fallback",
                )
            )

        desc_best, desc_conf = _best(desc_candidates, t)

        image_best = None
        image_conf = 0.0
        if image_candidates:
            image_best, image_conf = _best(image_candidates, t)

        desc_source = desc_best.source_type
        report["description_sources"][desc_source] = report["description_sources"].get(desc_source, 0) + 1

        if image_best is not None:
            img_source = image_best.source_type
            report["image_sources"][img_source] = report["image_sources"].get(img_source, 0) + 1
        else:
            report["missing_image"] += 1

        if desc_source == "generated":
            report["missing_description"] += 1

        enriched.append(
            {
                **t,
                "description": desc_best.value,
                "description_source": {
                    "type": desc_best.source_type,
                    "url": desc_best.source_url,
                    "license": desc_best.license,
                    "fetched_at": desc_best.fetched_at,
                    "confidence": desc_conf,
                },
                "image": {
                    "url": image_best.value if image_best else "",
                    "source": image_best.source_type if image_best else "",
                    "source_url": image_best.source_url if image_best else "",
                    "license": image_best.license if image_best else "",
                    "fetched_at": image_best.fetched_at if image_best else "",
                    "confidence": image_conf if image_best else 0.0,
                },
                "enriched_at": _now_iso(),
            }
        )

    return enriched, report


def write_outputs(enriched: list[dict], report: dict) -> None:
    out = BASE / "outputs"
    out.mkdir(parents=True, exist_ok=True)

    (out / "trails_enriched.json").write_text(
        json.dumps(enriched, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    with (out / "chroma_docs.jsonl").open("w", encoding="utf-8") as f:
        for t in enriched:
            doc = {
                "id": t["trail_id"],
                "document": (
                    f"{t['name']} in {t['area']}. "
                    f"{t['description']} "
                    f"Difficulty: {t['difficulty']}. "
                    f"Length: {t['length_miles']} miles. "
                    f"Elevation gain: {t['elev_gain_ft']} ft. "
                    f"Route: {t['route_type']}. "
                    f"Features: {', '.join(t.get('features') or [])}."
                ),
                "metadata": {
                    "trail_id": t["trail_id"],
                    "name": t["name"],
                    "area": t["area"],
                    "lat": t["lat"],
                    "lng": t["lng"],
                    "difficulty": t["difficulty"],
                    "route_type": t["route_type"],
                    "description_source": t["description_source"]["type"],
                    "description_confidence": t["description_source"]["confidence"],
                    "image_url": t["image"]["url"],
                },
            }
            f.write(json.dumps(doc, ensure_ascii=True) + "\n")

    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )


def main() -> None:
    enriched, report = build_enriched()
    write_outputs(enriched, report)
    print(f"Enriched trails: {len(enriched)}")
    print(f"Description sources: {report['description_sources']}")
    print(f"Image sources: {report['image_sources']}")
    print(f"Missing image: {report['missing_image']}")
    print(f"Outputs: {BASE / 'outputs'}")


if __name__ == "__main__":
    main()
