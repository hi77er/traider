"""OpenBB Platform SDK wrapper — the single entry point for market data.

The rest of the bot never imports OpenBB directly; it goes through this
client so backtest and live use the IDENTICAL normalized OHLCV schema.
Provides provider failover and an optional local cache (see config keys
``OPENBB_PROVIDER``, ``OPENBB_BACKUP_PROVIDERS``, ``DATA_CACHE_ENABLED``,
``CACHE_DIR``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.config import history
from src.config.settings import Settings

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


class OpenBBError(RuntimeError):
    """Raised when the OpenBB Platform is unavailable or every provider fails."""


class OpenBBClient:
    """Thin wrapper around ``openbb.obb`` with normalization, caching, failover."""

    def __init__(self, settings: Settings, cache_dir: Optional[Path] = None) -> None:
        self.settings = settings
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(settings.cache_dir)
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
        provider: Optional[str] = None,
        use_cache: Optional[bool] = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles normalized to the canonical schema.

        ``start_date``/``end_date`` are ``YYYY-MM-DD`` strings (end optional).
        Returns a DataFrame indexed by datetime with columns
        open/high/low/close/volume.
        """
        cache = use_cache if use_cache is not None else self.settings.data_cache_enabled
        if cache:
            cached = self._load_cache(symbol, start_date, end_date, interval, provider)
            if cached is not None:
                logger.info("Using cached candles for %s (%s)", symbol, interval)
                return cached

        fetch_interval, resample_interval = self._resolve_interval(interval)
        data = self._fetch_with_failover(symbol, start_date, end_date, fetch_interval, provider)
        if resample_interval is not None and not data.empty:
            logger.info("Resampling %s -> %s for %s", fetch_interval, resample_interval, symbol)
            data = self._resample(data, resample_interval)

        if cache and not data.empty:
            self._save_cache(data, symbol, start_date, end_date, interval, provider)
        return data

    def fetch_quote(self, symbol: str, provider: Optional[str] = None) -> pd.DataFrame:
        """Fetch a live quote (raw provider output, one row)."""
        provider = provider or self.settings.openbb_provider
        try:
            result = self.obb.equity.price.quote(symbol, provider=provider)
        except Exception as exc:  # no failover for quotes; surface the error
            raise OpenBBError(f"OpenBB quote failed for {symbol} via {provider}: {exc}") from exc
        return result.to_df()

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
    def _fetch_with_failover(
        self, symbol: str, start: str, end: Optional[str], interval: str, provider: Optional[str]
    ) -> pd.DataFrame:
        last_error: Optional[Exception] = None
        errors: List[str] = []
        for p in self._provider_chain(provider):
            try:
                logger.info("Fetching %s %s from %s via %s", symbol, interval, start or "?", p)
                result = self.obb.equity.price.historical(
                    symbol,
                    start_date=start,
                    end_date=end,
                    interval=interval,
                    provider=p,
                )
                frame = result.to_df()
                if frame is None or frame.empty:
                    # No bars is a provider-level failure, not a request-level
                    # one: record it and let the NEXT provider answer. Raising
                    # our own OpenBBError here would be re-raised by the clause
                    # below and abort the chain before the backups are tried.
                    errors.append(f"{p}: empty response")
                    logger.warning("Provider %s returned no bars for %s", p, symbol)
                    continue
                return self._drop_priceless(
                    self._complete_trailing_bar(
                        self._canonical(frame), symbol, interval, p
                    )
                )
            except OpenBBError:
                raise
            except Exception as exc:
                last_error = exc
                errors.append(f"{p}: {exc}")
                logger.warning("Provider %s failed for %s: %s", p, symbol, exc)
        # List EVERY provider's reason, not just the last fallback — the last
        # one is usually a keyless backup (e.g. fmp), which hides the real
        # cause (e.g. yfinance rate-limiting) behind a misleading message.
        raise OpenBBError(
            f"All providers failed for {symbol} — " + "; ".join(errors)
        ) from last_error

    def _provider_chain(self, provider: Optional[str]) -> List[str]:
        primary = provider or self.settings.openbb_provider
        chain = [primary]
        for backup in self.settings.backup_providers:
            if backup != primary and backup not in chain:
                chain.append(backup)
        return chain

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
        self, bars: pd.DataFrame, symbol: str, interval: str, provider: str
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
                self.obb.equity.price.historical(
                    symbol,
                    start_date=day.date().isoformat(),
                    end_date=day.date().isoformat(),
                    interval=interval,
                    provider=provider,
                ).to_df()
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
    def _cache_path(
        self, symbol: str, start: str, end: Optional[str], interval: str, provider: Optional[str]
    ) -> Path:
        safe = symbol.replace("/", "_").replace("=", "_")
        end_part = end or "now"
        prov = provider or self.settings.openbb_provider
        return self.cache_dir / f"{safe}_{start}_{end_part}_{interval}_{prov}.csv"

    def _load_cache(
        self, symbol: str, start: str, end: Optional[str], interval: str, provider: Optional[str]
    ) -> Optional[pd.DataFrame]:
        path = self._cache_path(symbol, start, end, interval, provider)
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
        self, df: pd.DataFrame, symbol: str, start: str, end: Optional[str], interval: str, provider: Optional[str]
    ) -> None:
        path = self._cache_path(symbol, start, end, interval, provider)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)
            logger.info("Saved %s rows to cache %s", len(df), path.name)
        except Exception as exc:
            logger.warning("Cache write failed: %s", exc)
