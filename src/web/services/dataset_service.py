"""Dataset business logic for the Web Portal.

Sits between the HTTP routes and the data layer. Knows nothing about HTTP;
only about the dataset (status, rows, backfill). The backfill runs in a
background thread guarded by an in-process lock so two downloads can never
run at once.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.config import history
from src.config.effective import get_effective_settings
from src.config.settings import Settings
from src.data.dataset import (
    bar_label,
    chart_time,
    dataset_path,
    delete_dataset,
    load_dataset,
    save_dataset,
)
from src.data.historical import fetch_candles, resolve_history_window

logger = logging.getLogger(__name__)

# In-process backfill job state (single-worker bot).
_JOB_LOCK = threading.Lock()
_JOB: Dict[str, object] = {"running": False, "last_error": None, "last_run": None, "rows": 0}


def dataset_status(settings: Optional[Settings] = None) -> dict:
    """Report whether the canonical dataset exists, plus summary stats."""
    settings = settings or get_effective_settings()
    symbol = settings.instrument
    interval = settings.historical_bar_size
    path = dataset_path(settings, symbol, interval)
    exists = path.exists()

    result = {
        "exists": exists,
        "symbol": symbol,
        "interval": interval,
        "period": settings.historical_lookback,
        "period_label": history.format_period(settings.historical_lookback),
        "rows": 0,
        "start": None,
        "end": None,
        "last_price": None,
        "job": _job_state(),
    }
    if exists:
        df = load_dataset(settings, symbol, interval)
        result["rows"] = int(len(df))
        if not df.empty:
            result["start"] = str(df.index.min().date())
            result["end"] = str(df.index.max().date())
            result["last_price"] = float(df["close"].iloc[-1])
    return result


def get_rows(
    settings: Optional[Settings] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Return dataset rows as JSON-ready records with pagination metadata.

    ``limit=0`` returns all rows ascending (used by the chart, which needs
    chronological order). Otherwise the table is paginated NEWEST-first: the
    most recent bar is row #1 (offset 0) and later pages walk back in time.
    Each row carries ``date`` (date-only), ``time`` (lightweight-charts key:
    date string for daily bars, unix seconds for intraday) and ``datetime``
    (human label, date+time for intraday).
    """
    settings = settings or get_effective_settings()
    df = load_dataset(
        settings, settings.instrument, settings.historical_bar_size, start=start, end=end
    )
    total = int(len(df))
    interval = settings.historical_bar_size
    if limit and limit > 0:
        # Reverse so page 1 shows the newest bars; offset counts from the newest.
        df = df.iloc[::-1].iloc[offset : offset + limit]
    rows: List[dict] = [
        {
            "date": str(idx.date()),
            "time": chart_time(idx, interval, settings.market_timezone),
            "datetime": bar_label(idx, interval),
            "open": round(float(r.open), 4),
            "high": round(float(r.high), 4),
            "low": round(float(r.low), 4),
            "close": round(float(r.close), 4),
            "volume": int(r.volume),
        }
        for idx, r in df.iterrows()
    ]
    return {"total": total, "offset": offset, "limit": limit, "rows": rows}


def start_backfill(settings: Optional[Settings] = None) -> dict:
    """Start the initial data download in the background (once at a time)."""
    settings = settings or get_effective_settings()
    with _JOB_LOCK:
        if _JOB["running"]:
            # NOTE: build the snapshot inline — calling _job_state() here would
            # re-acquire the (non-reentrant) lock and deadlock.
            return {"started": False, "reason": "backfill already running", "job": _snapshot()}
        _JOB["running"] = True
        _JOB["last_error"] = None
        _JOB["rows"] = 0

    logger.info("Starting dataset backfill in background thread")
    threading.Thread(target=_run_backfill, args=(settings,), daemon=True).start()
    return {"started": True, "job": _job_state()}


def _run_backfill(settings: Settings) -> None:
    """Fetch + persist the configured historical window (same path as backfill CLI)."""
    try:
        df = fetch_candles(
            settings,
            symbol=settings.instrument,
            start_date=settings.historical_start_date,
            end_date=settings.historical_end_date,
            bar_size=settings.historical_bar_size,
        )
        with _JOB_LOCK:
            _JOB["rows"] = int(len(df)) if df is not None else 0
            _JOB["last_error"] = None
            _JOB["last_run"] = datetime.now(timezone.utc).isoformat()
            logger.info("Backfill finished: %s rows", _JOB["rows"])
    except Exception as exc:  # network / provider / rate-limit
        logger.exception("Backfill failed")
        with _JOB_LOCK:
            _JOB["last_error"] = str(exc)
    finally:
        with _JOB_LOCK:
            _JOB["running"] = False


def _snapshot() -> dict:
    """Read the job state — caller must already hold ``_JOB_LOCK``."""
    return {
        "running": bool(_JOB["running"]),
        "last_error": _JOB["last_error"],
        "last_run": _JOB["last_run"],
        "rows": int(_JOB["rows"]),
    }


def _job_state() -> dict:
    with _JOB_LOCK:
        return _snapshot()


# Separate background job for the "history window / bar size changed" flow:
# the old dataset file is deleted and the new window is downloaded, then the
# chart is refreshed. Rules are untouched (they live in the strategy file).
_REBUILD_LOCK = threading.Lock()
_REBUILD: Dict[str, object] = {
    "running": False, "last_error": None, "last_run": None, "rows": 0, "label": "",
}


def rebuild_status() -> dict:
    """Status of the in-progress dataset re-download (empty dict when idle)."""
    with _REBUILD_LOCK:
        return {
            "running": bool(_REBUILD["running"]),
            "last_error": _REBUILD["last_error"],
            "last_run": _REBUILD["last_run"],
            "rows": int(_REBUILD["rows"]),
            "label": str(_REBUILD["label"]),
        }


def start_rebuild(settings: Optional[Settings] = None, old_bar_size: Optional[str] = None) -> dict:
    """Delete the old dataset and download the strategy's new history window.

    ``old_bar_size`` is the bar size the dataset previously used (when it
    changed). Runs in a background thread; poll ``rebuild_status``.
    """
    settings = settings or get_effective_settings()
    symbol = settings.instrument
    bar = settings.historical_bar_size
    period = history.format_period(settings.historical_lookback, unknown="?")
    with _REBUILD_LOCK:
        if _REBUILD["running"]:
            return {"started": False, "reason": "a re-download is already running", "job": rebuild_status()}
        _REBUILD["running"] = True
        _REBUILD["last_error"] = None
        _REBUILD["rows"] = 0
        _REBUILD["label"] = f"{symbol} · {period} · {bar}"
    start, end = resolve_history_window(settings)
    logger.info(
        "Starting dataset re-download: %s %s (%s -> %s), old bar size=%s",
        symbol, bar, start or "?", end or "now", old_bar_size,
    )
    threading.Thread(
        target=_run_rebuild,
        args=(settings, symbol, bar, start, end, old_bar_size),
        daemon=True,
    ).start()
    return {"started": True, "job": rebuild_status()}


def _run_rebuild(
    settings: Settings,
    symbol: str,
    bar: str,
    start: Optional[str],
    end: Optional[str],
    old_bar_size: Optional[str],
) -> None:
    """Fetch the new window into memory, then swap it in for the old dataset."""
    try:
        df = fetch_candles(
            settings,
            symbol=symbol,
            start_date=start,
            end_date=end,
            bar_size=bar,
            persist=False,  # hold in memory until the whole window is fetched
        )
        # Replace the target bar's file; also drop the previous bar-size file
        # (e.g. AAPL_1d) when the bar size changed.
        if old_bar_size and old_bar_size != bar:
            delete_dataset(settings, symbol, old_bar_size)
        delete_dataset(settings, symbol, bar)
        if df is not None and not df.empty:
            save_dataset(settings, df, symbol, bar)
        with _REBUILD_LOCK:
            _REBUILD["rows"] = int(len(df)) if df is not None else 0
            _REBUILD["last_error"] = None
            _REBUILD["last_run"] = datetime.now(timezone.utc).isoformat()
            logger.info("Dataset re-download finished: %s rows", _REBUILD["rows"])
    except Exception as exc:  # network / provider / rate-limit
        logger.exception("Dataset re-download failed")
        with _REBUILD_LOCK:
            _REBUILD["last_error"] = str(exc)
    finally:
        with _REBUILD_LOCK:
            _REBUILD["running"] = False
