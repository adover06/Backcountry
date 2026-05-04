"""Helpers for setting auth cookies consistently."""

from __future__ import annotations

import os

from fastapi import Response

from planner.auth.security import ACCESS_TTL_MIN, REFRESH_TTL_DAYS

COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN") or None
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
ACCESS_COOKIE = "bc_access"
REFRESH_COOKIE = "bc_refresh"


def _set(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        domain=COOKIE_DOMAIN,
        path="/",
    )


def set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    _set(response, ACCESS_COOKIE, access, ACCESS_TTL_MIN * 60)
    _set(response, REFRESH_COOKIE, refresh, REFRESH_TTL_DAYS * 24 * 60 * 60)


def clear_auth_cookies(response: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(key=name, domain=COOKIE_DOMAIN, path="/")
