#!/usr/bin/env python3
"""
Finalize a completed batch:
- appends a row to batch_tracker.csv
- marks matching in_progress queue row as done
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


BASE = Path(__file__).resolve().parent


def read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        headers = list(reader.fieldnames or [])
    return rows, headers


def write_csv(path: Path, rows: list[dict], headers: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize a completed batch")
    parser.add_argument("--batch-file", required=True, help="Path to batch JSON file")
    parser.add_argument("--queue", default="focus_queue_norcal_sacramento_first.csv")
    parser.add_argument("--tracker", default="batch_tracker.csv")
    parser.add_argument("--status", default="done", help="Queue status to set (default: done)")
    parser.add_argument(
        "--also-complete-stale",
        action="store_true",
        help="Also mark all remaining in_progress rows as done to clean stuck queue state",
    )
    args = parser.parse_args()

    batch_path = (BASE / args.batch_file).resolve() if not Path(args.batch_file).is_absolute() else Path(args.batch_file)
    queue_path = (BASE / args.queue).resolve() if not Path(args.queue).is_absolute() else Path(args.queue)
    tracker_path = (BASE / args.tracker).resolve() if not Path(args.tracker).is_absolute() else Path(args.tracker)

    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch_id = str(batch.get("batch_id", "")).strip()
    focus_area = str(batch.get("focus_area", "")).strip()
    focus_query = str(batch.get("focus_query", "")).strip()
    returned_count = len(batch.get("trails", [])) if isinstance(batch.get("trails"), list) else 0

    # Update queue
    q_rows, q_headers = read_csv(queue_path)
    updated = False
    # 1) Best match: same focus area, regardless of current status.
    for row in q_rows:
        area = (row.get("focus_area") or "").strip()
        if area == focus_area:
            row["status"] = args.status
            note = (row.get("notes") or "").strip()
            row["notes"] = f"{note}; finalized {batch_id}".strip("; ")
            updated = True
            break

    # 2) Fallback: first in_progress row.
    if not updated:
        for row in q_rows:
            status = (row.get("status") or "").strip().lower()
            if status == "in_progress":
                row["status"] = args.status
                note = (row.get("notes") or "").strip()
                row["notes"] = f"{note}; finalized {batch_id}".strip("; ")
                updated = True
                break

    write_csv(queue_path, q_rows, q_headers)

    if args.also_complete_stale:
        changed = 0
        for row in q_rows:
            if (row.get("status") or "").strip().lower() == "in_progress":
                row["status"] = args.status
                note = (row.get("notes") or "").strip()
                row["notes"] = f"{note}; stale-complete {batch_id}".strip("; ")
                changed += 1
        if changed:
            write_csv(queue_path, q_rows, q_headers)

    # Append tracker
    t_rows, t_headers = read_csv(tracker_path)
    row = {
        "batch_id": batch_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "focus_area": focus_area,
        "focus_query": focus_query,
        "file_name": batch_path.name,
        "returned_count": str(returned_count),
        "new_unique_after_merge": "",
        "status": "saved",
        "notes": "",
    }
    t_rows.append(row)
    write_csv(tracker_path, t_rows, t_headers)

    print(f"Finalized {batch_id} from {batch_path.name}")
    print(f"Focus area: {focus_area}")
    print(f"Returned count: {returned_count}")
    print(f"Queue updated: {updated}")
    if args.also_complete_stale:
        print("Stale in_progress rows were also completed")
    print(f"Tracker updated: {tracker_path}")


if __name__ == "__main__":
    main()
