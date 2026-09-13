"""Data layer — fetch market data via the OpenBB Platform.

Public API:
- ``OpenBBClient`` / ``OpenBBError`` (wrapper, normalization, caching)
- ``fetch_candles`` (historical OHLCV for backtesting/training)
- ``get_latest_candle`` / ``is_market_open`` (live polling)
- ``load_dataset`` / ``save_dataset`` / ``dataset_path`` (canonical Parquet store)
"""

from .dataset import dataset_path, load_dataset, save_dataset
from .delta import dataset_delta_status, eligible_until_date, sync_missing_days
from .historical import fetch_candles
from .live import get_latest_candle, is_market_open
from .openbb_client import OHLCV_COLUMNS, OpenBBClient, OpenBBError

__all__ = [
    "OHLCV_COLUMNS",
    "OpenBBClient",
    "OpenBBError",
    "dataset_delta_status",
    "dataset_path",
    "eligible_until_date",
    "fetch_candles",
    "get_latest_candle",
    "is_market_open",
    "load_dataset",
    "save_dataset",
    "sync_missing_days",
]
