"""Daily Delta API endpoints (dataset sync status + fetch missing days)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.config.settings import Settings, get_settings
from src.web.auth import require_auth
from src.web.services import delta_service

router = APIRouter(
    prefix="/api/v1/delta",
    tags=["delta"],
    dependencies=[Depends(require_auth)],
)


@router.get("/status")
def delta_status(_: Settings = Depends(get_settings)) -> dict:
    """Report dataset sync state: missing completed days, last 5 bars, etc."""
    return delta_service.status()


@router.post("/sync")
def delta_sync(_: Settings = Depends(get_settings)) -> dict:
    """Fetch the missing completed days, update the dataset, return new state."""
    return delta_service.sync()
