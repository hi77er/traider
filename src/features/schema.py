"""Feature column names — the single source of truth for the feature schema.

The signal model expects the exact columns, in the exact order, returned here.
Feature names embed their window/period so adding a window never collides with
an existing column (e.g. ``sma_20`` vs ``sma_50``).

"""

from __future__ import annotations

from typing import List

from src.config.settings import Settings

__all__ = ["active_feature_columns", "allowed_series", "macd_columns", "macd_tag"]

# Raw OHLCV bar fields that are ALWAYS available to a rule, in addition to the
# engineered feature columns (mirrors the Rules panel's series picker).
RAW_SERIES = ("open", "high", "low", "close", "volume")

# The three columns a MACD parameter set produces, in output order.
MACD_PARTS = ("macd", "macd_signal", "macd_hist")


def macd_tag(settings: Settings) -> str:
    """The ``fast_slow_signal`` suffix shared by every MACD column."""
    return (
        f"{settings.features_macd_fast_period}_"
        f"{settings.features_macd_slow_period}_"
        f"{settings.features_macd_signal_period}"
    )


def macd_columns(settings: Settings) -> List[str]:
    """MACD line / signal / histogram columns, e.g. ``macd_12_26_9``.

    All three are exposed as features: the histogram is what most MACD rules
    reference (zero crossovers), but the line and signal are what a crossover
    rule compares, so hiding them would make the indicator half-usable.
    """
    tag = macd_tag(settings)
    return [f"{part}_{tag}" for part in MACD_PARTS]


def allowed_series(settings: Settings) -> List[str]:
    """Ordered series a rule may reference: raw OHLCV + active features."""
    out: List[str] = list(RAW_SERIES)
    for col in active_feature_columns(settings):
        if col not in out:
            out.append(col)
    return out


def active_feature_columns(settings: Settings) -> List[str]:
    """Ordered feature columns for the enabled indicators in the config."""
    cols: List[str] = []
    if settings.feature_sma_enabled:
        cols.extend(f"sma_{p}" for p in settings.sma_periods)
    if settings.feature_ema_enabled:
        cols.extend(f"ema_{p}" for p in settings.ema_periods)
    if settings.feature_macd_enabled:
        cols.extend(macd_columns(settings))
    if settings.feature_rsi_enabled:
        cols.append(f"rsi_{settings.features_rsi_period}")
    if settings.feature_atr_enabled:
        cols.append(f"atr_{settings.features_atr_period}")
    if settings.feature_bollinger_enabled:
        cols.append(f"bb_pctb_{settings.features_bollinger_period}")
    if settings.feature_momentum_enabled:
        cols.extend(f"mom_{p}" for p in settings.momentum_periods)
    if settings.feature_volatility_enabled:
        cols.append(f"vol_{settings.features_volatility_period}")
    if settings.feature_vwap_enabled:
        cols.append(f"vwap_{settings.features_vwap_period}")
    if settings.feature_volume_enabled:
        cols.append(f"vratio_{settings.features_volume_period}")
    if settings.feature_volume_abs_enabled:
        cols.append("volume_abs")
    return cols
