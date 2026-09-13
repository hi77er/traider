"""Fetch historical OHLCV candles for backtesting and model training.

Every parameter defaults to a value from configuration (``.env``), so the
same code path is used by the backtester and the live scheduler.

Fetched data is both cached (transient CSV in ``CACHE_DIR``) and persisted
to the canonical Parquet dataset in ``HISTORICAL_DATA_DIR`` that backtesting
and training read.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.data.openbb_client import OpenBBClient

logger = logging.getLogger(__name__)


def resolve_history_window(settings: Settings) -> tuple:
    """(start_date, end_date) for a full historical fetch, YYYY-MM-DD strings.

    Prefers the years-based window (``HISTORICAL_LOOKBACK_YEARS``, up to now)
    and falls back to the legacy free-text start/end dates when years are unset.
    """
    end = settings.historical_end_date or None
    years = settings.historical_lookback_years
    if years:
        today = pd.Timestamp.now(tz=settings.market_timezone).normalize()
        start = (today - pd.DateOffset(years=int(years))).strftime("%Y-%m-%d")
        return start, end
    return (settings.historical_start_date or None), end


def fetch_candles(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    bar_size: Optional[str] = None,
    provider: Optional[str] = None,
    persist: bool = True,
) -> pd.DataFrame:
    """Fetch OHLCV candles for the configured instrument & historical window.

    Overrides: pass ``symbol``/``start_date``/``end_date``/``bar_size`` to
    fetch a different window without touching config. When ``persist`` is
    True (default) the result is merged into the canonical Parquet dataset.
    Returns a DataFrame indexed by datetime with canonical
    open/high/low/close/volume columns.
    """
    client = client or OpenBBClient(settings)
    symbol = symbol or settings.instrument
    # A blank date in .env means "not specified" (end = now, start = default).
    start_date = (start_date or settings.historical_start_date) or None
    end_date = (end_date or settings.historical_end_date) or None
    bar_size = bar_size or settings.historical_bar_size

    logger.info(
        "Fetching historical candles: %s %s %s -> %s via %s",
        symbol,
        bar_size,
        start_date or "?",
        end_date or "now",
        provider or settings.openbb_provider,
    )
    df = client.fetch_historical(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval=bar_size,
        provider=provider,
    )
    if persist and not df.empty:
        save_dataset(settings, df, symbol, bar_size)
    return df
