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

from src.config import history
from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.data.openbb_client import OpenBBClient

logger = logging.getLogger(__name__)


def resolve_history_window(settings: Settings) -> tuple:
    """(start_date, end_date) for a full historical fetch, YYYY-MM-DD strings.

    The window is the configured PERIOD (``HISTORICAL_LOOKBACK``, e.g. ``2y`` or
    ``30d``) counted back from the exchange's today, and the end is always now. It
    is the only window this bot fetches, and counting it back from NOW rather than
    forward from a fixed date is what lets a dataset grow with the strategy it
    belongs to. A day-based period is what intraday bar sizes use: the provider
    only serves a short trailing window of minute bars, so the window is measured
    in days there and in years for the coarser bars.

    An unusable period RAISES rather than quietly becoming a different window. A
    silent fallback is how a strategy set to "30 days" was once fetched from
    2022-01-01 with nothing in the panel to show the disagreement. Every caller is
    interactive (the backfill, the window re-fetch, the CLI) and reports the
    message where the operator can see it.

    The window is CLAMPED to what the configured provider can actually serve (see
    ``config.history.PROVIDER_MAX_DAYS``). A period longer than that is not a
    slower download, it is a fetch that silently returns what exists:
    ``30d`` of 1-minute bars came back as five trading days while the panel still
    said "30 days". The panel no longer offers such a pair, so this only bites a
    stored config, a hand-edited ``.env`` or a provider whose limits are unknown
    here — and it says so in the log rather than quietly shortening the window.
    """
    parts = history.period_parts(settings.historical_lookback)
    if not parts:
        raise ValueError(
            "HISTORICAL_LOOKBACK must be a period like '60d' or '2y' "
            f"(got {settings.historical_lookback!r}) — set the strategy's History period"
        )
    n, unit = parts
    cap = history.provider_max_days(history.DATA_PROVIDER, settings.historical_bar_size)
    days = history.period_days(settings.historical_lookback)
    if cap is not None and days is not None and days > cap:
        logger.warning(
            "%s serves at most %d days of %s bars — clamping the %s window to %dd",
            history.DATA_PROVIDER,
            cap,
            settings.historical_bar_size,
            settings.historical_lookback,
            cap,
        )
        n, unit = cap, history.UNIT_DAYS
    today = pd.Timestamp.now(tz=settings.market_timezone).normalize()
    offset = pd.DateOffset(days=n) if unit == history.UNIT_DAYS else pd.DateOffset(years=n)
    start = (today - offset).strftime("%Y-%m-%d")
    return start, None


def fetch_candles(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    bar_size: Optional[str] = None,
    persist: bool = True,
) -> pd.DataFrame:
    """Fetch OHLCV candles for the configured instrument & historical window.

    Overrides: pass ``symbol``/``start_date``/``end_date``/``bar_size`` to
    fetch a different window without touching config. With no ``start_date`` the
    configured period decides it (``resolve_history_window``), so a caller that
    wants "the strategy's history" does not have to restate the rule — and an
    empty ``end_date`` means "until now". When ``persist`` is True (default) the
    result is merged into the canonical Parquet dataset. Returns a DataFrame
    indexed by datetime with canonical open/high/low/close/volume columns.
    """
    client = client or OpenBBClient(settings)
    symbol = symbol or settings.instrument
    if start_date is None:
        start_date = resolve_history_window(settings)[0]
    # A blank date means "not specified"; the provider wants None, not "".
    end_date = end_date or None
    bar_size = bar_size or settings.historical_bar_size

    logger.info(
        "Fetching historical candles: %s %s %s -> %s via %s",
        symbol,
        bar_size,
        start_date or "?",
        end_date or "now",
        history.DATA_PROVIDER,
    )
    df = client.fetch_historical(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval=bar_size,
    )
    if persist and not df.empty:
        save_dataset(settings, df, symbol, bar_size)
    return df
