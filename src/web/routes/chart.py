"""Chart indicators API (Option B viewer overlays)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.config.settings import Settings, get_settings
from src.web.services import chart_service

router = APIRouter(
    prefix="/api/v1/chart",
    tags=["chart"],
)


@router.get("/indicators")
def indicators(_: Settings = Depends(get_settings)) -> dict:
    """Overlay-ready indicator series (price overlays + oscillator panes)."""
    return chart_service.chart_indicators()
