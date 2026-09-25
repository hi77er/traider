"""OpenBB Platform SDK wrapper — the single entry point for market data.

The rest of the bot never imports OpenBB directly; it goes through this
client so backtest and live use the IDENTICAL normalized OHLCV schema.
ONE provider answers — yfinance, named once in ``config.history.DATA_PROVIDER`` —
plus an optional local cache (``DATA_CACHE_ENABLED``, ``CACHE_DIR``).

Failures come in two shapes and callers must tell them apart:

* ``NoDataError`` — the provider answered, and the answer was "no bars in that
  window". A delta probe asks about windows nothing traded in, so this is a
  normal answer, not a fault.
* ``OpenBBError`` — the request itself failed, carrying the provider's OWN
  message. There is no fallback chain left to bury it in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.config import history
from src.config.settings import Settings
from src.data.deadline import bounded

logger = logging.getLogger(__name__)

# Canonical OHLCV schema consumed by features, backtest and live.
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# Columns a bar MUST carry. Volume is excluded on purpose: a legitimate bar can
# be volume-less, but a bar without a price is not a bar (see _normalize).
PRICE_COLUMNS = ["open", "high", "low", "close"]

# Extra columns preserved when a provider includes them (e.g. vwap).
OPTIONAL_COLUMNS = ["vwap", "transactions"]

# Intervals providers expose natively (yfinance list); anything else is
# resampled client-side from a finer base interval.
_SUPPORTED_INTERVALS = {
    "1m", "2m", "5m", "15m", "30m", "60m", "90m",
    "1h", "1d", "5d", "1W", "1M", "1Q",
}

# Target interval -> finer base interval to fetch, then resample.
# The map itself lives in ``config.history`` (``FETCH_INTERVAL``): the provider's
# history limit binds on the interval actually FETCHED, so the settings schema has
# to know what each bar size is made of, and two copies would drift apart.

# Target interval -> pandas resample rule.
_PANDAS_RULE = {
    "2h": "2h", "4h": "4h", "8h": "8h", "12h": "12h",
    "3d": "3D", "2W": "2W", "2M": "2M",
}

# How long ONE provider call may take before the caller gives up waiting on it. Sized on the
# provider's own behaviour: Yahoo times its queries out well inside a minute, so an answer that
# has not arrived in 90s is a dead socket rather than a slow one — and the loop is at most one
# bar late instead of losing the session.
PROVIDER_DEADLINE_SECONDS = 90.0


class OpenBBError(RuntimeError):
    """Raised when the data provider is unavailable or refuses the request."""


class NoDataError(OpenBBError):
    """The provider answered, and the answer was: no bars in that window.

    OpenBB raises ``EmptyDataError`` for a symbol/window it has nothing for, which
    looks exactly like a genuine outage at the call site. They are not the same
    thing: a delta probe deliberately asks about windows that never traded (a thin
    symbol, a quiet stretch, a window that has not happened yet), and treating that
    as a failure is what made a perfectly healthy dataset report "Check failed".

    A subclass of ``OpenBBError``, so a caller that only knows the old failure
    still catches it.
    """


def _is_no_data(exc: BaseException) -> bool:
    """True when OpenBB reported "no data" rather than a failure."""
    try:
        from openbb_core.provider.utils.errors import EmptyDataError
    except ImportError:  # pragma: no cover - only without the platform installed
        return False
    return isinstance(exc, EmptyDataError)


def _no_bars_message(symbol: str, interval: str, start: str, end: Optional[str]) -> str:
    """Say WHICH window came back empty — a bare "no data" is not actionable."""
    return f"No {interval} bars for {symbol} between {start or '?'} and {end or 'now'}"


class OpenBBClient:
    """Thin wrapper around ``openbb.obb`` with normalization, caching, failover."""

    def __init__(
        self,
        settings: Settings,
        cache_dir: Optional[Path] = None,
        deadline_seconds: float = PROVIDER_DEADLINE_SECONDS,
    ) -> None:
        self.settings = settings
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(settings.cache_dir)
        self.deadline_seconds = float(deadline_seconds)
        self._obb = None

    # -- OpenBB access (lazy import so the module is testable without it) ---
    @property
    def obb(self):
        if self._obb is None:
            try:
                from openbb import obb
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise OpenBBError(
                    "OpenBB Platform is not installed. Run `pip install openbb` "
                    "in your virtual environment."
                ) from exc
            # Use curl_cffi (browser TLS impersonation) for the yfinance provider
            # so Yahoo is far less likely to rate-limit the bot.
            from src.data.openbb_session import apply_openbb_session_patch

            apply_openbb_session_patch()
            self._obb = obb
        return self._obb

    # -- public API ----------------------------------------------------------
    def fetch_historical(
        self,
        symbol: str,
        start_date: str,
        end_date: Optional[str] = None,
        interval: str = "4h",
        use_cache: Optional[bool] = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles normalized to the canonical schema.

        ``start_date``/``end_date`` are ``YYYY-MM-DD`` strings (end optional).
        Returns a DataFrame indexed by datetime with columns
        open/high/low/close/volume.

        Raises ``NoDataError`` when the provider has no bars for that window (a
        normal answer for a probe) and ``OpenBBError`` — carrying the provider's
        own message — when the request failed.
        """
        cache = use_cache if use_cache is not None else self.settings.data_cache_enabled
        if cache:
            cached = self._load_cache(symbol, start_date, end_date, interval)
            if cached is not None:
                logger.info("Using cached candles for %s (%s)", symbol, interval)
                return cached

        fetch_interval, resample_interval = self._resolve_interval(interval)
        data = self._fetch_from_provider(symbol, start_date, end_date, fetch_interval)
        if resample_interval is not None and not data.empty:
            logger.info("Resampling %s -> %s for %s", fetch_interval, resample_interval, symbol)
            data = self._resample(data, resample_interval)

        if cache and not data.empty:
            self._save_cache(data, symbol, start_date, end_date, interval)
        return data

    def fetch_quote(self, symbol: str) -> pd.DataFrame:
        """Fetch a live quote (raw provider output, one row)."""
        try:
            quote = bounded(
                lambda: self.obb.equity.price.quote(
                    symbol, provider=history.DATA_PROVIDER
                ).to_df(),
                self.deadline_seconds,
                what=f"the {history.DATA_PROVIDER} quote for {symbol}",
                error=OpenBBError,
            )
        except Exception as exc:
            # No failover for quotes, and no wrapper either: the provider's own
            # message is what the operator needs to see.
            raise OpenBBError(f"{type(exc).__name__}: {str(exc).strip()}") from exc
        return quote

    # -- interval handling ---------------------------------------------------
    @staticmethod
    def _resolve_interval(interval: str):
        """Return (fetch_interval, resample_interval_or_None).

        If the requested interval is natively supported it is fetched as-is;
        otherwise a finer base interval is fetched and resampled up.
        """
        if interval in _SUPPORTED_INTERVALS:
            return interval, None
        base = history.fetch_interval(interval)
        if base != interval:
            return base, interval
        return interval, None  # unknown -> let the provider decide / raise

    @staticmethod
    def _resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """Aggregate OHLCV to a coarser bar size (e.g. 1h -> 4h)."""
        rule = _PANDAS_RULE.get(interval)
        if rule is None:
            return df
        out = (
            df.resample(rule, origin="start_day")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna(subset=["close"])
        )
        out.index.name = "date"
        return out

    # -- internals -----------------------------------------------------------
    def _fetch_from_provider(
        self, symbol: str, start: str, end: Optional[str], interval: str
    ) -> pd.DataFrame:
        """One request, to the one provider we use. There is no fallback chain.

        The chain was removed on purpose (2026-09-18). Its backups needed API keys
        nobody had, so every genuine yfinance message arrived wrapped in their
        "Missing credential" errors and the real cause — usually rate limiting —
        was the hardest part of the message to find. With one provider there is
        nothing to fail over TO, so the provider's own error is what gets raised.
        """
        logger.info("Fetching %s %s from %s via %s", symbol, interval, start or "?", history.DATA_PROVIDER)
        try:
            frame = bounded(
                lambda: self.obb.equity.price.historical(
                    symbol,
                    start_date=start,
                    end_date=end,
                    interval=interval,
                    provider=history.DATA_PROVIDER,
                ).to_df(),
                self.deadline_seconds,
                what=f"the {history.DATA_PROVIDER} fetch for {symbol} {interval}",
                error=OpenBBError,
            )
        except Exception as exc:
            if _is_no_data(exc):
                # The provider answered: nothing there. Not a failure.
                raise NoDataError(_no_bars_message(symbol, interval, start, end)) from exc
            # ...and when it DID fail, say what it said — the class name carries the
            # actionable part (e.g. ``YFRateLimitError``) and the message the rest.
            raise OpenBBError(f"{type(exc).__name__}: {str(exc).strip()}") from exc
        if frame is None or frame.empty:
            # A provider handing back an empty frame is the same answer.
            raise NoDataError(_no_bars_message(symbol, interval, start, end))
        return self._drop_priceless(
            self._complete_trailing_bar(self._canonical(frame), symbol, interval)
        )
    @staticmethod
    def _canonical(frame: pd.DataFrame) -> pd.DataFrame:
        """Lowercase OHLCV columns with a datetime index (no row filtering)."""
        df = frame.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]

        # Promote a date/timestamp/time column to the index if present.
        for name in ("date", "timestamp", "time"):
            if name in df.columns:
                df[name] = pd.to_datetime(df[name], utc=True)
                df = df.set_index(name)
                break

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)

        missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
        if missing:
            raise OpenBBError(f"Provider response missing required columns: {missing}")

        keep = [c for c in OHLCV_COLUMNS + OPTIONAL_COLUMNS if c in df.columns]
        df = df[keep].astype({c: "float64" for c in keep})
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.index.name = "date"
        return df

    @staticmethod
    def _drop_priceless(df: pd.DataFrame) -> pd.DataFrame:
        """Remove placeholder bars that carry no price at all.

        Providers emit these for a session that has not settled in their cache:
        the row has an open and a high but a NaN close. Such a bar is not
        tradable and must never reach the dataset — a single null close makes
        lightweight-charts reject the payload and abandon the WHOLE series,
        which renders a blank chart.
        """
        blank = df[PRICE_COLUMNS].isna().any(axis=1)
        if blank.any():
            logger.warning(
                "Dropped %d bar(s) with a missing price: %s",
                int(blank.sum()),
                ", ".join(str(ts) for ts in df.index[blank][:5]),
            )
            return df[~blank]
        return df

    @classmethod
    def _normalize(cls, frame: pd.DataFrame) -> pd.DataFrame:
        """Canonical lowercase OHLCV with price-less placeholder bars removed."""
        return cls._drop_priceless(cls._canonical(frame))

    def _complete_trailing_bar(
        self, bars: pd.DataFrame, symbol: str, interval: str
    ) -> pd.DataFrame:
        """Re-ask for a trailing bar the provider returned without a price.

        yfinance returns the FINAL row of a multi-day range with a NaN close —
        the session is not settled in its cache — even though a request that
        STARTS on that very date returns the settled close. Dropping the row
        alone would leave the dataset permanently a day short of its newest
        bar (the one the chart, the delta panel and the strategy all need),
        because every daily sync re-fetches the same wide range. So ask for that
        single day again and take only its prices; the rest of the row (volume)
        is kept as delivered.
        """
        if bars.empty:
            return bars
        blank = bars[PRICE_COLUMNS].isna().any(axis=1)
        if not blank.any() or not bool(blank.iloc[-1]):
            return bars  # nothing trailing to recover
        day = bars.index[-1]
        logger.info("Retrying %s for %s (trailing bar has no price)", day.date(), symbol)
        try:
            retry = self._canonical(
                bounded(
                    lambda: self.obb.equity.price.historical(
                        symbol,
                        start_date=day.date().isoformat(),
                        end_date=day.date().isoformat(),
                        interval=interval,
                        provider=history.DATA_PROVIDER,
                    ).to_df(),
                    self.deadline_seconds,
                    what=f"the {history.DATA_PROVIDER} retry for {symbol} {day.date()}",
                    error=OpenBBError,
                )
            )
        except Exception as exc:  # noqa: BLE001 - recovery is best-effort
            logger.warning("Could not recover the trailing bar for %s: %s", symbol, exc)
            return bars
        good = retry.dropna(subset=PRICE_COLUMNS)
        if good.empty:
            return bars  # no settled bar for that date -> leave it to be dropped
        bars.loc[good.index, PRICE_COLUMNS] = good[PRICE_COLUMNS]
        return bars

    # -- cache ---------------------------------------------------------------
    def _cache_path(self, symbol: str, start: str, end: Optional[str], interval: str) -> Path:
        safe = symbol.replace("/", "_").replace("=", "_")
        end_part = end or "now"
        return self.cache_dir / f"{safe}_{start}_{end_part}_{interval}_{history.DATA_PROVIDER}.csv"

    def _load_cache(
        self, symbol: str, start: str, end: Optional[str], interval: str
    ) -> Optional[pd.DataFrame]:
        path = self._cache_path(symbol, start, end, interval)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            logger.info("Loaded %s rows from cache %s", len(df), path.name)
            return df
        except Exception as exc:  # corrupt cache -> refetch
            logger.warning("Cache read failed for %s: %s", path, exc)
            return None

    def _save_cache(
        self, df: pd.DataFrame, symbol: str, start: str, end: Optional[str], interval: str
    ) -> None:
        path = self._cache_path(symbol, start, end, interval)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)
            logger.info("Saved %s rows to cache %s", len(df), path.name)
        except Exception as exc:
            logger.warning("Cache write failed: %s", exc)
