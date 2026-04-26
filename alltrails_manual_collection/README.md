# AllTrails Manual Batch Collection (20 at a time)

This folder gives you a reliable, low-friction workflow for collecting AllTrails data in ChatGPT app integration mode.

## What you get

- `master_prompt_batch20.txt`: prompt for consistent 20-at-a-time batches.
- `resume_prompt_template.txt`: prompt for resuming in a new chat/day.
- `focus_queue_norcal_sacramento_first.csv`: Sacramento-first region order.
- `batch_tracker.csv`: manual progress tracker.
- `merge_batches.py`: merges, dedupes, validates, and reports progress.
- `extract_json_from_chat.py`: pulls the first valid JSON object from noisy chat text.

## Recommended workflow

1. Pick next row from `focus_queue_norcal_sacramento_first.csv`.
2. Paste `master_prompt_batch20.txt` into ChatGPT (AllTrails integration enabled).
3. Replace placeholders:
   - `BATCH_ID`
   - `FOCUS_AREA`
   - `FOCUS_QUERY`
4. Save output JSON into `batches/batch_XXX.json`.
5. Log row in `batch_tracker.csv`.
6. Run merge script:

```bash
python3 alltrails_manual_collection/merge_batches.py
```

If ChatGPT includes extra narration/tool text, save the whole reply to a raw text file and extract JSON:

```bash
python3 alltrails_manual_collection/extract_json_from_chat.py --input raw_reply.txt --output alltrails_manual_collection/batches/batch_001.json
```

7. Check outputs in `alltrails_manual_collection/outputs/`.

## Output reliability recommendation

Most reliable in practice is a single fenced JSON block from ChatGPT. Downloadable files are nice when available, but copy-paste from a JSON code block is usually consistent.

## Resume after context reset/rate limit

- Open a new chat.
- Paste `resume_prompt_template.txt`.
- Insert your last completed batch id and next focus area.
- Continue writing files into `batches/` and rerun merge.
