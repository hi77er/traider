"""Feature Engineering — turn OHLCV candles into the model's feature vector.

The single most important guarantee: **the same computation runs in backtest,
training and live.** ``compute_frame`` (full history, vectorized) and
``compute_latest`` (the live decision) share one code path — ``compute_latest``
simply runs ``compute_frame`` on the available history and takes the last row.
All parameters and on/off toggles come from ``Settings`` (``.env``); disabled
indicators are simply omitted from the output frame, so the model's feature
columns always match ``FeatureEngineer.feature_columns``.
"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from src.config.settings import Settings, get_settings
from src.features import indicators
from src.features.schema import active_feature_columns

__all__ = ["FeatureEngineer", "compute_features", "latest_features"]


class FeatureEngineer:
    """Config-aware feature computation over canonical OHLCV candles."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    # -- schema ----------------------------------------------------------
    @property
    def feature_columns(self) -> list:
        """Active feature columns in their canonical order (the model contract)."""
        return active_feature_columns(self._settings)

    @property
    def warmup(self) -> int:
        """Bars needed before indicators are stable (FEATURES_MIN_LOOKBACK)."""
        return int(self._settings.features_min_lookback)

    # -- computation -----------------------------------------------------
    def compute_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Vectorized features for the whole history (one row per bar).

        Returns a DataFrame sharing ``df``'s index with the active feature
        columns. Rows inside an indicator's warmup are NaN.
        """
        if df is None or df.empty:
            return pd.DataFrame(index=pd.DatetimeIndex([]))
        if "close" not in df.columns:
            raise ValueError("Feature input must contain a 'close' column")

        out = pd.DataFrame(index=df.index)
        close = df["close"]

        if self._settings.feature_sma_enabled:
            for p in self._settings.sma_periods:
                out[f"sma_{p}"] = indicators.sma(close, p)

        if self._settings.feature_rsi_enabled:
            out[f"rsi_{self._settings.features_rsi_period}"] = indicators.rsi(
                close, self._settings.features_rsi_period
            )

        if self._settings.feature_atr_enabled:
            out[f"atr_{self._settings.features_atr_period}"] = indicators.atr(
                df["high"], df["low"], close, self._settings.features_atr_period
            )

        if self._settings.feature_bollinger_enabled:
            out[f"bb_pctb_{self._settings.features_bollinger_period}"] = indicators.bollinger_pctb(
                close,
                self._settings.features_bollinger_period,
                self._settings.features_bollinger_std,
            )

        if self._settings.feature_momentum_enabled:
            for p in self._settings.momentum_periods:
                out[f"mom_{p}"] = indicators.momentum(close, p)

        if self._settings.feature_volatility_enabled:
            out[f"vol_{self._settings.features_volatility_period}"] = indicators.rolling_vol(
                close, self._settings.features_volatility_period
            )

        volume = df["volume"] if "volume" in df.columns else None

        if self._settings.feature_vwap_enabled and volume is not None:
            out[f"vwap_{self._settings.features_vwap_period}"] = indicators.vwap(
                df["high"], df["low"], close, volume, self._settings.features_vwap_period
            )

        if self._settings.feature_volume_enabled and volume is not None:
            out[f"vratio_{self._settings.features_volume_period}"] = indicators.volume_ratio(
                volume, self._settings.features_volume_period
            )

        if self._settings.feature_volume_abs_enabled and volume is not None:
            # Raw traded volume per candle — it is not an indicator, so it has
            # no warmup and is available from the very first bar.
            out["volume_abs"] = volume.astype("float64")

        return out

    def compute_latest(self, df: pd.DataFrame) -> Dict[str, float]:
        """Feature vector for the most recent bar (the live decision row).

        Identical math to ``compute_frame`` — same function, last row only.
        Rows that are still in warmup yield NaN values; callers should treat a
        row containing NaN as \"no decision\" (see ``is_ready``).
        """
        frame = self.compute_frame(df)
        if frame.empty:
            return {}
        row = frame.iloc[-1]
        return {col: (float(row[col]) if pd.notna(row[col]) else None) for col in frame.columns}

    def is_ready(self, df: pd.DataFrame) -> bool:
        """True when enough history exists for a complete feature row."""
        return len(df) >= self.warmup and len(df) > 0


def compute_features(settings: Optional[Settings], df: pd.DataFrame) -> pd.DataFrame:
    """Module-level convenience around ``FeatureEngineer.compute_frame``."""
    return FeatureEngineer(settings).compute_frame(df)


def latest_features(settings: Optional[Settings], df: pd.DataFrame) -> Dict[str, float]:
    """Module-level convenience around ``FeatureEngineer.compute_latest``."""
    return FeatureEngineer(settings).compute_latest(df)
