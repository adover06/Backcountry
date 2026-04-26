# AllTrails Probe (Isolated Test)

Small, isolated scraper experiment to test whether we can reliably extract:

- Trail description
- First/cover image URL
- Optional local download of first image

This folder is intentionally standalone and does not integrate with the app.

This probe is designed for compliant, low-volume testing:

- Respects `robots.txt` by default
- Uses retry + backoff and jittered pacing
- Caps processing count with `--max-trails`
- Supports headed Playwright mode for manual, human-in-the-loop browsing

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

Headed browser mode (manual interaction):

```bash
python probe.py --input sample_trails.json --use-playwright --headed
```

Force Playwright only (skip static requests):

```bash
python probe.py --input sample_trails.json --use-playwright --playwright-only --headed
```

Interactive debug run (pause browser until you press Enter in terminal):

```bash
python probe.py --input sample_trails.json --use-playwright --playwright-only --headed --interactive --debug-dir outputs/debug
```

Persistent session profile (reuse cookies/challenge state across runs):

```bash
python probe.py --input sample_trails.json --use-playwright --playwright-only --headed --interactive --profile-dir outputs/pw_profile
```

Recommended manual-verification flow:

1. Start with one trail (`--max-trails 1`).
2. When browser opens, manually solve any verification.
3. Confirm the actual trail page content is visible.
4. Return to terminal and press Enter.
5. Re-run with the same `--profile-dir` for next trails.

Conservative run settings:

```bash
python probe.py --input sample_trails.json --use-playwright --max-trails 10 --delay 4
```

Also download first image locally:

```bash
python probe.py --input sample_trails.json --discover-url --use-playwright --download-images
```

## Output

- `outputs/results.json`
- `outputs/results.csv`
- Optional image files in `outputs/images/`

Each result row includes:

- `status`, `method`
- `http_status`
- `blocked_by_robots`
- `page_title`
- `error` (if any)

## Notes

- No account credentials are required for this basic probe.
- Subscriber-only galleries are likely not fully accessible without auth.
- This is an experiment: reliability can vary due to anti-bot controls and page changes.
- `--ignore-robots` exists only for debugging and is not recommended.
