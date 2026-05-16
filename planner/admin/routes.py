"""Admin routes: mint / list / revoke single-use invite codes."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planner.auth.deps import get_admin_user
from planner.db import get_session
from planner.models import InviteCode, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
