"""Auth routes — Firebase token in, app User out. Single-use invite code on first signup."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planner.auth.deps import (
    get_admin_user,
    get_current_user,
    get_firebase_claims,
)
from planner.db import get_session
from planner.models import InviteCode, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class UserOut(BaseModel):
    id: str
    email: str
    display_name: Optional[str] = None
    preferences: dict = {}
    is_admin: bool = False

    class Config:
        from_attributes = True


class SignupIn(BaseModel):
    invite_code: str = Field(min_length=4, max_length=64)


class ProfileUpdateIn(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    preferences: Optional[dict] = None


def _admin_emails() -> set[str]:
    return {
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "").split(",")
        if e.strip()
    }


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        preferences=user.preferences or {},
        is_admin=(user.email or "").lower() in _admin_emails(),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return _to_out(user)


@router.patch("/me", response_model=UserOut)
async def update_me(
    payload: ProfileUpdateIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.preferences is not None:
        user.preferences = {**(user.preferences or {}), **payload.preferences}
    await session.commit()
    await session.refresh(user)
    return _to_out(user)


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupIn,
    claims: dict = Depends(get_firebase_claims),
    session: AsyncSession = Depends(get_session),
):
    """First-time signup: requires a valid, unredeemed invite code.

    Existing users hitting this endpoint just get their record back idempotently.
    """
    uid = claims["uid"]
    email = (claims.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=400, detail="Firebase user has no email")
    # Admins can sign up without an invite code.
    is_admin = email in _admin_emails()

    existing = await session.get(User, uid)
    if existing:
        return _to_out(existing)

    code: InviteCode | None = None
    if not is_admin:
        code = await session.get(InviteCode, payload.invite_code.strip())
        if not code:
            raise HTTPException(status_code=400, detail="Invalid invite code")
        if code.redeemed_by:
            raise HTTPException(status_code=400, detail="Invite code already used")

    user = User(
        id=uid,
        email=email,
        display_name=claims.get("name"),
        preferences={},
    )
    session.add(user)
    if code is not None:
        code.redeemed_by = uid
        code.redeemed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(user)
    return _to_out(user)
