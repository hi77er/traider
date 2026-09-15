"""The live data feed: enough history to decide, from the last completed bar.

Stocks only trade ~9:30-16:00 ET on weekdays, so a poll outside the window is
skipped (``src.config.session`` owns that rule, and the per-bar decision filter uses
the same one).

What this module fetches is a WINDOW, not a quote: a strategy needs the bars its
indicators look back over, and one bar is not a decision — see
:func:`required_bars`. ``get_latest_candle`` still exists for the price displays that
only want a quote.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import session
from src.config.settings import Settings
from src.data.dataset import is_intraday
from src.data.openbb_client import OpenBBClient
from src.features import schema
from src.features.engineering import FeatureEngineer

logger = logging.getLogger(__name__)


def is_market_open(settings: Settings, now: Optional[datetime] = None) -> bool:
    """Return True if ``now`` (default: current time) falls inside the
    configured trading window on a weekday.

    Thin wrapper over ``src.config.session``, which owns the rule: the poll and the
    per-bar decision filter must agree, or the bot could fetch a bar it then refuses
    to decide on (or worse, decide on a bar it would not fetch).
    """
    return session.is_open_at(settings, now)


def required_bars(settings: Settings) -> int:
    """Closed bars a single decision needs, from the feature configuration.

    Derived, never guessed: an EMA(50) has no value until 50 bars exist, so a live run
    handed fewer bars evaluates NaN and emits HOLD — indistinguishable from a quiet
    ``FEATURES_MIN_LOOKBACK`` is the configured floor; the longest enabled indicator
    window can demand more. Two bars on top: the last bar must have a feature row, and so
    must the one before it, because a rule that compares against the previous bar (a
    cross) reads two feature rows.
    """
    engineer = FeatureEngineer(settings)
    return max(int(engineer.warmup), schema.longest_window(settings)) + 2


def days_for_bars(settings: Settings, bars: int) -> int:
    """Calendar days to request so ``bars`` closed bars come back.

    The provider is asked by DATE, so the bar size decides how many dates a bar count
    costs: 51 daily bars need ~102 calendar days (weekends), 51 hourly bars need ~8
    sessions. Generous by design — over-fetching a few bars is free, being one bar short
    is a silent HOLD.
    """
    interval = str(settings.historical_bar_size or "1d")
    if not is_intraday(interval):
        # A daily bar per trading day: ~5/7 of calendar days are trading days, and a
        # weekly/monthly bar needs that many weeks/months.
        per_bar = {"1d": 7 / 5, "1W": 7.0, "1M": 31.0}.get(interval, 7 / 5)
        return int(bars * per_bar) + 7
    minutes = _interval_minutes(interval) or 60
    session_minutes = _session_minutes(settings) or 390
    bars_per_session = max(1, int(session_minutes // minutes))
    sessions = -(-int(bars) // bars_per_session)  # ceil
    return sessions * 2 + 5


def _interval_minutes(interval: str) -> int:
    """Minutes in an intraday bar code (``'15m'`` -> 15, ``'4h'`` -> 240)."""
    text = str(interval).strip().lower()
    if text.endswith("m") and text[:-1].isdigit():
        return int(text[:-1])
    if text.endswith("h") and text[:-1].isdigit():
        return int(text[:-1]) * 60
    return 0


def _session_minutes(settings: Settings) -> int:
    """Minutes between the session's open and close."""
    start, end = session.window(settings)
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return minutes if minutes > 0 else 390


def get_history_window(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    symbol: Optional[str] = None,
    bar_size: Optional[str] = None,
    bars: Optional[int] = None,
) -> pd.DataFrame:
    """The trailing bars a decision needs — NOT a single quote.

    ``get_latest_candle`` returns one row, which is a price and not a decision: handed
    one bar the feature frame is entirely NaN, so every rule is skipped and the signal
    is HOLD forever. This returns the window the strategy actually needs, which is what
    ``src/strategy/live.LiveDriver`` is fed.
    """
    client = client or OpenBBClient(settings)
    symbol = symbol or settings.instrument
    bar_size = bar_size or settings.historical_bar_size
    wanted = int(bars or required_bars(settings))
    days = max(int(getattr(settings, "live_lookback_days", 0) or 0), days_for_bars(settings, wanted))

    now = datetime.now(ZoneInfo(settings.market_timezone))
    lookback_start = (now - pd.Timedelta(days=days)).date().isoformat()
    logger.info(
        "Polling %s %s bars (>=%d) since %s for %s", wanted, bar_size, days, lookback_start, symbol
    )
    df = client.fetch_historical(
        symbol=symbol,
        start_date=lookback_start,
        end_date=None,
        interval=bar_size,
    )
    if df is None or df.empty:
        return df
    return df.sort_index().tail(wanted)


def get_latest_candle(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    symbol: Optional[str] = None,
    bar_size: Optional[str] = None,
) -> pd.DataFrame:
    """Poll the most recent completed OHLCV candle — a QUOTE, not a decision.

    One row is enough to show a price and not enough to decide: use
    :func:`get_history_window` for anything that feeds the strategy. Returns a one-row
    DataFrame when a candle is available, or an empty one when the market is closed and
    no fresh data can be fetched.
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
