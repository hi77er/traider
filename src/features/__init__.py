"""Features Engineering — price -> indicator features for the signal model.

Public API:
- ``FeatureEngineer`` — config-aware ``compute_frame`` / ``compute_latest``
- ``compute_features`` / ``latest_features`` — module-level shortcuts
- ``active_feature_columns`` — canonical ordered feature-name contract
- ``indicators`` — pure pandas indicator functions (sma, rsi, atr, ...)

Identical code path in backtest and live; all windows/periods and on/off
toggles come from ``Settings`` (``.env``).
"""

from src.features import indicators
from src.features.engineering import (
    FeatureEngineer,
    compute_features,
    latest_features,
)
from src.features.schema import active_feature_columns

__all__ = [
    "FeatureEngineer",
    "active_feature_columns",
    "compute_features",
    "indicators",
    "latest_features",
]
