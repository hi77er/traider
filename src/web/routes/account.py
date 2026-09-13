"""Account settings (``settings/account/account.json``) API endpoints.

These are the settings that belong to the trading ACCOUNT rather than to one
strategy: the broker it trades through (Trading Account), where its data and
backtest results are stored, and the backtest defaults. They are shared by every
strategy and stored in a JSON file (never in ``.env``), so a strategy's own
values can still override them.
"""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.web.auth import require_auth
from src.web.services import config_service

router = APIRouter(
    prefix="/api/v1/account",
    tags=["account"],
    dependencies=[Depends(require_auth)],
)


class AccountUpdate(BaseModel):
    """Submitted form values, keyed by env var name (e.g. ``DATA_DIR``)."""

    values: Dict[str, str]


@router.get("")
def get_account() -> dict:
    """Return the account settings schema (groups/fields, secrets masked)."""
    return config_service.get_account_schema()


@router.post("")
def update_account(body: AccountUpdate) -> dict:
    """Validate and persist the account settings to the JSON file."""
    return config_service.update_account(body.values)
