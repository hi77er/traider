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
    "ema",
    "macd",
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


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential moving average of ``close`` with span ``period``.

    Recursive form ``E[t] = a * close[t] + (1 - a) * E[t-1]`` with
    ``a = 2 / (period + 1)`` (pandas ``ewm(span=..., adjust=False)``), seeded
    with the first close. ``adjust=False`` is the trading convention and, unlike
    the ``adjust=True`` default, only ever looks backwards - so the value at bar
    ``t`` is identical whether it is computed over full history or live, which
    is what keeps backtest and live features the same.

    ``min_periods=period`` blanks the warm-up rows exactly like ``sma``, so both
    moving averages become available at the same bar and an EMA/SMA pair is
    directly comparable.
    """
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Moving Average Convergence/Divergence: line, signal line and histogram.

    ``macd = EMA(fast) - EMA(slow)``, ``signal = EMA(signal)`` of that line and
    ``hist = macd - signal``. Both EMAs use ``adjust=False`` (see :func:`ema`),
    so every value depends only on past bars and is identical whether computed
    over the full history or live — the same guarantee SMA/EMA already have.

    Returns a DataFrame with ``macd`` / ``signal`` / ``hist`` columns.
    ``min_periods`` blanks the warm-up rows: the MACD line becomes valid on the
    ``slow``-th bar and the signal line (hence the histogram) on the
    ``slow + signal - 1``-th, so the histogram — the column rules usually
    reference — carries the longest warm-up of the family.
    """
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    line = fast_ema - slow_ema
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {"macd": line, "signal": signal_line, "hist": line - signal_line}
    )


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
