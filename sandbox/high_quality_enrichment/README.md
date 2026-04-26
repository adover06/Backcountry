# High Quality Enrichment Demo

This is an isolated example pipeline that shows how trail enrichment can work
without integrating into the main app yet.

It demonstrates:

- multi-source candidates per field
- source ranking + confidence scoring
- provenance tracking (source URL, license, fetched time)
- fallback generated description
- Chroma-ready document output

## Files

- `enrich_demo.py` - demo enrichment pipeline
- `sample_trails.json` - tiny canonical input set
- `mock_sources/official_descriptions.json` - mock official text candidates
- `mock_sources/image_catalog.json` - mock image candidates + licenses
- `outputs/` - generated results

## Run

```bash
python3 enrich_demo.py
```

## Output

- `outputs/trails_enriched.json`
- `outputs/chroma_docs.jsonl`
- `outputs/report.json`

## Notes

- This is intentionally small and deterministic.
- It is a template for a real pipeline, not production data collection.
