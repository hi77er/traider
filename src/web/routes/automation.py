"""Instrument Automation API — the panel's criteria, its Top-10, and its ↻.

Deliberately NOT behind ``require_trading_off`` (the lock every other strategy-config write sits
behind): turning the automation OFF has to work while the loop it drives is trading, and a control
that can only be disarmed while nothing is running is not a control. The write itself places no
order — it changes which instrument the NEXT tick may pick, and the tick re-reads these criteria
every bar.

The preview the panel shows is the tick's own decision function, so the two cannot disagree about
what the criteria mean.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.web.services import automation_service

router = APIRouter(
    prefix="/api/v1/automation",
    tags=["instrument automation"],
)


class AutomationSave(BaseModel):
    """The criteria as the panel submits them: a switch and the two sets of named values."""

    on: bool = False
    enter: Dict[str, Any] = {}
    switch: Dict[str, Any] = {}


@router.get("")
def get_automation(settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """The criteria, the cached Top-10, and what the tick would decide with it."""
    return automation_service.payload(settings)


@router.post("")
def save_automation(
    body: AutomationSave, settings: Settings = Depends(get_effective_settings_dep)
) -> dict:
    """Store the criteria for the active strategy (allowed while trading is ON)."""
    return automation_service.save(settings, body.model_dump())


@router.post("/refresh")
def refresh_list(
    force: bool = True, settings: Settings = Depends(get_effective_settings_dep)
) -> dict:
    """Screen a new Top-10 now, instead of waiting for the list to age out."""
    return automation_service.refresh(settings, force=force)
