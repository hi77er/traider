"""Pure pandas indicator functions for the Features Engineering module.

Every function is deterministic and uses only data up to the current bar
(rolling / ewm / shift) so it is safe to use identically in backtest and
live. They are deliberately free of configuration — parameters are passed
explicitly so each function is trivially unit-testable.

Column contracts live in ``src/features/schema.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "rsi",
    "atr",
    "bollinger_pctb",
    "bollinger_bands",
    "momentum",
    "rolling_vol",
    "vwap",
    "volume_ratio",
]


def sma(close: pd.Series, period: int) -> pd.Series:
    """Simple moving average of ``close`` over ``period`` bars."""
    return close.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing (0-100).

    Flat series (no gains/losses) resolves to a neutral 50 instead of NaN.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder's smoothing == EMA with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 (pure up-move) -> RSI 100; both 0 (flat) -> neutral 50.
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return out


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range (Wilder smoothing). True range of the first bar is
    ``high - low`` because there is no previous close."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = (high - low).iloc[0]
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def bollinger_pctb(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> pd.Series:
    """Bollinger %B: where ``close`` sits inside the bands (0..1, may exceed).

    ``%B = (close - lower) / (upper - lower)``.
    """
    mid = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower).replace(0.0, np.nan)
    return (close - lower) / width


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> pd.DataFrame:
    """Bollinger band lines (mid / upper / lower) for charting.

    ``%B`` is the model feature; these are the actual price-scaled band lines
    used for display overlays on the candles chart.
    """
    mid = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std()
    return pd.DataFrame(
        {
            "mid": mid,
            "upper": mid + std_mult * std,
            "lower": mid - std_mult * std,
        }
    )


def momentum(close: pd.Series, period: int) -> pd.Series:
    """Momentum = (close[t] / close[t - period]) - 1."""
    return close.pct_change(periods=period)


def rolling_vol(close: pd.Series, period: int = 20) -> pd.Series:
    """Rolling standard deviation of daily returns over ``period`` bars."""
    returns = close.pct_change()
    return returns.rolling(window=period, min_periods=period).std()


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Rolling volume-weighted average price over ``period`` bars.

    Uses the typical price ``(high + low + close) / 3`` weighted by volume, so
    it is deterministic and uses only data up to the current bar (no future
    volume leaks in).
    """
    typical = (high + low + close) / 3.0
    num = (typical * volume).rolling(window=period, min_periods=period).sum()
    den = volume.rolling(window=period, min_periods=period).sum().replace(0.0, np.nan)
    return num / den


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """Relative volume: current volume vs its rolling mean over ``period`` bars.

    Around 1.0 on average; >1 means heavier-than-usual volume, <1 lighter. A
    scale-free proxy for the raw volume column (which is not stationary).
    """
    avg = volume.rolling(window=period, min_periods=period).mean().replace(0.0, np.nan)
    return volume / avg
