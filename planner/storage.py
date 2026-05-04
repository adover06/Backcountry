"""GPX file storage on local filesystem, scoped per user_id."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "./var/gpx")).resolve()


def _user_dir(user_id: uuid.UUID) -> Path:
    p = STORAGE_ROOT / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def gpx_path_for(user_id: uuid.UUID, trip_id: uuid.UUID) -> Path:
    return _user_dir(user_id) / f"{trip_id}.gpx"


def write_gpx(user_id: uuid.UUID, trip_id: uuid.UUID, data: bytes) -> str:
    """Write GPX bytes; return path relative to STORAGE_ROOT."""
    full = gpx_path_for(user_id, trip_id)
    full.write_bytes(data)
    return str(full.relative_to(STORAGE_ROOT))


def read_gpx(user_id: uuid.UUID, trip_id: uuid.UUID) -> bytes | None:
    full = gpx_path_for(user_id, trip_id)
    if not full.exists():
        return None
    return full.read_bytes()


def delete_gpx(user_id: uuid.UUID, trip_id: uuid.UUID) -> None:
    full = gpx_path_for(user_id, trip_id)
    if full.exists():
        full.unlink()
