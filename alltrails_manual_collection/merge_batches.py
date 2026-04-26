#!/usr/bin/env python3
"""
Merge + validate manual AllTrails batches.

Reads JSON files from ./batches, deduplicates by id and alltrails_url,
writes master JSON and a QA report in ./outputs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
BATCH_DIR = BASE / "batches"
OUT_DIR = BASE / "outputs"


REQUIRED_TOP = {
    "batch_id",
    "generated_at",
    "source",
    "focus_area",
    "focus_query",
    "target_count",
    "returned_count",
    "batch_notes",
    "trails",
}

REQUIRED_TRAIL = {
    "id",
    "name",
    "area",
    "region",
    "city",
    "state",
    "lat",
    "lng",
    "length_miles",
    "elev_gain_ft",
    "difficulty",
    "route_type",
    "avg_rating",
    "num_reviews",
    "features",
    "description",
    "weather_hint",
    "permit_hint",
    "alltrails_url",
    "photo_url",
    "data_confidence",
    "missing_fields",
}


def load_batches() -> list[dict]:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    batches = []
    for p in sorted(BATCH_DIR.glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            batches.append({"_file": str(p.name), "_error": f"json parse error: {e}"})
            continue
        obj["_file"] = p.name
        batches.append(obj)
    return batches


def validate_batch(batch: dict) -> list[str]:
    errs: list[str] = []
    if "_error" in batch:
        errs.append(batch["_error"])
        return errs

    missing_top = sorted(REQUIRED_TOP - set(batch.keys()))
    if missing_top:
        errs.append(f"missing top-level keys: {missing_top}")

    trails = batch.get("trails")
    if not isinstance(trails, list):
        errs.append("trails must be a list")
        return errs

    for i, t in enumerate(trails):
        if not isinstance(t, dict):
            errs.append(f"trail index {i} is not an object")
            continue
        missing = sorted(REQUIRED_TRAIL - set(t.keys()))
        if missing:
            errs.append(f"trail index {i} missing keys: {missing}")

    return errs


def merge() -> tuple[dict, dict]:
    batches = load_batches()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch_files_seen": len(batches),
        "batch_errors": [],
        "rows_in": 0,
        "rows_out_unique": 0,
        "dupe_by_id": 0,
        "dupe_by_url": 0,
        "null_url_rows": 0,
        "by_focus_area": {},
    }

    all_rows: list[dict] = []

    for b in batches:
        errs = validate_batch(b)
        if errs:
            report["batch_errors"].append({"file": b.get("_file", "unknown"), "errors": errs})
            continue

        focus = b.get("focus_area", "unknown")
        report["by_focus_area"][focus] = report["by_focus_area"].get(focus, 0) + len(b.get("trails", []))
        for t in b.get("trails", []):
            t2 = dict(t)
            t2["_batch_id"] = b.get("batch_id")
            t2["_batch_file"] = b.get("_file")
            all_rows.append(t2)

    report["rows_in"] = len(all_rows)

    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    unique: list[dict] = []

    for r in all_rows:
        rid = (r.get("id") or "").strip().lower()
        rurl = (r.get("alltrails_url") or "").strip().lower()

        if not rurl:
            report["null_url_rows"] += 1

        if rid and rid in seen_ids:
            report["dupe_by_id"] += 1
            continue

        if rurl and rurl in seen_urls:
            report["dupe_by_url"] += 1
            continue

        if rid:
            seen_ids.add(rid)
        if rurl:
            seen_urls.add(rurl)

        unique.append(r)

    report["rows_out_unique"] = len(unique)

    master = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "alltrails_chatgpt_manual_batches",
        "total_unique": len(unique),
        "trails": unique,
    }

    return master, report


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master, report = merge()

    (OUT_DIR / "norcal_master_merged.json").write_text(
        json.dumps(master, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (OUT_DIR / "merge_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    print(f"Merged unique trails: {master['total_unique']}")
    print(f"Batch files seen: {report['batch_files_seen']}")
    print(f"Batch errors: {len(report['batch_errors'])}")
    print(f"Output: {OUT_DIR / 'norcal_master_merged.json'}")
    print(f"Report: {OUT_DIR / 'merge_report.json'}")


if __name__ == "__main__":
    main()
