"""Discovery API — find a hike by what you want from it.

This is the primary surface of the app. The planner's condition checks still exist
and are reachable per-trail, but they are an overlay on discovery rather than the
product.

Browsing is intentionally open (optional auth). Trail geometry is public reference
data; requiring a login to look at a map is hostile and buys no security. Endpoints
that touch a user's own data still require authentication.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from planner import discover, graph_service
from planner.auth.deps import get_current_user_optional
from planner.models import User
from planner.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discover", tags=["discovery"])

# Guard rails on client-supplied numbers.
MAX_LIMIT = 200
MAX_MAP_FEATURES = 600


def _parse_range(raw: Optional[str], name: str) -> list[float] | None:
    """Parse a 'min,max' query parameter. Either side may be blank for open-ended."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail=f"{name} must be 'min,max'")
    bounds: list[float | None] = []
    for part in parts:
        part = part.strip()
        if not part:
            bounds.append(None)
            continue
        try:
            bounds.append(float(part))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{name} must be numeric")
    low, high = bounds
    if low is not None and high is not None and low > high:
        raise HTTPException(status_code=400, detail=f"{name} min exceeds max")
    return [low, high]


def _parse_bbox(raw: Optional[str]) -> list[float] | None:
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must be 'minLng,minLat,maxLng,maxLat'")
    try:
        bbox = [float(p) for p in parts]
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox values must be numeric")
    if not (-180 <= bbox[0] <= 180 and -180 <= bbox[2] <= 180):
        raise HTTPException(status_code=400, detail="bbox longitude out of range")
    if not (-90 <= bbox[1] <= 90 and -90 <= bbox[3] <= 90):
        raise HTTPException(status_code=400, detail="bbox latitude out of range")
    if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
        raise HTTPException(status_code=400, detail="bbox min exceeds max")
    return bbox


def _parse_list(raw: Optional[str]) -> list[str] | None:
    if not raw:
        return None
    values = [v.strip().lower() for v in raw.split(",") if v.strip()]
    return values or None


def _common_filters(
    q: str,
    bbox: Optional[str],
    length: Optional[str],
    gain: Optional[str],
    elevation: Optional[str],
    difficulty: Optional[str],
    steepness: Optional[str],
    features: Optional[str],
    features_mode: str,
    route_type: Optional[str],
    month: Optional[int],
    activity: Optional[str],
    wilderness: bool,
    accessible: bool,
) -> dict:
    if month is not None and not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be 1-12")
    if features_mode not in ("any", "all"):
        raise HTTPException(status_code=400, detail="features_mode must be 'any' or 'all'")

    return {
        "q": (q or "").strip(),
        "bbox": _parse_bbox(bbox),
        "length_mi": _parse_range(length, "length"),
        "gain_ft": _parse_range(gain, "gain"),
        "max_elevation_ft": _parse_range(elevation, "elevation"),
        "difficulty": _parse_list(difficulty),
        "steepness": _parse_list(steepness),
        "features": _parse_list(features),
        "features_mode": features_mode,
        "route_type": route_type,
        "month": month,
        "activity": activity,
        "wilderness_only": wilderness,
        "accessible_only": accessible,
    }


@router.get("/status")
@limiter.limit("60/minute")
async def discovery_status(request: Request):
    """What the index contains, and what it is missing.

    Exposed deliberately: the UI uses this to say "scenery data not yet built"
    rather than showing an empty result set as though nothing matched.
    """
    return discover.index_status()


@router.get("/search")
@limiter.limit("120/minute")
async def discovery_search(
    request: Request,
    q: str = "",
    bbox: Optional[str] = Query(None, description="minLng,minLat,maxLng,maxLat"),
    length: Optional[str] = Query(None, description="miles, 'min,max'"),
    gain: Optional[str] = Query(None, description="feet, 'min,max'"),
    elevation: Optional[str] = Query(None, description="max elevation ft, 'min,max'"),
    difficulty: Optional[str] = Query(None, description="easy,moderate,hard,strenuous"),
    steepness: Optional[str] = Query(None, description="gentle,moderate,steep,very steep"),
    features: Optional[str] = Query(None, description="lake,waterfall,peak,viewpoint,..."),
    features_mode: str = Query("any", description="'any' or 'all'"),
    route_type: Optional[str] = Query(None, description="loop or out-and-back"),
    month: Optional[int] = Query(None, description="1-12, filters by seasonal access"),
    activity: Optional[str] = Query(None, description="hiking, bike, horse, ..."),
    wilderness: bool = False,
    accessible: bool = False,
    sort: str = "relevance",
    limit: int = 50,
    offset: int = 0,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Faceted trail search. Returns results plus facet counts for the filter UI."""
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    filters = _common_filters(
        q, bbox, length, gain, elevation, difficulty, steepness, features,
        features_mode, route_type, month, activity, wilderness, accessible,
    )

    try:
        return discover.search(sort=sort, limit=limit, offset=offset, **filters)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/map")
@limiter.limit("120/minute")
async def discovery_map(
    request: Request,
    q: str = "",
    bbox: Optional[str] = Query(None),
    length: Optional[str] = Query(None),
    gain: Optional[str] = Query(None),
    elevation: Optional[str] = Query(None),
    difficulty: Optional[str] = Query(None),
    steepness: Optional[str] = Query(None),
    features: Optional[str] = Query(None),
    features_mode: str = "any",
    route_type: Optional[str] = Query(None),
    month: Optional[int] = Query(None),
    activity: Optional[str] = Query(None),
    wilderness: bool = False,
    accessible: bool = False,
    limit: int = 400,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """GeoJSON for the map, using the same filters as /search.

    `truncated` is returned so the UI can tell the user results were capped rather
    than letting them believe they are seeing everything that matched.
    """
    limit = max(1, min(limit, MAX_MAP_FEATURES))
    filters = _common_filters(
        q, bbox, length, gain, elevation, difficulty, steepness, features,
        features_mode, route_type, month, activity, wilderness, accessible,
    )
    try:
        return discover.map_features(limit=limit, **filters)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/facets")
@limiter.limit("120/minute")
async def discovery_facets(
    request: Request,
    bbox: Optional[str] = Query(None),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Facet counts for the current viewport with no other filters applied."""
    try:
        result = discover.search(bbox=_parse_bbox(bbox), limit=0)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"total": result["total"], "facets": result["facets"]}


@router.get("/graph/status")
@limiter.limit("60/minute")
async def graph_status(request: Request):
    """Graph size and build state. Does not trigger a build."""
    return graph_service.status()


@router.get("/graph/route")
@limiter.limit("30/minute")
async def graph_route(
    request: Request,
    start: str = Query(..., description="lng,lat"),
    end: str = Query(..., description="lng,lat"),
    out_and_back: bool = True,
    snap: float = Query(0.25, description="miles; how far to look for a trail"),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Compose a hike between two points on the trail network.

    The first call builds the graph (~14s) and later calls reuse it.
    """
    def _point(raw: str, label: str) -> tuple[float, float]:
        try:
            lng, lat = (float(v) for v in raw.split(","))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{label} must be 'lng,lat'")
        if not (-180 <= lng <= 180) or not (-90 <= lat <= 90):
            raise HTTPException(status_code=400, detail=f"{label} out of range")
        return lng, lat

    return graph_service.compose(
        _point(start, "start"),
        _point(end, "end"),
        out_and_back=out_and_back,
        snap_miles=max(0.02, min(snap, 2.0)),
    )


@router.get("/trail/{trail_id}/photos")
@limiter.limit("60/minute")
async def discovery_trail_photos(
    request: Request,
    trail_id: str,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """CC-licensed photos near a trail, from Wikimedia Commons.

    Resolved on demand and cached per trail. Photos are ranked by how likely the
    filename is to describe the place rather than by proximity alone — proximity
    alone surfaces macro shots of plants taken beside the trail.

    Each photo carries its licence and whether attribution is required; the caller
    must credit CC-BY images at display time.
    """
    try:
        trail = discover.get_trail(trail_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not trail:
        raise HTTPException(status_code=404, detail="Trail not found")

    from pipeline.photos import get_photos

    entry = discover.get_geometry(trail_id) or {}
    return get_photos(trail_id, entry.get("geometry"), trail)


@router.get("/trail/{trail_id}")
@limiter.limit("120/minute")
async def discovery_trail(
    request: Request,
    trail_id: str,
    include_geometry: bool = True,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Full detail for one trail, including elevation profile and geometry."""
    try:
        trail = discover.get_trail(trail_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not trail:
        raise HTTPException(status_code=404, detail="Trail not found")

    payload = discover._public(trail)
    entry = discover.get_geometry(trail_id) or {}
    if include_geometry:
        payload["geometry"] = entry.get("geometry")
    if entry.get("profile"):
        payload.setdefault("elevation", {})
        payload["elevation"] = {**(payload.get("elevation") or {}), "profile": entry["profile"]}
    return payload
