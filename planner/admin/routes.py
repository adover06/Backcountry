"""Admin routes: mint / list / revoke single-use invite codes."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from planner.auth.deps import get_admin_user
from planner.db import get_session
from planner.models import InviteCode, SavedTrail, ShareToken, TripPlan, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


class MetricsOut(BaseModel):
    users: int
    trips: int
    saved_trails: int
    invites_total: int
    invites_redeemed: int
    invites_available: int
    active_share_links: int


@router.get("/metrics", response_model=MetricsOut)
async def metrics(
    _admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
):
    async def _count(stmt) -> int:
        return int(await session.scalar(stmt) or 0)

    users = await _count(select(func.count()).select_from(User))
    trips = await _count(select(func.count()).select_from(TripPlan))
    saved = await _count(select(func.count()).select_from(SavedTrail))
    invites_total = await _count(select(func.count()).select_from(InviteCode))
    invites_redeemed = await _count(
        select(func.count()).select_from(InviteCode).where(InviteCode.redeemed_by.isnot(None))
    )
    active_shares = await _count(
        select(func.count()).select_from(ShareToken).where(ShareToken.revoked_at.is_(None))
    )
    return MetricsOut(
        users=users,
        trips=trips,
        saved_trails=saved,
        invites_total=invites_total,
        invites_redeemed=invites_redeemed,
        invites_available=invites_total - invites_redeemed,
        active_share_links=active_shares,
    )


class InviteOut(BaseModel):
    code: str
    note: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None
    redeemed_by: Optional[str] = None
    redeemed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InviteCreateIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=255)
    code: Optional[str] = Field(default=None, min_length=4, max_length=64)


def _new_code() -> str:
    # 10-char url-safe, e.g. "X7r2qLm4Tn". Plenty of entropy for an invite list.
    return secrets.token_urlsafe(8)[:10]


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(
    _admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(InviteCode).order_by(InviteCode.created_at.desc())
    )
    return list(rows.all())


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
async def mint_invite(
    payload: InviteCreateIn,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
):
    code = (payload.code or _new_code()).strip()
    existing = await session.get(InviteCode, code)
    if existing:
        raise HTTPException(status_code=409, detail="Code already exists")
    invite = InviteCode(code=code, note=payload.note, created_by=admin.id)
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return invite


@router.delete("/invites/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    code: str,
    _admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
):
    invite = await session.get(InviteCode, code)
    if not invite:
        raise HTTPException(status_code=404, detail="No such code")
    if invite.redeemed_by:
        raise HTTPException(status_code=409, detail="Already redeemed; cannot revoke")
    await session.delete(invite)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
