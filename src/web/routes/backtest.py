"""Backtest API endpoints for the Web Portal.

``GET /api/v1/backtest``        -> status + latest result
``POST /api/v1/backtest/run``   -> start a backtest of the ACTIVE strategy
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.web.auth import require_auth
from src.web.services import backtest_service

router = APIRouter(
    prefix="/api/v1/backtest",
    tags=["backtest"],
    dependencies=[Depends(require_auth)],
)


@router.get("")
def get_backtest() -> dict:
    """Current job status and the most recent backtest result."""
    return backtest_service.payload()


@router.post("/run")
def run_backtest() -> dict:
    """Run a new backtest against the active strategy's saved rules/config."""
    return backtest_service.start_backtest()
