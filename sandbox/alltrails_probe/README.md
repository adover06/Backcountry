# AllTrails Probe (Isolated Test)

Small, isolated scraper experiment to test whether we can reliably extract:

- Trail description
- First/cover image URL
- Optional local download of first image

This folder is intentionally standalone and does not integrate with the app.

## What this test does

1. Takes a small trail list JSON (`sample_trails.json` format).
2. Uses either:
   - Direct `alltrails_url` from your input, or
   - Optional URL discovery via DuckDuckGo (`--discover-url`), then filters to AllTrails trail pages.
3. Fetches page data with `requests + BeautifulSoup`.
4. Optionally uses Playwright as a fallback (`--use-playwright`) when static HTML is incomplete.
5. Writes results to JSON and CSV in `outputs/`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Input format

`sample_trails.json` is an array:

```json
[
  {
    "name": "Mist Trail",
    "region": "Yosemite",
    "alltrails_url": "https://www.alltrails.com/trail/us/california/mist-trail"
  },
  {
    "name": "Alamere Falls Trail",
    "region": "Point Reyes"
  }
]
```

## Run examples

Basic (static fetch only):

```bash
python probe.py --input sample_trails.json
```

Enable URL discovery + Playwright fallback:

```bash
python probe.py --input sample_trails.json --discover-url --use-playwright
```

Also download first image locally:

```bash
python probe.py --input sample_trails.json --discover-url --use-playwright --download-images
```

## Output

- `outputs/results.json`
- `outputs/results.csv`
- Optional image files in `outputs/images/`

## Notes

- No account credentials are required for this basic probe.
- Subscriber-only galleries are likely not fully accessible without auth.
- This is an experiment: reliability can vary due to anti-bot controls and page changes.
