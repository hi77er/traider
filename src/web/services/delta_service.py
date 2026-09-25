"""Daily Delta service (sits between HTTP routes and data logic)."""

from __future__ import annotations

import logging
from typing import Optional

from src.config.effective import get_effective_settings
from src.config.settings import Settings
from src.data.delta import dataset_delta_status as _compute_status
from src.data.delta import sync_missing_days as _sync_delta

logger = logging.getLogger(__name__)


def _error_payload(settings: Settings, exc: Exception) -> dict:
    logger.exception("Delta operation failed")
    return {
        "exists": None,
        "symbol": settings.instrument,
        "interval": settings.historical_bar_size,
        "last_date": None,
        "rows": 0,
        "eligible_until": None,
        "missing": [],
        "synced": False,
        "last_available": None,
        "recent": [],
        "error": str(exc),
    }


def status(settings: Optional[Settings] = None) -> dict:
    """Current dataset delta state (checks the dataset for missing days)."""
    settings = settings or get_effective_settings()
    try:
        return _compute_status(settings)
    except Exception as exc:
        return _error_payload(settings, exc)


def sync(settings: Optional[Settings] = None) -> dict:
    """Fetch the missing days into the dataset and return the new state."""
    settings = settings or get_effective_settings()
    try:
        return _sync_delta(settings)
    except Exception as exc:
        return _error_payload(settings, exc)
