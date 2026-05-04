"""Auth routes: register, login, logout, refresh, me, profile update."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planner.auth.cookies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
)
from planner.auth.deps import get_current_user
from planner.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from planner.db import get_session
from planner.models import User
from planner.rate_limit import limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    display_name: Optional[str] = None
    preferences: dict = {}

    class Config:
        from_attributes = True


class ProfileUpdateIn(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    preferences: Optional[dict] = None


def _issue_session(response: Response, user: User) -> None:
    set_auth_cookies(
        response,
        access=create_access_token(str(user.id)),
        refresh=create_refresh_token(str(user.id)),
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(
    request: Request,
    payload: RegisterIn,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    existing = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        preferences={},
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    _issue_session(response, user)
    return user


@router.post("/login", response_model=UserOut)
@limiter.limit("10/minute")
async def login(
    request: Request,
    payload: LoginIn,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    _issue_session(response, user)
    return user


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@router.post("/refresh", response_model=UserOut)
async def refresh(
    response: Response,
    bc_refresh: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if not bc_refresh:
        raise HTTPException(status_code=401, detail="No refresh token")
    payload = decode_token(bc_refresh, expected_type="refresh")
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Malformed token")
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    _issue_session(response, user)
    return user


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


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
    return user
