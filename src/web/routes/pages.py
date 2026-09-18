"""Page routes for the Web Portal."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse


router = APIRouter()

_INDEX = Path(__file__).resolve().parents[1] / "templates" / "index.html"
_MARKET = Path(__file__).resolve().parents[1] / "templates" / "market.html"


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the dashboard page."""
    return FileResponse(_INDEX)


@router.get("/market", include_in_schema=False)
def market() -> FileResponse:
    """Serve the whole-market landing page (screener, gainers, volume, small caps)."""
    return FileResponse(_MARKET)


@router.get("/api/v1/health", include_in_schema=False)
def health() -> dict:
    """Simple liveness check for monitoring / load-balancer probes."""
    return {"status": "ok"}
