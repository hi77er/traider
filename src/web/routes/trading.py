"""Trading on/off API — the master switch behind the Execution panel.

Reads are always allowed. Turning trading ON is refused when the execution
configuration could not actually place an order, and requires an explicit
confirmation when the strategy routes to the LIVE account. Turning it OFF is
always allowed, because that is the action that releases the configuration lock.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.config.effective import get_effective_settings_dep
from src.web.auth import require_auth
from src.web.services import trading_service

router = APIRouter(
    prefix="/api/v1/trading",
    tags=["trading"],
    dependencies=[Depends(require_auth)],
)


class TradingStart(BaseModel):
    """Turning trading on. ``confirm_live`` is the acknowledgement that a LIVE
    strategy is about to send real orders."""

    confirm_live: bool = False


@router.get("")
def get_trading(settings=Depends(get_effective_settings_dep)) -> dict:
    """Trading state, the resolved execution target, and the env dropdown options."""
    return trading_service.payload(settings)


@router.post("/on")
def turn_trading_on(
    body: Optional[TradingStart] = None, settings=Depends(get_effective_settings_dep)
) -> dict:
    """Start trading. Refused (as ``ok: false``) when misconfigured, or when the
    environment is LIVE and ``confirm_live`` was not set."""
    return trading_service.turn_on(settings, confirm_live=bool(body and body.confirm_live))


@router.post("/off")
def turn_trading_off(settings=Depends(get_effective_settings_dep)) -> dict:
    """Stop trading and unlock configuration."""
    return trading_service.turn_off(settings)
