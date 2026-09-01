"""Firebase Admin initialization + ID-token verification."""

from __future__ import annotations

import logging
import os

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import auth as fb_auth, credentials

load_dotenv()
logger = logging.getLogger(__name__)

_app: firebase_admin.App | None = None
_init_failed = False


def _init_app() -> firebase_admin.App | None:
    """Initialize Firebase Admin, or return None when no credential is available.

    Discovery is public and must work without Firebase configured, so a missing or
    unreadable service account degrades to "nobody is signed in" rather than
    raising on every request. The failure is logged once, not per request.
    """
    global _app, _init_failed
    if _app is not None:
        return _app
    if _init_failed:
        return None

    sa_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")
    try:
        if sa_path and os.path.exists(sa_path) and os.path.getsize(sa_path) > 0:
            _app = firebase_admin.initialize_app(credentials.Certificate(sa_path))
        else:
            # Falls back to GOOGLE_APPLICATION_CREDENTIALS or ADC (GCP envs).
            _app = firebase_admin.initialize_app()
    except Exception as exc:
        _init_failed = True
        logger.warning(
            "Firebase Admin unavailable (%s). Sign-in is disabled; public "
            "endpoints still work. Place a service account JSON at %s to enable it.",
            exc,
            sa_path or "$FIREBASE_SERVICE_ACCOUNT_PATH",
        )
        return None

    logger.info("Firebase Admin initialized (project=%s)", _app.project_id)
    return _app


def verify_id_token(token: str) -> dict | None:
    """Return the decoded claims dict, or None if the token is invalid/expired."""
    try:
        if _init_app() is None:
            return None
        return fb_auth.verify_id_token(token, check_revoked=False)
    except Exception as exc:  # InvalidIdTokenError, ExpiredIdTokenError, etc.
        logger.debug("ID token verify failed: %s", exc)
        return None
