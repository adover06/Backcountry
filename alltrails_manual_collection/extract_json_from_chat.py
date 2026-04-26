#!/usr/bin/env python3
"""
Extract first valid JSON object from noisy chat transcript text.

Usage:
  python3 extract_json_from_chat.py --input raw.txt --output batches/batch_001.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def extract_first_json_blob(text: str) -> dict:
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for s in starts:
        depth = 0
        in_str = False
        esc = False
        for i in range(s, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = text[s : i + 1]
                    try:
                        obj = json.loads(blob)
                    except Exception:
                        break
                    if isinstance(obj, dict) and ("trails" in obj or "generated_at" in obj):
                        return obj
                    break
    raise ValueError("No valid JSON object found")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract JSON object from chat text")
    parser.add_argument("--input", required=True, help="Path to raw transcript text")
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    text = in_path.read_text(encoding="utf-8")
    obj = extract_first_json_blob(text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(obj, indent=2, ensure_ascii=True), encoding="utf-8")
    count = len(obj.get("trails", [])) if isinstance(obj.get("trails"), list) else 0
    print(f"Extracted JSON with {count} trails -> {out_path}")


if __name__ == "__main__":
    main()
