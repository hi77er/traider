"""Execution status API: which environment orders go to, and how to change it.

Read-only status plus ONE narrow write path. The paper/live switch used to be a
field in the strategy panel; it is now a header dropdown, so it gets its own
endpoint instead of being smuggled through a full ruleset save. Changing it while
trading is ON is refused — switching a running strategy from paper to live is the
single most dangerous edit in the app.

``GET /api/v1/execution/status``  -> resolved target + whether orders are allowed
``POST /api/v1/execution/env``    -> set the ACTIVE strategy's environment
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.config.effective import get_effective_settings_dep
from src.execution.config import execution_status
from src.web.services import rules_service
from src.web.services import trading_service
from src.web.services.trading_service import require_trading_off

router = APIRouter(
    prefix="/api/v1/execution",
    tags=["execution"],
)


class EnvRequest(BaseModel):
    """The environment the ACTIVE strategy's orders should go to."""

    env: str


@router.get("/status")
def get_status(settings=Depends(get_effective_settings_dep)) -> dict:
    """The resolved broker + paper/live environment, and whether orders are allowed.

    Reflects the ACTIVE strategy's ``EXECUTION_ENV`` overlaid on the account's
    credentials, so switching strategy can change the answer — which is the whole
    point of scoping the mode per strategy.
    """
    return execution_status(settings)


@router.post(
    "/env",
    dependencies=[
        Depends(require_trading_off),
        # Switching accounts while one of them holds a position puts that position beyond
        # this UI's reach: the screen would be pointed at the other account, and the
        # flatten button would be looking somewhere else. So the switch is what has to
        # wait — not the position.
        Depends(trading_service.require_flat("The environment cannot be switched while a position is open")),
    ],
)
def set_env(body: EnvRequest, settings=Depends(get_effective_settings_dep)) -> dict:
    """Point the active strategy's orders at ``paper`` (safe) or ``live`` (real money)."""
    return rules_service.set_execution_env(settings, body.env)
