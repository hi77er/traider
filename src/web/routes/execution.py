"""Execution status API: which broker, and which environment, orders go to.

Read-only and non-raising by design. The dashboard badge needs to be able to say
"orders would be refused, and why" — which is far more useful than a 500 because
a credential has not been configured yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.config.effective import get_effective_settings_dep
from src.execution.config import execution_status
from src.web.auth import require_auth

router = APIRouter(
    prefix="/api/v1/execution",
    tags=["execution"],
    dependencies=[Depends(require_auth)],
)


@router.get("/status")
def get_status(settings=Depends(get_effective_settings_dep)) -> dict:
    """The resolved broker + paper/live environment, and whether orders are allowed.

    Reflects the ACTIVE strategy's ``EXECUTION_ENV`` overlaid on the account's
    broker/credentials, so switching strategy can change the answer — which is
    the whole point of scoping the mode per strategy.
    """
    return execution_status(settings)
