"""Chart indicators for the dashboard (Option B viewer).

Produces overlay-ready indicator series computed from the canonical Parquet
dataset, grouped by scale so the frontend can draw price-scaled lines on the
candles chart (SMA, Bollinger bands) and different-scale indicators (RSI,
momentum, ATR, volatility) on their own panes.

The heavy pandas work (the feature frame + bands) is **memoized in-process**:
it is recomputed only when the dataset or the feature configuration changes.
The memo key covers (symbol, interval, feature-config fingerprint, dataset
fingerprint), so toggling indicators on the chart never re-runs the math.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.config.effective import get_effective_settings
from src.config.settings import Settings
from src.data.dataset import chart_time, load_dataset
from src.features import indicators
from src.features.engineering import FeatureEngineer
from src.features.schema import macd_tag

logger = logging.getLogger(__name__)

# Colors used for line series (assigned per overlay in order).
_PALETTE = ["#f0b429", "#4c8dff", "#9b7bff", "#26a69a", "#ef5350", "#e879f9", "#ff7043"]

# MACD draws three series in one pane, so they must not all take the overlay's
# palette colour — the crossing of the two lines is the whole point of the
# indicator, and the histogram is the same story told as bars.
_MACD_LINE_COLOR = "#4c8dff"
_MACD_SIGNAL_COLOR = "#f0b429"
# Histogram bars are coloured per bar: above zero (momentum building) vs below.
_MACD_HIST_UP_COLOR = "rgba(38, 166, 154, 0.60)"
_MACD_HIST_DOWN_COLOR = "rgba(239, 83, 80, 0.60)"
# The pane renderer's default histogram format is `volume` (thousands/millions),
# which would round MACD's small decimals to whole numbers.
_MACD_HIST_PRICE_FORMAT = {"type": "price", "precision": 2, "minMove": 0.01}

_CACHE: Dict[str, pd.DataFrame] = {}
_CACHE_LOCK = threading.Lock()

# Feature-config fields that influence the computed series.
_CFG_FIELDS = (
    "historical_bar_size",
    "features_sma_periods",
    "features_ema_periods",
    "features_macd_fast_period",
    "features_macd_slow_period",
    "features_macd_signal_period",
    "features_rsi_period",
    "features_atr_period",
    "features_bollinger_period",
    "features_bollinger_std",
    "features_momentum_periods",
    "features_volatility_period",
    "features_min_lookback",
    "feature_sma_enabled",
    "feature_ema_enabled",
    "feature_macd_enabled",
    "feature_rsi_enabled",
    "feature_atr_enabled",
    "feature_bollinger_enabled",
    "feature_momentum_enabled",
    "feature_volatility_enabled",
    "feature_vwap_enabled",
    "feature_volume_enabled",
    "feature_volume_abs_enabled",
    "features_vwap_period",
    "features_volume_period",
)


def chart_indicators(settings: Optional[Settings] = None) -> dict:
    """Overlay-ready indicator series for the current dataset + config."""
    settings = settings or get_effective_settings()
    frame, last_date = _cached_indicator_frame(settings)
    overlays = _build_overlays(settings, frame)

    return {
        "symbol": settings.instrument,
        "interval": settings.historical_bar_size,
        "last_date": last_date,
        "overlays": overlays,
    }


def clear_cache() -> None:
    """Drop the memoized frame (mainly for tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# memoized computation
# ---------------------------------------------------------------------------
def _config_fingerprint(settings: Settings) -> str:
    raw = "|".join(str(getattr(settings, f, "")) for f in _CFG_FIELDS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cached_indicator_frame(settings: Settings) -> Tuple[pd.DataFrame, Optional[str]]:
    """Return (wide indicator frame, dataset last date), cached on (data, config)."""
    df = load_dataset(settings, settings.instrument, settings.historical_bar_size)
    if df is None or df.empty:
        return pd.DataFrame(), None

    last_date = str(df.index.max().date())
    data_fp = f"{len(df)}@{last_date}"
    key = f"{settings.instrument}|{settings.historical_bar_size}|{_config_fingerprint(settings)}|{data_fp}"

    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key], last_date

    frame = _build_indicator_frame(settings, df)
    with _CACHE_LOCK:
        # Guard against unbounded growth (tiny LRU by simple reset at cap).
        if len(_CACHE) > 8:
            _CACHE.clear()
        _CACHE[key] = frame
    return frame, last_date


def _build_indicator_frame(settings: Settings, df: pd.DataFrame) -> pd.DataFrame:
    """Feature columns + derived Bollinger band lines, same index as ``df``."""
    eng = FeatureEngineer(settings)
    frame = eng.compute_frame(df)

    if settings.feature_bollinger_enabled:
        p = settings.features_bollinger_period
        bands = indicators.bollinger_bands(df["close"], p, settings.features_bollinger_std)
        for name in ("mid", "upper", "lower"):
            frame[f"bb_{name}_{p}"] = bands[name]
    return frame


# ---------------------------------------------------------------------------
# overlays
# ---------------------------------------------------------------------------
def _points(series: pd.Series, settings: Settings) -> List[dict]:
    """JSON points ``[{time, value|null}]`` for lightweight-charts.

    ``time`` mirrors the candle axis: date string for daily bars, UTC unix
    seconds for intraday (so overlays line up with the candles).

    Leading warm-up rows (``NaN`` before the indicator has enough history) are
    DROPPED entirely: feeding lightweight-charts a run of null values at the
    start of a series makes it draw them as the pane's zero baseline, so the
    line appears to start at 0 and spike up to its first real value. The series
    therefore begins at its first real point; interior NaN gaps are kept as
    ``null`` so genuine gaps still render as breaks in the line.
    """
    interval = settings.historical_bar_size
    tz = settings.market_timezone
    points: List[dict] = []
    started = False
    for ts, v in series.items():
        if pd.isna(v):
            if not started:
                continue  # skip the leading warm-up gap
            points.append({"time": chart_time(ts, interval, tz), "value": None})
        else:
            started = True
            points.append({"time": chart_time(ts, interval, tz), "value": round(float(v), 6)})
    return points


def _histogram_points(
    series: pd.Series, settings: Settings, up_color: str, down_color: str
) -> List[dict]:
    """``_points`` with a per-bar ``color`` (``up_color`` at/above zero).

    lightweight-charts histogram series accept a colour on each point, which is
    what gives a MACD histogram its green/red split without a second series.
    Interior ``null`` gaps keep no colour (a null bar is not drawn anyway).
    """
    points = _points(series, settings)
    for point in points:
        value = point.get("value")
        if value is not None:
            point["color"] = up_color if value >= 0 else down_color
    return points


def _stats(series: pd.Series) -> dict:
    valid = series.dropna()
    if valid.empty:
        return {}
    return {"min": round(float(valid.min()), 6), "max": round(float(valid.max()), 6)}


def _build_overlays(settings: Settings, frame: pd.DataFrame) -> List[dict]:
    if frame is None or frame.empty:
        return []
    overlays: List[dict] = []

    # Price-scaled overlays (drawn on the candles chart).
    if settings.feature_sma_enabled:
        for p in settings.sma_periods:
            col = f"sma_{p}"
            overlays.append(
                {
                    "key": col,
                    "label": f"SMA {p}",
                    "scale": "price",
                    "kind": "line",
                    "color": _PALETTE[len(overlays) % len(_PALETTE)],
                    "lines": [{"name": col, "data": _points(frame[col], settings)}],
                }
            )

    if settings.feature_ema_enabled:
        for p in settings.ema_periods:
            col = f"ema_{p}"
            overlays.append(
                {
                    "key": col,
                    "label": f"EMA {p}",
                    "scale": "price",
                    "kind": "line",
                    "color": _PALETTE[len(overlays) % len(_PALETTE)],
                    "lines": [{"name": col, "data": _points(frame[col], settings)}],
                }
            )

    if settings.feature_bollinger_enabled:
        p = settings.features_bollinger_period
        bands = [
            {
                "name": name,
                "data": _points(frame[f"bb_{name}_{p}"], settings),
            }
            for name in ("upper", "mid", "lower")
        ]
        overlays.append(
            {
                "key": f"bbands_{p}",
                "label": f"Bollinger Bands ({p}, {settings.features_bollinger_std:g})",
                "scale": "price",
                "kind": "bands",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                "lines": bands,
            }
        )

    if settings.feature_vwap_enabled:
        p = settings.features_vwap_period
        col = f"vwap_{p}"
        overlays.append(
            {
                "key": col,
                "label": f"VWAP {p}",
                "scale": "price",
                "kind": "line",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                "lines": [{"name": col, "data": _points(frame[col], settings)}],
            }
        )

    # Different-scale indicators (each drawn on its own pane).
    if settings.feature_macd_enabled:
        tag = macd_tag(settings)
        line_col = f"macd_{tag}"
        signal_col = f"macd_signal_{tag}"
        hist_col = f"macd_hist_{tag}"
        overlays.append(
            {
                "key": line_col,
                "label": f"MACD {tag.replace('_', ', ')}",
                "scale": "osc",
                "kind": "line",
                "color": _MACD_LINE_COLOR,
                **_stats(frame[line_col]),
                # The histogram is listed FIRST: series are created in order, so
                # its bars then paint BEHIND the two lines instead of over them.
                "lines": [
                    {
                        "name": hist_col,
                        "kind": "histogram",
                        "color": _MACD_HIST_UP_COLOR,
                        "priceFormat": _MACD_HIST_PRICE_FORMAT,
                        "data": _histogram_points(
                            frame[hist_col],
                            settings,
                            _MACD_HIST_UP_COLOR,
                            _MACD_HIST_DOWN_COLOR,
                        ),
                    },
                    {"name": line_col, "color": _MACD_LINE_COLOR,
                     "data": _points(frame[line_col], settings)},
                    {"name": signal_col, "color": _MACD_SIGNAL_COLOR,
                     "data": _points(frame[signal_col], settings)},
                ],
            }
        )

    if settings.feature_rsi_enabled:
        p = settings.features_rsi_period
        col = f"rsi_{p}"
        overlays.append(
            {
                "key": col,
                "label": f"RSI {p}",
                "scale": "osc",
                "kind": "line",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                "range": {"min": 0.0, "max": 100.0},
                **_stats(frame[col]),
                "lines": [{"name": col, "data": _points(frame[col], settings)}],
            }
        )

    if settings.feature_atr_enabled:
        p = settings.features_atr_period
        col = f"atr_{p}"
        overlays.append(
            {
                "key": col,
                "label": f"ATR {p}",
                "scale": "osc",
                "kind": "line",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                **_stats(frame[col]),
                "lines": [{"name": col, "data": _points(frame[col], settings)}],
            }
        )

    if settings.feature_momentum_enabled:
        for p in settings.momentum_periods:
            col = f"mom_{p}"
            overlays.append(
                {
                    "key": col,
                    "label": f"Momentum {p}",
                    "scale": "osc",
                    "kind": "line",
                    "color": _PALETTE[len(overlays) % len(_PALETTE)],
                    **_stats(frame[col]),
                    "lines": [{"name": col, "data": _points(frame[col], settings)}],
                }
            )

    if settings.feature_volatility_enabled:
        p = settings.features_volatility_period
        col = f"vol_{p}"
        overlays.append(
            {
                "key": col,
                "label": f"Volatility {p}",
                "scale": "osc",
                "kind": "line",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                **_stats(frame[col]),
                "lines": [{"name": col, "data": _points(frame[col], settings)}],
            }
        )

    if settings.feature_volume_enabled:
        p = settings.features_volume_period
        col = f"vratio_{p}"
        overlays.append(
            {
                "key": col,
                "label": f"Relative Volume {p}",
                "scale": "osc",
                "kind": "line",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                **_stats(frame[col]),
                "lines": [{"name": col, "data": _points(frame[col], settings)}],
            }
        )

    if settings.feature_volume_abs_enabled and "volume_abs" in frame:
        overlays.append(
            {
                "key": "volume_abs",
                "label": "Volume (absolute)",
                "scale": "osc",
                "kind": "histogram",
                "color": _PALETTE[len(overlays) % len(_PALETTE)],
                **_stats(frame["volume_abs"]),
                "lines": [{"name": "volume_abs", "data": _points(frame["volume_abs"], settings)}],
            }
        )

    return overlays
