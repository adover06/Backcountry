"""Trail photos from Wikimedia Commons.

Commons is the only free, no-key, openly-licensed source of geotagged outdoor
photography at useful density. Measured against this index: roughly two thirds of
trails have at least one CC-licensed image near them.

Two things this module is careful about:

* **Sampling follows the trail, not its midpoint.** A midpoint-only lookup misses
  everything along a long route and, on a horseshoe, samples ground the trail never
  touches.
* **Proximity is not depiction.** An image 1.5 mi from the line is evidence of
  *somewhere near here*, not a photo of this trail. Every record keeps its distance
  so the UI can label rather than imply, and photos are ranked by closeness.

License metadata (`license`, `artist`, `attribution_required`) is captured per image
because CC-BY requires credit at display time.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .normalize import haversine_miles

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

_CACHE_DIR = Path(os.environ.get("PHOTO_CACHE_DIR", Path(__file__).resolve().parent.parent / ".photo_cache"))

# Commons caps geosearch radius at 10 km; 2 km keeps results plausibly relevant.
SEARCH_RADIUS_M = 2000
RESULTS_PER_POINT = 20

# How many points along the trail to sample. More points cost more requests.
MAX_SAMPLE_POINTS = 5

# Keep the best few per trail.
MAX_PHOTOS_PER_TRAIL = 8

REQUEST_PAUSE_SECONDS = float(os.environ.get("PHOTO_PAUSE", "0.5"))

# Commons returns 429 under sustained querying; back off rather than losing the trail.
MAX_RETRIES = 4

_HEADERS = {"User-Agent": "OpenTrails/1.0 (personal trail discovery)"}

# Licenses acceptable for display. Anything else is dropped rather than shown with
# terms we have not checked.
_ALLOWED_LICENSE_PREFIXES = ("cc0", "cc by", "cc-by", "public domain", "pd")


def _sample_points(geometry: dict, limit: int = MAX_SAMPLE_POINTS) -> list[tuple[float, float]]:
    """Evenly spaced (lat, lng) samples along the trail."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "MultiLineString":
        flat = [c for line in coords for c in line]
    elif gtype == "LineString":
        flat = list(coords)
    else:
        return []

    flat = [c for c in flat if isinstance(c, (list, tuple)) and len(c) >= 2]
    if not flat:
        return []
    if len(flat) <= limit:
        return [(c[1], c[0]) for c in flat]

    step = (len(flat) - 1) / (limit - 1)
    return [(flat[int(round(i * step))][1], flat[int(round(i * step))][0]) for i in range(limit)]


def _license_ok(license_name: str | None) -> bool:
    if not license_name:
        return False
    lowered = license_name.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _ALLOWED_LICENSE_PREFIXES)


def _extmeta(imageinfo: dict, key: str) -> str | None:
    value = ((imageinfo.get("extmetadata") or {}).get(key) or {}).get("value")
    if not value:
        return None
    # extmetadata values arrive as HTML fragments.
    import re

    text = re.sub(r"<[^>]+>", "", str(value)).strip()
    return text or None


def _geosearch(lat: float, lng: float) -> list[dict]:
    """Commons images near a point, with license metadata.

    Retries with exponential backoff on 429. Without this a rate-limited burst
    reads as "this trail has no photos", which is the same missing-vs-absent
    confusion the rest of this codebase exists to avoid.
    """
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "geosearch",
        "ggscoord": f"{lat}|{lng}",
        "ggsradius": SEARCH_RADIUS_M,
        "ggslimit": RESULTS_PER_POINT,
        "ggsnamespace": 6,  # File:
        "prop": "imageinfo|coordinates",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 640,
    }
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(COMMONS_API, params=params, timeout=45, headers=_HEADERS)
            if response.status_code == 429:
                time.sleep(2.0 * (2**attempt))
                last_error = RuntimeError("429 Too Many Requests")
                continue
            response.raise_for_status()
            return (response.json().get("query") or {}).get("pages") or []
        except Exception as exc:
            last_error = exc
            time.sleep(1.0 * (2**attempt))
    raise RuntimeError(f"Commons geosearch failed: {last_error}")


# ── Relevance ─────────────────────────────────────────────────────────────────
#
# Geographic proximity is a weak signal for "is this a picture of the hike". A
# macro shot of a pine cone taken beside the trail scores the same distance as the
# view from the pass. Rank on the filename, which on Commons is usually descriptive.

_SCENERY_WORDS = (
    "trail", "falls", "waterfall", "lake", "peak", "summit", "view", "vista",
    "valley", "canyon", "dome", "creek", "river", "meadow", "ridge", "pass",
    "panorama", "landscape", "overlook", "sunset", "sunrise", "mountain",
)

# Species pages and camera-default filenames dominate Commons near popular trails.
_JUNK_PATTERNS = (
    "dsc_", "dscn", "img_", "p10", "cimg", "_mg_", "dscf",
)

_SPECIES_HINT = (
    "aceraceae", "pinus", "quercus", "abies", "juniperus", "arctostaphylos",
    "marmota", "sciurus", "spermophilus", "corvus", "insect", "fungi", "lichen",
    "flower", "botany", "herbarium", "specimen",
)

# Orbital and aerial imagery is geotagged to a point on the ground, so it lands
# inside the search radius for almost any trail. "ISS041-E-34506 - View of Earth"
# even matched the "view" scenery keyword and ranked first for the Tahoe Rim Trail.
_NOT_OF_THE_PLACE = (
    "iss0", "iss ", "view of earth", "from space", "satellite", "landsat",
    "sentinel-", "aerial view", "from orbit", "space shuttle", "astronaut",
    "topographic map", "usgs map", "diagram", "logo", "coat of arms", "poster",
)

# Commons near any popular trailhead is full of lodging and vacation snapshots.
# These share the place name, so the place-token boost alone promotes them over
# actual landscape — "Dinner at our condo - Tahoe Summit Village" outranked the
# trail itself.
_NOT_OUTDOORS = (
    "dinner", "cooking", "breakfast", "lunch", "condo", "resort", "hotel", "motel",
    "wedding", "bedroom", "kitchen", "bathroom", "restaurant", "casino", "lobby",
    "interior", "room ", "buffet", "cocktail", "spa", "gondola ride", "ski lift",
    "parking garage", "storefront", "shopping", "airport", "conference",
)

# Words too generic to be evidence on their own.
_STOPWORDS = {
    "trail", "trails", "creek", "lake", "river", "fork", "canyon", "national",
    "forest", "park", "the", "of", "and", "north", "south", "east", "west",
    "upper", "lower", "big", "little", "mount", "mt",
}


def _tokens(text: str | None) -> set[str]:
    import re as _re

    if not text:
        return set()
    return {t for t in _re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def _relevance(title: str | None, place_tokens: set[str] | None = None) -> float:
    """Higher is more likely to be a usable picture of *this* place.

    The strongest available signal is whether the filename shares a distinctive
    word with the trail's own name or its park — Commons filenames are usually
    descriptive, so "…Eagle Nest, Lake Tahoe" is real evidence for the Tahoe Rim
    Trail while "View of Earth" is not.
    """
    if not title:
        return 0.0
    lowered = title.lower()

    score = 1.0

    # Named for the same place: the highest-confidence signal we have.
    if place_tokens:
        shared = _tokens(lowered) & place_tokens
        if shared:
            score += 4.0 + min(2.0, len(shared) - 1)

    if any(word in lowered for word in _SCENERY_WORDS):
        score += 2.0
    if any(hint in lowered for hint in _NOT_OF_THE_PLACE):
        score -= 6.0  # decisive: never surface these above a real photo
    if any(hint in lowered for hint in _NOT_OUTDOORS):
        score -= 4.5  # enough to sink below any genuine outdoor photo
    if any(hint in lowered for hint in _SPECIES_HINT):
        score -= 2.5
    if any(pattern in lowered for pattern in _JUNK_PATTERNS):
        score -= 1.5
    # "File:12345678.jpg" carries no information.
    stem = lowered.replace("file:", "").rsplit(".", 1)[0]
    if stem.replace(" ", "").isdigit():
        score -= 2.0
    return score


def place_tokens_for(trail: dict) -> set[str]:
    """Distinctive words from a trail's name and area, minus generic terrain words."""
    combined = _tokens(trail.get("name")) | _tokens(trail.get("mgmt_area"))
    return combined - _STOPWORDS


def photos_for_trail(trail: dict, geometry: dict) -> list[dict] | None:
    """Photos near a trail, nearest first. None means the lookup failed."""
    points = _sample_points(geometry)
    if not points:
        return []

    found: dict[str, dict] = {}
    errors = 0

    for lat, lng in points:
        try:
            pages = _geosearch(lat, lng)
        except Exception:
            errors += 1
            continue

        for page in pages:
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]

            license_name = _extmeta(info, "LicenseShortName")
            if not _license_ok(license_name):
                continue

            coordinates = (page.get("coordinates") or [{}])[0]
            plat, plng = coordinates.get("lat"), coordinates.get("lon")
            distance = (
                round(haversine_miles(lat, lng, plat, plng), 2)
                if plat is not None and plng is not None
                else None
            )

            title = page.get("title")
            existing = found.get(title)
            if existing and (existing.get("distance_mi") or 9e9) <= (distance or 9e9):
                continue

            found[title] = {
                "title": title,
                "thumb": info.get("thumburl"),
                "url": info.get("url"),
                "descriptionurl": info.get("descriptionurl"),
                "license": license_name,
                "artist": _extmeta(info, "Artist"),
                # CC-BY and CC-BY-SA require credit wherever the image is shown.
                "attribution_required": not (license_name or "").lower().startswith(
                    ("cc0", "public domain", "pd")
                ),
                "distance_mi": distance,
                "source": "Wikimedia Commons",
            }

        time.sleep(REQUEST_PAUSE_SECONDS)

    # Every sample point failed: report unknown rather than "no photos".
    if errors == len(points):
        return None

    # Rank by relevance first, then proximity. Proximity alone surfaces macro shots
    # of plants and wildlife taken beside the trail over the view from the pass.
    place = place_tokens_for(trail)
    for photo in found.values():
        photo["relevance"] = _relevance(photo.get("title"), place)

    photos = sorted(
        found.values(),
        key=lambda p: (-p["relevance"], p.get("distance_mi") if p.get("distance_mi") is not None else 9e9),
    )
    return photos[:MAX_PHOTOS_PER_TRAIL]


def enrich_photos(
    trails: list[dict],
    geometries: dict,
    limit: int | None = None,
    verbose: bool = True,
) -> dict[str, list[dict]]:
    """Fetch photos for trails that do not have them yet. Returns {trail_id: photos}."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / "commons_photos.json"

    cache: dict[str, list[dict]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            if verbose:
                print(f"  loaded photos for {len(cache)} trails from cache")
        except Exception:
            cache = {}

    pending = [t for t in trails if t["id"] not in cache]
    if limit:
        pending = pending[:limit]

    if verbose:
        print(f"  fetching photos for {len(pending)} trails")

    for index, trail in enumerate(pending, start=1):
        entry = geometries.get(trail["id"]) or {}
        geometry = entry.get("geometry") or trail.get("geometry") or {}
        result = photos_for_trail(trail, geometry)
        if result is not None:
            cache[trail["id"]] = result

        if verbose and index % 100 == 0:
            with_photos = sum(1 for v in cache.values() if v)
            print(f"    {index}/{len(pending)} · {with_photos} trails have photos")
        if index % 250 == 0:
            cache_path.write_text(json.dumps(cache))

    cache_path.write_text(json.dumps(cache))
    if verbose:
        with_photos = sum(1 for v in cache.values() if v)
        print(f"  {with_photos}/{len(cache)} trails have at least one CC photo")
    return cache


# ── Request-time lookup ───────────────────────────────────────────────────────
#
# Bulk-fetching every trail would take hours and mostly fetch photos nobody looks
# at. Photos are resolved on demand for the trail being viewed, then cached to disk
# so each trail costs one lookup ever.

import threading

_cache_lock = threading.Lock()
_memory_cache: dict[str, list[dict]] | None = None
_CACHE_FILE = _CACHE_DIR / "commons_photos.json"


def _load_cache() -> dict[str, list[dict]]:
    global _memory_cache
    if _memory_cache is None:
        try:
            _memory_cache = json.loads(_CACHE_FILE.read_text())
        except Exception:
            _memory_cache = {}
    return _memory_cache


def get_photos(trail_id: str, geometry: dict | None, trail: dict | None = None) -> dict:
    """Photos for one trail, cached. Distinguishes 'none found' from 'lookup failed'."""
    cache = _load_cache()

    with _cache_lock:
        if trail_id in cache:
            return {"status": "ok", "cached": True, "photos": cache[trail_id]}

    if not geometry:
        return {"status": "ok", "cached": False, "photos": []}

    try:
        photos = photos_for_trail({"id": trail_id, **(trail or {})}, geometry)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc), "photos": []}

    if photos is None:
        # Every sample point failed. Do not cache: a rate limit is not an answer.
        return {
            "status": "unavailable",
            "message": "Wikimedia Commons did not respond",
            "photos": [],
        }

    with _cache_lock:
        cache[trail_id] = photos
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _CACHE_FILE.write_text(json.dumps(cache))
        except Exception:
            pass  # an unwritable cache is not worth failing the request over

    return {"status": "ok", "cached": False, "photos": photos}
