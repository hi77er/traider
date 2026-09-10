"""Dataset dashboard API endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.config.settings import Settings, get_settings
from src.web.auth import require_auth
from src.web.services import dataset_service

router = APIRouter(
    prefix="/api/v1/dataset",
    tags=["dataset"],
    dependencies=[Depends(require_auth)],
)


@router.get("/status")
def status(_: Settings = Depends(get_settings)) -> dict:
    """Report whether the initial dataset exists (and summary stats)."""
    return dataset_service.dataset_status()


@router.get("/data")
def data(
    start: Optional[str] = Query(default=None, description="Include rows on/after this date"),
    end: Optional[str] = Query(default=None, description="Include rows on/before this date"),
    limit: int = Query(default=100, ge=0, description="Rows per page; 0 = all"),
    offset: int = Query(default=0, ge=0, description="Row offset for pagination"),
    _: Settings = Depends(get_settings),
) -> dict:
    """Return dataset rows (paginated) for the table and chart."""
    return dataset_service.get_rows(start=start, end=end, limit=limit, offset=offset)


@router.post("/backfill")
def backfill(_: Settings = Depends(get_settings)) -> dict:
    """Trigger the initial historical-data download (async, once at a time)."""
    return dataset_service.start_backfill()


@router.get("/rebuild")
def rebuild_job(_: Settings = Depends(get_settings)) -> dict:
    """Status of the history-window re-download (started via POST /rebuild)."""
    return dataset_service.rebuild_status()


@router.post("/rebuild")
def start_rebuild(
    old_bar_size: Optional[str] = Query(default=None, description="Bar size the dataset previously used"),
    _: Settings = Depends(get_settings),
) -> dict:
    """Delete the old dataset and download the newly configured window."""
    return dataset_service.start_rebuild(old_bar_size=old_bar_size)
