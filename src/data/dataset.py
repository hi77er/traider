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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import session
from src.config.settings import Settings
from src.data.openbb_client import PRICE_COLUMNS

logger = logging.getLogger(__name__)


def dataset_path(settings: Settings, symbol: str, interval: str) -> Path:
    """Path of the canonical dataset file for a symbol/interval."""
    return Path(settings.historical_data_dir) / f"{symbol}_{interval}.parquet"


# Bar codes that are keyed by their DATE alone. Compared case-sensitively and by
# exact match, because the provider's notation collides otherwise: "1m" is one
# MINUTE and "1M" is one MONTH. Anything not in this set is a sub-daily bar.
_CALENDAR_BARS = frozenset({"1d", "5d", "3d", "1W", "2W", "1M", "2M", "3M", "1Q"})


def is_intraday(interval: Optional[str]) -> bool:
    """True for sub-daily bar sizes (1m/15m/1h/4h/...); calendar bars (1d/1W/1M)
    are keyed by their date alone."""
    return str(interval or "") not in _CALENDAR_BARS


def bar_in_trading_window(settings: Settings, ts) -> bool:
    """True when a decision may be taken on the bar stamped ``ts``.

    A *calendar* bar (1d or coarser) is always eligible: it IS a session, its
    decision is taken at that session's close and filled at the next session's open,
    so there is no clock time to compare. An *intraday* bar is eligible only when its
    own timestamp — exchange-local, and the bar's START for every provider we use —
    falls inside ``TRADING_START_HOUR``–``TRADING_END_HOUR`` on a weekday. That is
    what makes the window a real bound on decisions rather than a note in the docs:
    neither the backtest nor a live tick can act on a pre-market, after-hours or
    overnight bar, and neither can trade on a weekend.
    """
    if not is_intraday(settings.historical_bar_size):
        return True
    stamp = pd.Timestamp(ts)
    if stamp.tz is None:
        stamp = stamp.tz_localize(settings.market_timezone or "America/New_York")
    return session.is_open_at(settings, stamp.to_pydatetime())


# ---------------------------------------------------------------------------
# the bar grid: which bar is this, when did it close, when is the next one
# ---------------------------------------------------------------------------
# Everything below exists because a bar's timestamp alone is not enough to reason with,
# and because the DATASET does not agree with itself about how to write one down: a daily
# index comes back UTC-aware (``2026-09-15 00:00:00+00:00``) while an hourly one comes
# back naive and exchange-local (``2026-09-15 15:30:00``). Comparing the two, or keying
# an idempotency check on ``str(ts)``, silently depends on which provider wrote the file.
# These two functions are the ONE conversion, so every caller agrees.

_INTERVAL_UNITS = {"m": 1, "h": 60, "d": 1440, "W": 7 * 1440, "M": 30 * 1440, "Q": 91 * 1440}


def interval_minutes(interval: Optional[str]) -> Optional[int]:
    """``"15m"``/``"1h"``/``"1d"`` → minutes. ``None`` when it cannot be read.

    Month/quarter lengths are approximations on purpose: this is used for BUFFERING a
    fetch and for stepping a bar grid, never for dating a bar.
    """
    text = str(interval or "").strip()
    if not text:
        return None
    unit = text[-1]
    number = text[:-1]
    if unit not in _INTERVAL_UNITS or not number.isdigit():
        return None
    return int(number) * _INTERVAL_UNITS[unit]


def bar_stamp(settings: Settings, ts) -> pd.Timestamp:
    """A bar's time in ONE canonical form — UTC-aware — whatever the file holds.

    Naive intraday stamps are exchange-local (``15:30`` means half past three in New
    York) and naive calendar stamps are UTC dates, because that is how the two kinds are
    actually stored. Localising them the same way would shift every daily bar by the
    session offset and make a stored bar look missing.
    """
    stamp = pd.Timestamp(ts)
    if stamp.tz is None:
        zone = "UTC" if not is_intraday(settings.historical_bar_size) else (
            settings.market_timezone or "America/New_York"
        )
        stamp = stamp.tz_localize(zone)
    return stamp.tz_convert("UTC")


def bar_key(settings: Settings, ts) -> str:
    """The IDEMPOTENCY KEY for a bar: ``bar_stamp`` as ISO text.

    ``str(Timestamp)`` is not one. An hourly index stringifies as
    ``"2026-09-15 15:30:00"`` and a daily one as ``"2026-09-15 00:00:00+00:00"``, so a
    key stored from one and compared against the other never matches — which re-fires a
    decision on a bar that was already acted on. That is the one failure here that costs
    money rather than time.
    """
    return bar_stamp(settings, ts).isoformat()


def last_closed_bar(settings: Settings, now=None):
    """The newest bar that has FINISHED FORMING at ``now``, as providers stamp it.

    This is the question an intraday tick actually needs to ask, and the one
    ``eligible_until_date`` cannot answer: that returns the latest date whose *daily* bar
    is complete, which before the close is yesterday — so an hourly dataset could never
    receive today's bars during the session.

    A bar stamped ``T`` covers ``[T, T + size)``, and the session's last bar is cut short
    by the close. So a bar is closed when its own interval has passed, or as soon as the
    session ends. On a weekend, before the open, or after the close, the answer is the
    final bar of the most recent session.
    """
    tz = ZoneInfo(settings.market_timezone or "America/New_York")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(tz)

    interval = settings.historical_bar_size
    if not is_intraday(interval):
        # A calendar bar IS its session: the eligible date's bar is the last complete one.
        return pd.Timestamp(eligible_date(settings, moment), tz="UTC")

    size = interval_minutes(interval) or 60
    open_at = session.parse_hhmm(settings.trading_start_hour)
    close_at = session.parse_hhmm(settings.trading_end_hour)

    def session_bars(day) -> list:
        """Every stamp in that session, in order — the grid the provider actually uses."""
        stamps, cursor = [], datetime.combine(day, open_at).replace(tzinfo=tz)
        end = datetime.combine(day, close_at).replace(tzinfo=tz)
        while cursor < end:
            stamps.append(pd.Timestamp(cursor))
            cursor += timedelta(minutes=size)
        return stamps

    def previous_session(day) -> list:
        back = day - timedelta(days=1)
        while back.weekday() >= 5:
            back -= timedelta(days=1)
        return session_bars(back)

    today = moment.date()
    if today.weekday() >= 5:
        bars = previous_session(today)
        return bars[-1] if bars else None
    if moment.time() >= close_at:
        # The session is over, so its last (short) bar is complete too.
        bars = session_bars(today)
        return bars[-1] if bars else None
    if moment.time() < open_at:
        bars = previous_session(today)
        return bars[-1] if bars else None

    closed = [
        stamp for stamp in session_bars(today)
        if stamp + timedelta(minutes=size) <= moment
    ]
    if closed:
        return closed[-1]
    bars = previous_session(today)
    return bars[-1] if bars else None


def eligible_date(settings: Settings, moment: datetime):
    """The most recent weekday that is over — the anchor for a calendar bar.

    Deliberately re-derived here rather than imported from ``src/data/delta``: delta
    imports THIS module, and a cycle for one date calculation would be worse than the
    four lines.
    """
    day = moment.date()
    if day.weekday() >= 5 or moment.time() < session.parse_hhmm(settings.trading_end_hour):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def next_bar_boundary(settings: Settings, now=None):
    """When the next bar will have closed — the moment the loop should wake.

    Derived from the SESSION and the bar size, never from the clock's round numbers: this
    market opens at :30, so an hourly grid is :30-past, not on the hour, and a loop that
    woke at :00 would always be half a bar early or late. The session's last bar is cut
    short by the close, so the close itself is a boundary too.

    Returns ``None`` only for a bar size that cannot be parsed.
    """
    tz = ZoneInfo(settings.market_timezone or "America/New_York")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(tz)

    open_at = session.parse_hhmm(settings.trading_start_hour)
    close_at = session.parse_hhmm(settings.trading_end_hour)
    interval = settings.historical_bar_size

    if not is_intraday(interval):
        # A calendar bar closes with its session: the next boundary is the next session's
        # close, since today's has either passed or is the one we are waiting for.
        day = moment.date()
        if day.weekday() < 5 and moment.time() < close_at:
            return datetime.combine(day, close_at).replace(tzinfo=tz)
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return datetime.combine(day, close_at).replace(tzinfo=tz)

    size = interval_minutes(interval)
    if not size:
        return None

    def boundaries(day) -> list:
        out, cursor = [], datetime.combine(day, open_at).replace(tzinfo=tz)
        end = datetime.combine(day, close_at).replace(tzinfo=tz)
        while cursor < end:
            out.append(min(cursor + timedelta(minutes=size), end))
            cursor += timedelta(minutes=size)
        return out

    day = moment.date()
    for _ in range(10):  # a fortnight of weekdays is plenty; this only skips weekends
        if day.weekday() < 5:
            ahead = [b for b in boundaries(day) if b > moment]
            if ahead:
                return ahead[0]
        day += timedelta(days=1)
    return None


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
