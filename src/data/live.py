"""Poll the current price at each decision interval.

Stocks only trade ~9:30-16:00 ET on weekdays. The live fetcher skips polls
outside that window (config: ``TRADING_START_HOUR``, ``TRADING_END_HOUR``,
``MARKET_TIMEZONE``) and returns the last completed candle from OpenBB.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import session
from src.config.settings import Settings
from src.data.openbb_client import OpenBBClient

logger = logging.getLogger(__name__)


def is_market_open(settings: Settings, now: Optional[datetime] = None) -> bool:
    """Return True if ``now`` (default: current time) falls inside the
    configured trading window on a weekday.

    Thin wrapper over ``src.config.session``, which owns the rule: the poll and the
    per-bar decision filter must agree, or the bot could fetch a bar it then refuses
    to decide on (or worse, decide on a bar it would not fetch).
    """
    return session.is_open_at(settings, now)


def get_latest_candle(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    symbol: Optional[str] = None,
    bar_size: Optional[str] = None,
) -> pd.DataFrame:
    """Poll the most recent completed OHLCV candle.

    Returns a one-row DataFrame (canonical schema) when a candle is
    available, or an empty DataFrame when the market is closed and no
    fresh data can be fetched.
    """
    client = client or OpenBBClient(settings)
    symbol = symbol or settings.instrument
    bar_size = bar_size or settings.historical_bar_size

    if not is_market_open(settings):
        logger.info("Market closed — skipping live poll for %s", symbol)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    now = datetime.now(ZoneInfo(settings.market_timezone))
    lookback_start = (now - pd.Timedelta(days=settings.live_lookback_days)).date().isoformat()
    logger.info("Polling latest candle for %s (%s)", symbol, bar_size)
    df = client.fetch_historical(
        symbol=symbol,
        start_date=lookback_start,
        end_date=None,
        interval=bar_size,
    )
    return df.tail(1) if not df.empty else df
