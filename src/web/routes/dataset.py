"""Dataset API endpoints for the lab's chart, table and delta panel."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.config.settings import Settings
from src.config.effective import get_effective_settings_dep
from src.web.services import dataset_service
from src.web.services.trading_service import require_trading_off

router = APIRouter(
    prefix="/api/v1/dataset",
    tags=["dataset"],
)


# The settings these endpoints read are the ACTIVE strategy's effective ones, and they are
# passed down: every service here can resolve them itself, but a route that injects a settings
# object and then lets the service guess its own is a route no test can point at another
# dataset — which is how a test that wrote its bars into a temp directory came to be reading
# the real one, and passing only while the active strategy happened to hold the same session.


@router.get("/status")
def status(settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Report whether the initial dataset exists (and summary stats)."""
    return dataset_service.dataset_status(settings)


@router.get("/data")
def data(
    start: Optional[str] = Query(default=None, description="Include rows on/after this date"),
    end: Optional[str] = Query(default=None, description="Include rows on/before this date"),
    limit: int = Query(default=100, ge=0, description="Rows per page; 0 = all"),
    offset: int = Query(default=0, ge=0, description="Row offset for pagination"),
    settings: Settings = Depends(get_effective_settings_dep),
) -> dict:
    """Return dataset rows (paginated) for the table and chart."""
    return dataset_service.get_rows(
        settings=settings, start=start, end=end, limit=limit, offset=offset
    )


@router.post("/backfill", dependencies=[Depends(require_trading_off)])
def backfill(settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Trigger the initial historical-data download (async, once at a time).

    Refused while trading is on: replacing the candles a strategy is running on
    would change its decisions under it.
    """
    return dataset_service.start_backfill(settings)


@router.get("/rebuild")
def rebuild_job() -> dict:
    """Status of the history-window re-download (started via POST /rebuild)."""
    return dataset_service.rebuild_status()


@router.post("/rebuild", dependencies=[Depends(require_trading_off)])
def start_rebuild(
    old_bar_size: Optional[str] = Query(default=None, description="Bar size the dataset previously used"),
    settings: Settings = Depends(get_effective_settings_dep),
) -> dict:
    """Download the newly configured window and merge it into the dataset.

    Nothing is deleted: dataset files are keyed by instrument AND bar size and are
    shared with the other strategies, so a re-download only adds bars. The old bar
    size is passed for the log only.

    Refused while trading is on — replacing the candles a strategy is running on
    would change its decisions under it.
    """
    return dataset_service.start_rebuild(settings, old_bar_size=old_bar_size)
