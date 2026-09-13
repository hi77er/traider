"""Canonical historical dataset store.

Each symbol/interval is persisted as a single Parquet file under
``HISTORICAL_DATA_DIR`` (e.g. ``data/historical/AAPL_1d.parquet``). This is
the stable dataset that backtesting and model training read — separate from
the transient query-keyed CSV cache in ``CACHE_DIR`` used by the live poll.

Why Parquet (not DynamoDB / a time-series DB): backtesting and training need
bulk sequential reads of the whole series for vectorized pandas work, the
volume is tiny (thousands of bars per instrument), and DynamoDB is already
reserved for trading state. Parquet loads directly into pandas, is columnar
and compressed, and is trivially appendable with dedupe.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config.settings import Settings
from src.data.openbb_client import PRICE_COLUMNS

logger = logging.getLogger(__name__)


def dataset_path(settings: Settings, symbol: str, interval: str) -> Path:
    """Path of the canonical dataset file for a symbol/interval."""
    return Path(settings.historical_data_dir) / f"{symbol}_{interval}.parquet"


def is_intraday(interval: Optional[str]) -> bool:
    """True for sub-daily bar sizes (1h/4h/...); calendar bars (1d/1W/1M)
    are keyed by their date alone."""
    return str(interval or "").lower() not in ("1d", "1w", "1m")


def chart_time(
    ts, interval: Optional[str], market_timezone: Optional[str] = None
):
    """lightweight-charts ``time`` value for a bar timestamp.

    Daily+ bars use a ``'YYYY-MM-DD'`` business-day string. Intraday bars use a
    UTC unix-seconds integer so every bar is a unique, strictly increasing time
    (date-collapsed duplicate times are rejected by lightweight-charts, which
    left the chart blank for 1h data). Naive intraday timestamps are treated as
    ``market_timezone`` local (they are stored exchange-local, e.g. 09:30 ET).
    """
    t = pd.Timestamp(ts)
    if not is_intraday(interval):
        return str(t.date())
    if t.tz is None:
        t = t.tz_localize(market_timezone or "America/New_York")
    return int(t.timestamp())


def bar_label(ts, interval: Optional[str]) -> str:
    """Human-readable row label: date for daily bars, date+time for intraday."""
    t = pd.Timestamp(ts)
    if not is_intraday(interval):
        return str(t.date())
    return str(t.to_pydatetime())[:16]


def s3_enabled(settings: Settings) -> bool:
    """True when S3 sync is configured (enabled + a bucket is set)."""
    return bool(getattr(settings, "s3_enabled", False) and settings.s3_bucket)


def s3_key(settings: Settings, symbol: str, interval: str) -> str:
    """S3 object key for a symbol/interval dataset."""
    prefix = settings.s3_prefix.strip("/")
    return f"{prefix}/{symbol}_{interval}.parquet" if prefix else f"{symbol}_{interval}.parquet"


def _drop_invalid_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Drop bars with no usable price (NaN in any of ``PRICE_COLUMNS``).

    Providers occasionally emit a placeholder row for a session that has not
    settled yet — it has an open and a high but a NaN close. Storing such a row
    poisons everything downstream: the chart goes blank (lightweight-charts
    rejects a ``null`` value and abandons the whole series, so no candles are
    drawn at all) and the NaN close propagates into features and the backtest.

    Filtering on READ heals datasets already at rest; filtering on WRITE keeps
    them clean. Volume is deliberately not required — a legitimate bar can be
    volume-less (and only a missing price makes a bar untradable).
    """
    if df is None or df.empty:
        return df
    cols = [c for c in PRICE_COLUMNS if c in df.columns]
    if not cols:
        return df
    blank = df[cols].isna().any(axis=1)
    if blank.any():
        logger.warning(
            "Dropped %d bar(s) with a missing price: %s",
            int(blank.sum()),
            ", ".join(str(ts) for ts in df.index[blank][:5]),
        )
        return df[~blank]
    return df


def load_dataset(
    settings: Settings,
    symbol: str,
    interval: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    """Load the canonical dataset (optionally sliced to a date window).

    Reads the local Parquet file; when it's missing and S3 sync is enabled,
    the dataset is first restored from S3. Returns an empty DataFrame when no
    dataset exists yet. Result is indexed by datetime with canonical OHLCV.
    """
    path = dataset_path(settings, symbol, interval)
    if not path.exists() and s3_enabled(settings):
        _s3_download(settings, symbol, interval, path)
    if not path.exists():
        logger.info("No dataset yet at %s", path)
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df = _drop_invalid_bars(df)  # heal datasets written before this guard
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    logger.info("Loaded %s rows from dataset %s", len(df), path)
    return df


def delete_dataset(settings: Settings, symbol: str, interval: str) -> bool:
    """Delete the canonical dataset file for symbol/interval (local + S3).

    Returns True when a local file existed and was removed.
    """
    path = dataset_path(settings, symbol, interval)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
        logger.info("Deleted dataset %s", path)
    if s3_enabled(settings):
        try:
            _s3_client(settings).delete_object(
                Bucket=settings.s3_bucket, Key=s3_key(settings, symbol, interval)
            )
        except Exception as exc:  # noqa: BLE001 - object may be missing
            logger.warning("Could not delete dataset from S3: %s", exc)
    return removed


def save_dataset(
    settings: Settings,
    df: pd.DataFrame,
    symbol: str,
    interval: str,
) -> Path:
    """Merge ``df`` into the canonical dataset for symbol/interval and write.
    New rows are deduped against existing ones (last-write-wins per timestamp)
    so repeated backfills or appended daily bars never create duplicates.
    After writing locally, the file is uploaded to S3 when sync is enabled
    (write-then-upload keeps the local file consistent; enable bucket
    versioning for crash safety).
    """
    path = dataset_path(settings, symbol, interval)
    if df is None or df.empty:
        return path
    # Never persist a bar without a price: the dedupe below keeps the LAST row
    # per timestamp, so a bad re-fetch would silently overwrite a good row.
    df = _drop_invalid_bars(df)
    if df.empty:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_dataset(settings, symbol, interval)
    merged = _merge(existing, df)
    merged.to_parquet(path)
    logger.info("Saved %s rows to dataset %s", len(merged), path)

    if s3_enabled(settings):
        _s3_upload(settings, symbol, interval, path)
    return path


# -- S3 sync (durable source of truth) --------------------------------------
def _s3_client(settings: Settings):
    """Lazy boto3 S3 client (raises a clear error if boto3 is missing)."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "S3 sync is enabled but boto3 is not installed. Run `pip install boto3`."
        ) from exc
    kwargs = {"region_name": settings.aws_region}
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    return boto3.client("s3", **kwargs)


def _s3_upload(settings: Settings, symbol: str, interval: str, path: Path) -> None:
    client = _s3_client(settings)
    key = s3_key(settings, symbol, interval)
    client.upload_file(str(path), settings.s3_bucket, key)
    logger.info("Uploaded dataset to s3://%s/%s", settings.s3_bucket, key)


def _s3_download(settings: Settings, symbol: str, interval: str, path: Path) -> None:
    client = _s3_client(settings)
    key = s3_key(settings, symbol, interval)
    try:
        client.download_file(settings.s3_bucket, key, str(path))
        logger.info("Restored dataset from s3://%s/%s", settings.s3_bucket, key)
    except Exception as exc:  # object missing / no access -> leave local missing
        logger.warning("Could not restore dataset from S3: %s", exc)


def _merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Concatenate, dedupe on index (last wins), sort ascending."""
    if existing is None or existing.empty:
        return new.sort_index()
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.index.name = "date"
    return combined
