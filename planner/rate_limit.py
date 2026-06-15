"""Shared slowapi rate limiter.

Behind Cloudflare Tunnel + nginx, ``request.client.host`` is always the nginx
container address, so the default key would rate-limit every user as one bucket.
Prefer the real client IP from the proxy headers (Cloudflare sets
``CF-Connecting-IP``; nginx forwards ``X-Forwarded-For``).
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Left-most entry is the original client.
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip)
