"""Tiny in-memory TTL cache for external check results."""

from __future__ import annotations

import os
import threading
import time


class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._data: dict[object, tuple[float, object]] = {}

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl_seconds, value)

    def clear(self):
        with self._lock:
            self._data.clear()


def env_ttl_seconds(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
        return max(0, value)
    except Exception:
        return default
