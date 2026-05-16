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


def _init_app() -> firebase_admin.App:
    global _app
    if _app is not None:
        return _app
    sa_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")
    if sa_path and os.path.exists(sa_path):
        cred = credentials.Certificate(sa_path)
        _app = firebase_admin.initialize_app(cred)
    else:
        # Falls back to GOOGLE_APPLICATION_CREDENTIALS or ADC. Useful in GCP envs.
        _app = firebase_admin.initialize_app()
    logger.info("Firebase Admin initialized (project=%s)", _app.project_id)
    return _app


def verify_id_token(token: str) -> dict | None:
    """Return the decoded claims dict, or None if the token is invalid/expired."""
    try:
        _init_app()
        return fb_auth.verify_id_token(token, check_revoked=False)
    except Exception as exc:  # InvalidIdTokenError, ExpiredIdTokenError, etc.
        logger.debug("ID token verify failed: %s", exc)
        return None
