"""FastAPI dependencies for Firebase-backed authentication."""

from __future__ import annotations

import os

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from planner.auth.firebase import verify_id_token
from planner.db import get_session
from planner.models import User


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def get_firebase_claims(authorization: str | None = Header(default=None)) -> dict:
    """Verified Firebase token claims, or 401."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    claims = verify_id_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return claims


async def get_firebase_claims_optional(
    authorization: str | None = Header(default=None),
) -> dict | None:
    token = _bearer(authorization)
    if not token:
        return None
    return verify_id_token(token)


async def get_current_user(
    claims: dict = Depends(get_firebase_claims),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Look up the User row. Returns 403 if the Firebase user has no app account yet
    (they must complete invite-gated signup via POST /api/auth/me first)."""
    user = await session.get(User, claims["uid"])
    if not user:
        raise HTTPException(
            status_code=403,
            detail="No account for this Firebase user. Redeem an invite code first.",
        )
    return user


async def get_current_user_optional(
    claims: dict | None = Depends(get_firebase_claims_optional),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    if not claims:
        return None
    return await session.get(User, claims["uid"])


def _admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


async def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if (user.email or "").lower() not in _admin_emails():
        raise HTTPException(status_code=403, detail="Admin only")
    return user
