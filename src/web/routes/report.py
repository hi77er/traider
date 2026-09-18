"""Backtest report page + its data endpoints.

``GET /report``              -> the standalone report page (one strategy, all its runs)
``GET /api/v1/report/runs``  -> the run menu for a strategy
``GET /api/v1/report/run``   -> the full report for one run (newest by default)
``DELETE /api/v1/report/run``-> remove one stored run from disk
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from src.web.services import report_service

router = APIRouter()

_REPORT_PAGE = Path(__file__).resolve().parents[1] / "templates" / "report.html"


@router.get("/report", include_in_schema=False)
def report_page() -> FileResponse:
    """Serve the standalone backtest report page."""
    return FileResponse(_REPORT_PAGE)


@router.get("/api/v1/report/runs", tags=["report"])
def get_report_runs(strategy: Optional[str] = Query(default=None)) -> dict:
    """The list of stored backtest runs for one strategy (newest first)."""
    return report_service.runs_payload(strategy)


@router.get("/api/v1/report/run", tags=["report"])
def get_report_run(
    strategy: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
) -> dict:
    """The full report for one run, plus the run menu for the selector."""
    return report_service.report_payload(strategy, run_id)


@router.delete("/api/v1/report/run", tags=["report"])
def delete_report_run(
    strategy: Optional[str] = Query(default=None),
    run_id: str = Query(..., description="The stored run to delete."),
) -> dict:
    """Delete one stored run (its file, index record and report output)."""
    return report_service.delete_report(strategy, run_id)
