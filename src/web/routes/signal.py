"""Rule-based signal API for the Strategy lab (test the rule engine from the UI)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.config.settings import Settings, get_settings
from src.web.services import signal_service

router = APIRouter(
    prefix="/api/v1/signal",
    tags=["signal"],
)


@router.get("")
def signal_summary(_: Settings = Depends(get_settings)) -> dict:
    """Latest decision + per-bar series from the active strategy's rules."""
    return signal_service.signal_payload()
