"""Account settings (``data/account/account.json``) API endpoints.

These are the settings that belong to the trading ACCOUNT rather than to one
strategy: the broker it trades through (Trading Account), where its data and
backtest results are stored, and the backtest defaults. They are shared by every
strategy and stored in a JSON file (never in ``.env``), so a strategy's own
values can still override them.
"""

from __future__ import annotations

from typing import Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.web.services import config_service
from src.web.services.trading_service import require_trading_off

router = APIRouter(
    prefix="/api/v1/account",
    tags=["account"],
)


class AccountUpdate(BaseModel):
    """Submitted form values, keyed by env var name (e.g. ``DATA_DIR``)."""

    values: Dict[str, str]


class VerifyRequest(BaseModel):
    """Which environment's credentials to check against Alpaca.

    ``key_id``/``secret`` carry what is currently typed in the form, so Validate
    checks the values on screen rather than only the saved ones; blank or masked
    fields fall back to the stored pair, exactly like a save.
    """

    env: str
    key_id: Optional[str] = None
    secret: Optional[str] = None


@router.get("")
def get_account() -> dict:
    """Return the account settings schema (groups/fields, secrets masked)."""
    return config_service.get_account_schema()


@router.post("/verify")
def verify_credentials(body: VerifyRequest) -> dict:
    """Ask Alpaca whether one environment's key pair actually works.

    Deliberately NOT blocked by the trading lock. It writes a verdict about a
    credential, not configuration, and being able to re-check a key while trading
    is on is the point: a revoked key should be discoverable rather than hidden
    behind a locked panel.
    """
    env = (body.env or "").strip().lower()
    if env not in ("paper", "live"):
        return {"ok": False, "message": "env must be 'paper' or 'live'", "errors": ["unknown environment"]}
    return config_service.verify_credentials(env, body.key_id, body.secret)


@router.post("", dependencies=[Depends(require_trading_off)])
def update_account(body: AccountUpdate) -> dict:
    """Validate and persist the account settings to the JSON file.

    Refused while trading is on — credentials are part of what a running
    strategy is using. A credential pair that has never been verified is checked
    against Alpaca as part of the save, so adding keys is the moment they are
    proved; an unchanged, already-verified pair costs no call.
    """
    return config_service.update_account(body.values)
