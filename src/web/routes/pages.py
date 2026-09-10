"""Page routes for the Web Portal."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from src.web.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

_INDEX = Path(__file__).resolve().parents[1] / "templates" / "index.html"


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the dashboard page."""
    return FileResponse(_INDEX)


@router.get("/api/v1/health", include_in_schema=False)
def health() -> dict:
    """Simple liveness check for monitoring / load-balancer probes."""
    return {"status": "ok"}
