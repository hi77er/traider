"""Configuration (``.env``) API endpoints for the Web Portal."""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.web.auth import require_auth
from src.web.services import config_service
from src.web.services.trading_service import require_trading_off

router = APIRouter(
    prefix="/api/v1/config",
    tags=["config"],
    dependencies=[Depends(require_auth)],
)


class ConfigUpdate(BaseModel):
    """Submitted form values, keyed by env var name (e.g. ``INSTRUMENT``)."""

    values: Dict[str, str]


@router.get("")
def get_config() -> dict:
    """Return the editable config schema (sections/fields, secrets masked)."""
    return config_service.get_config_schema()


@router.post("", dependencies=[Depends(require_trading_off)])
def update_config(body: ConfigUpdate) -> dict:
    """Merge submitted values into ``.env`` and revalidate the whole config.

    Refused while trading is on: a running strategy must not be reconfigured.
    """
    return config_service.update_config(body.values)
