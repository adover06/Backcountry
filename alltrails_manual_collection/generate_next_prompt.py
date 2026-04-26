#!/usr/bin/env python3
"""
Generate the next ChatGPT collection prompt from queue + template.

Defaults:
- picks first pending row in focus queue
- auto-increments batch id from existing batch files
- writes rendered prompt to generated_prompt.txt
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


BASE = Path(__file__).resolve().parent


def next_batch_id(batches_dir: Path) -> str:
    max_n = 0
    pattern = re.compile(r"batch_(\d+)\.json$")
    for p in batches_dir.glob("batch_*.json"):
        m = pattern.search(p.name)
        if m:
            max_n = max(max_n, int(m.group(1)))

    tracker_path = BASE / "batch_tracker.csv"
    if tracker_path.exists():
        try:
            with tracker_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                id_pattern = re.compile(r"batch_(\d+)$")
                for row in reader:
                    bid = (row.get("batch_id") or "").strip()
                    m = id_pattern.search(bid)
                    if m:
                        max_n = max(max_n, int(m.group(1)))
        except Exception:
            pass

    return f"batch_{max_n + 1:03d}"


def load_queue(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        headers = list(reader.fieldnames or [])
    return rows, headers


def save_queue(path: Path, rows: list[dict], headers: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def pick_pending_row(rows: list[dict]) -> tuple[int, dict]:
    # If multiple rows are in_progress, keep working on the earliest one.
    for i, row in enumerate(rows):
        status = (row.get("status") or "").strip().lower()
        if status == "in_progress":
            return i, row

    for i, row in enumerate(rows):
        status = (row.get("status") or "").strip().lower()
        if status in ("", "pending"):
            return i, row
    raise RuntimeError("No pending rows in focus queue")


def render_prompt(template: str, batch_id: str, area: str, query: str) -> str:
    return (
        template.replace("BATCH_ID", batch_id)
        .replace("FOCUS_AREA", area)
        .replace("FOCUS_QUERY", query)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate next prompt from queue")
    parser.add_argument("--queue", default="focus_queue_norcal_sacramento_first.csv")
    parser.add_argument("--template", default="master_prompt_batch20.txt")
    parser.add_argument("--batches-dir", default="batches")
    parser.add_argument("--output", default="generated_prompt.txt")
    parser.add_argument("--batch-id", default="", help="Override auto batch id")
    parser.add_argument(
        "--mark-in-progress",
        action="store_true",
        help="Mark selected queue row as in_progress",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print rendered prompt to stdout only",
    )
    args = parser.parse_args()

    queue_path = (BASE / args.queue).resolve() if not Path(args.queue).is_absolute() else Path(args.queue)
    template_path = (BASE / args.template).resolve() if not Path(args.template).is_absolute() else Path(args.template)
    batches_dir = (BASE / args.batches_dir).resolve() if not Path(args.batches_dir).is_absolute() else Path(args.batches_dir)
    output_path = (BASE / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)

    rows, headers = load_queue(queue_path)
    idx, row = pick_pending_row(rows)

    batch_id = args.batch_id.strip() or next_batch_id(batches_dir)
    area = (row.get("focus_area") or "").strip()
    query = (row.get("focus_query") or "").strip()

    if not area or not query:
        raise RuntimeError("Selected queue row is missing focus_area or focus_query")

    template = template_path.read_text(encoding="utf-8")
    rendered = render_prompt(template, batch_id, area, query)

    if args.print_only:
        print(rendered)
    else:
        output_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote prompt: {output_path}")

    print(f"Batch: {batch_id}")
    print(f"Focus area: {area}")
    print(f"Focus query: {query}")

    if args.mark_in_progress:
        rows[idx]["status"] = "in_progress"
        save_queue(queue_path, rows, headers)
        print(f"Updated queue row {idx + 1} to in_progress")


if __name__ == "__main__":
    main()
