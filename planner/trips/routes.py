"""Trip CRUD, GPX, share-token, and saved-trail routes."""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from planner.auth.deps import get_current_user
from planner.db import get_session
from planner.models import SavedTrail, ShareToken, TripPlan, User
from planner.storage import delete_gpx, read_gpx, write_gpx

router = APIRouter(prefix="/api", tags=["trips"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class TripCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    route: Optional[dict] = None
    selected_trail: Optional[dict] = None
    checks: Optional[dict] = None
    report: Optional[dict] = None


class TripUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    route: Optional[dict] = None
    selected_trail: Optional[dict] = None
    checks: Optional[dict] = None
    report: Optional[dict] = None


class TripSummary(BaseModel):
    id: uuid.UUID
    name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    selected_trail: Optional[dict] = None
    created_at: datetime
    updated_at: datetime
    has_gpx: bool = False
    share_token: Optional[str] = None

    class Config:
        from_attributes = True


class TripDetail(TripSummary):
    route: Optional[dict] = None
    checks: Optional[dict] = None
    report: Optional[dict] = None


class ShareOut(BaseModel):
    token: str


class SaveTrailIn(BaseModel):
    trail_id: str = Field(min_length=1, max_length=128)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_summary(trip: TripPlan) -> TripSummary:
    return TripSummary(
        id=trip.id,
        name=trip.name,
        start_date=trip.start_date,
        end_date=trip.end_date,
        selected_trail=trip.selected_trail,
        created_at=trip.created_at,
        updated_at=trip.updated_at,
        has_gpx=bool(trip.gpx_path),
        share_token=(
            trip.share_token.token
            if trip.share_token and trip.share_token.revoked_at is None
            else None
        ),
    )


def _to_detail(trip: TripPlan) -> TripDetail:
    return TripDetail(
        id=trip.id,
        name=trip.name,
        start_date=trip.start_date,
        end_date=trip.end_date,
        route=trip.route,
        selected_trail=trip.selected_trail,
        checks=trip.checks,
        report=trip.report,
        created_at=trip.created_at,
        updated_at=trip.updated_at,
        has_gpx=bool(trip.gpx_path),
        share_token=(
            trip.share_token.token
            if trip.share_token and trip.share_token.revoked_at is None
            else None
        ),
    )


async def _load_owned_trip(
    trip_id: uuid.UUID, user: User, session: AsyncSession
) -> TripPlan:
    trip = await session.scalar(
        select(TripPlan)
        .where(TripPlan.id == trip_id)
        .options(selectinload(TripPlan.share_token))
    )
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    if trip.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your trip")
    return trip


# ── Trip CRUD ────────────────────────────────────────────────────────────────

@router.get("/trips", response_model=list[TripSummary])
async def list_trips(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(TripPlan)
        .where(TripPlan.user_id == user.id)
        .options(selectinload(TripPlan.share_token))
        .order_by(TripPlan.created_at.desc())
    )
    return [_to_summary(t) for t in rows.all()]


@router.post("/trips", response_model=TripDetail, status_code=status.HTTP_201_CREATED)
async def create_trip(
    payload: TripCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = TripPlan(
        user_id=user.id,
        name=payload.name,
        start_date=payload.start_date,
        end_date=payload.end_date,
        route=payload.route,
        selected_trail=payload.selected_trail,
        checks=payload.checks,
        report=payload.report,
    )
    session.add(trip)
    await session.commit()
    await session.refresh(trip, attribute_names=["share_token"])
    return _to_detail(trip)


@router.get("/trips/{trip_id}", response_model=TripDetail)
async def get_trip(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = await _load_owned_trip(trip_id, user, session)
    return _to_detail(trip)


@router.patch("/trips/{trip_id}", response_model=TripDetail)
async def update_trip(
    trip_id: uuid.UUID,
    payload: TripUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = await _load_owned_trip(trip_id, user, session)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(trip, field, value)
    await session.commit()
    await session.refresh(trip, attribute_names=["share_token"])
    return _to_detail(trip)


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = await _load_owned_trip(trip_id, user, session)
    delete_gpx(user.id, trip.id)
    await session.delete(trip)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── GPX file ─────────────────────────────────────────────────────────────────

@router.get("/trips/{trip_id}/gpx")
async def download_gpx(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = await _load_owned_trip(trip_id, user, session)
    if not trip.gpx_path:
        raise HTTPException(status_code=404, detail="No GPX stored for this trip")
    data = read_gpx(user.id, trip.id)
    if not data:
        raise HTTPException(status_code=404, detail="GPX file missing on disk")
    return Response(
        content=data,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{trip.name}.gpx"'},
    )


# ── Share tokens ─────────────────────────────────────────────────────────────

@router.post("/trips/{trip_id}/share", response_model=ShareOut)
async def create_share(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = await _load_owned_trip(trip_id, user, session)
    if trip.share_token and trip.share_token.revoked_at is None:
        return ShareOut(token=trip.share_token.token)
    if trip.share_token and trip.share_token.revoked_at is not None:
        await session.delete(trip.share_token)
        await session.flush()
    token = secrets.token_urlsafe(24)
    st = ShareToken(token=token, trip_id=trip.id)
    session.add(st)
    await session.commit()
    return ShareOut(token=token)


@router.delete("/trips/{trip_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    trip = await _load_owned_trip(trip_id, user, session)
    if trip.share_token and trip.share_token.revoked_at is None:
        trip.share_token.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/trips/share/{token}", response_model=TripDetail)
async def get_shared_trip(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    st = await session.scalar(
        select(ShareToken)
        .where(ShareToken.token == token)
        .options(selectinload(ShareToken.trip).selectinload(TripPlan.share_token))
    )
    if not st or st.revoked_at is not None or not st.trip:
        raise HTTPException(status_code=404, detail="Share link not found")
    trip = st.trip
    # Redact: drop gpx_path; share_token field will round-trip but contains nothing sensitive.
    detail = _to_detail(trip)
    return detail


# ── Saved trails ─────────────────────────────────────────────────────────────

@router.get("/saved-trails", response_model=list[str])
async def list_saved_trails(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(SavedTrail.trail_id)
        .where(SavedTrail.user_id == user.id)
        .order_by(SavedTrail.saved_at.desc())
    )
    return list(rows.all())


@router.post("/saved-trails", status_code=status.HTTP_201_CREATED)
async def save_trail(
    payload: SaveTrailIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    existing = await session.get(SavedTrail, (user.id, payload.trail_id))
    if existing:
        return {"ok": True, "already": True}
    session.add(SavedTrail(user_id=user.id, trail_id=payload.trail_id))
    await session.commit()
    return {"ok": True, "already": False}


@router.delete("/saved-trails/{trail_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_trail(
    trail_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await session.execute(
        delete(SavedTrail).where(
            SavedTrail.user_id == user.id, SavedTrail.trail_id == trail_id
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
