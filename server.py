"""FastAPI server wrapper for the trip planner API."""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

load_dotenv()

from planner.admin.routes import router as admin_router
from planner.auth.routes import router as auth_router
from planner.db import get_session
from planner.rate_limit import limiter
from planner.trips.routes import router as trips_router
from planner_api import router as planner_router

app = FastAPI(title="Backcountry API")

# CORS — frontend origin. In dev: http://localhost:5173.
_origins = [o.strip() for o in os.getenv("FRONTEND_ORIGIN", "http://localhost:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (used via @limiter.limit on routes that opt in).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(trips_router)
app.include_router(planner_router)


@app.get("/api/health")
async def health(session: AsyncSession = Depends(get_session)):
    """Liveness + DB readiness. Returns 503 if the database is unreachable."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - infra failure path
        return JSONResponse(status_code=503, content={"ok": False, "db": str(exc)})
    return {"ok": True, "db": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6767)
