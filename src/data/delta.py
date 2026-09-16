"""Daily delta maintenance for the canonical Parquet dataset.

A daily bar is only final after the market close (``TRADING_END_HOUR`` in
``MARKET_TIMEZONE``). These helpers:

- compute which *completed* trading days are missing from the dataset, and
- fetch + merge those missing bars so the file stays up to date.

Missing days are computed **provider-grounded**: we only ever list dates for
which the provider actually returned a bar (between the dataset's last date
and the latest eligible date). Exchange holidays are therefore never
misreported as missing — the provider simply has no bar for them.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import session
from src.config.settings import Settings
from src.data import dataset
from src.data.dataset import load_dataset, save_dataset
from src.data.openbb_client import OpenBBClient

logger = logging.getLogger(__name__)

_RECENT = 5  # bars shown in the "All data synced" state

# The web UI's automatic delta check hits the provider whenever the dataset
# lags the eligible date — and the dashboard reloads often (strategy switch,
# config/history save all reload it). Without a guard, every reload burns
# another provider request, which is exactly how you trip yfinance's rate
# limiter for the whole day. So automatic checks (no client injected = the
# live endpoint path) are throttled to at most one provider fetch per window;
# explicit callers (tests, and the post-sync status inside sync_missing_days,
# which always pass a client) always fetch fresh.
_AUTO_CHECK_WINDOW_S = 900.0  # 15 min between automatic provider fetches
_auto_check_cache = {}  # (symbol, interval) -> (monotonic_ts, fetched_df | None, exc | None)
_auto_check_lock = threading.Lock()

# Detecting gaps in the MIDDLE of the dataset is provider-grounded too: a
# weekday can be absent because it's a market holiday, or because a bar was
# genuinely lost mid-set. Confirming against the provider needs a full-span
# fetch, so it is throttled harder than the small tail check.
_RECONCILE_WINDOW_S = 12 * 3600.0  # 12h between full-span reconciliation fetches
_reconcile_cache = {}  # (symbol, interval) -> (monotonic_ts, provider_df | None)
_reconcile_lock = threading.Lock()


def eligible_until_date(settings: Settings, now: Optional[datetime] = None) -> date:
    """Latest date whose daily bar is final (complete) at ``now``.

    Before the configured market close, today's bar is still forming, so the
    latest eligible date is the previous weekday. At/after close (on a
    weekday) it is today. Weekends roll back to the previous Friday.

    This is the DAILY rule, and it is the right answer for a calendar bar and the wrong
    one for anything intraday — see ``sync_missing_days``. Delegates to
    ``dataset.eligible_date`` so there is one implementation: delta imports that module
    already, so the shared rule lives in the lower of the two.
    """
    tz = ZoneInfo(settings.market_timezone)
    moment = now or datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return dataset.eligible_date(settings, moment.astimezone(tz))


def dataset_delta_status(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Compute the daily-delta state of the dataset (no writes).

    Returns metadata about the dataset plus the provider-grounded list of
    completed trading days that are missing from it.
    """
    auto_check = client is None  # live endpoint path -> throttle provider hits
    client = client or OpenBBClient(settings)
    symbol = settings.instrument
    interval = settings.historical_bar_size
    base = {
        "symbol": symbol,
        "interval": interval,
        "eligible_until": eligible_until_date(settings, now).isoformat(),
        "missing": [],
        "synced": True,
        "last_available": None,
        "error": None,
    }
    df = load_dataset(settings, symbol, interval)
    if df.empty:
        return {
            **base,
            "exists": False,
            "last_date": None,
            "rows": 0,
            "recent": [],
        }

    last_date = df.index.max().date()
    first_date = df.index.min().date()
    eligible = eligible_until_date(settings, now)
    have = {ts.date() for ts in df.index}
    missing: List[date] = []
    last_available: Optional[str] = last_date.isoformat()

    # 1) Tail — completed days after the dataset's last bar that the provider
    #    traded but we haven't stored yet.
    if last_date < eligible:
        fetched = (
            _auto_fetch_after(settings, client, last_date)
            if auto_check
            else _fetch_after(settings, client, last_date)
        )
        if fetched is not None and not fetched.empty:
            pending = [
                ts.date()
                for ts in fetched.index
                if last_date < ts.date() <= eligible and ts.date() not in have
            ]
            missing = sorted(pending)
            last_available = fetched.index.max().date().isoformat()
        elif fetched is not None:
            last_available = last_date.isoformat()

    # 2) Interior — holes in the MIDDLE of the stored range. A tail-only check
    #    never notices these once later bars arrive (e.g. a bar lost mid-set).
    #    Absent weekdays are candidates; each is confirmed against the provider
    #    so genuine market holidays (e.g. Labor Day) are never misreported.
    interior = _interior_candidates(have, first_date, last_date)
    if interior:
        span = _provider_span(settings, client, first_date, last_date)
        if span is not None and not span.empty:
            prov_dates = {ts.date() for ts in span.index}
            holes = sorted(d for d in interior if d in prov_dates)
            if holes:
                missing = sorted(set(missing) | set(holes))

    return {
        **base,
        "exists": True,
        "last_date": last_date.isoformat(),
        "rows": int(len(df)),
        "recent": _recent_rows(df),
        "missing": [d.isoformat() for d in missing],
        "synced": not missing,
        "last_available": last_available,
    }


def sync_missing_days(
    settings: Settings,
    client: Optional[OpenBBClient] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Fetch + merge any missing completed daily bars, then return the new state.

    One provider-grounded pass over the whole stored range merges every bar the
    provider traded in [first_date, eligible-until] that is missing from the
    dataset — filling the tail AND any real holes in the middle. Mid-day runs
    never write today's still-forming bar (eligible-until excludes it), and
    ``save_dataset`` dedupes by timestamp so the operation is idempotent.
    """
    client = client or OpenBBClient(settings)
    symbol = settings.instrument
    interval = settings.historical_bar_size

    df = load_dataset(settings, symbol, interval)
    if df.empty:
        return dataset_delta_status(settings, client, now)

    # The bound is the newest bar that has FINISHED FORMING, as a canonical stamp. This
    # used to be ``eligible_until_date`` — a DATE — which for an hourly dataset before the
    # close is YESTERDAY, so today's closed bars could never reach the dataset during the
    # session and a live tick would decide on yesterday's bar at today's price.
    until = dataset.bar_stamp(settings, dataset.last_closed_bar(settings, now))
    first_ts = dataset.bar_stamp(settings, df.index.min())
    last_ts = dataset.bar_stamp(settings, df.index.max())
    have_bars = {dataset.bar_stamp(settings, ts) for ts in df.index}

    # The interior check stays DAY-based: it is asking "is a whole trading day missing",
    # which is the right question for a tail that never notices a hole in the middle, and
    # the wrong one for per-bar completeness (which the tail comparison above covers).
    have_days = {ts.date() for ts in df.index}
    first_date = df.index.min().date()
    last_date = df.index.max().date()
    interior = _interior_candidates(have_days, first_date, last_date)
    if last_ts >= until and not interior:
        return dataset_delta_status(settings, client, now)

    fetched = client.fetch_historical(
        symbol=symbol,
        start_date=first_date.isoformat(),
        end_date=None,
        interval=interval,
        use_cache=False,
    )
    if fetched is not None and not fetched.empty:
        wanted = [
            ts for ts in fetched.index
            if first_ts <= dataset.bar_stamp(settings, ts) <= until
            and dataset.bar_stamp(settings, ts) not in have_bars
        ]
        keep = fetched[fetched.index.isin(wanted)] if wanted else fetched.iloc[0:0]
        if not keep.empty:
            logger.info("Syncing %s missing bar(s) for %s", len(keep), symbol)
            save_dataset(settings, keep, symbol, interval)
        else:
            logger.info("No missing bars for %s (through %s)", symbol, until)
        _remember_provider_span(settings, fetched)
    return dataset_delta_status(settings, client, now)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fetch_after(settings: Settings, client: OpenBBClient, last_date: date) -> pd.DataFrame:
    """Fetch provider bars strictly after ``last_date`` (no dataset write)."""
    start = (last_date + timedelta(days=1)).isoformat()
    logger.info("Fetching bars after %s for delta check (%s)", start, settings.instrument)
    return client.fetch_historical(
        symbol=settings.instrument,
        start_date=start,
        end_date=None,
        interval=settings.historical_bar_size,
        use_cache=False,
    )


def _auto_fetch_after(
    settings: Settings, client: OpenBBClient, last_date: date
) -> pd.DataFrame:
    """Like ``_fetch_after`` but throttled for automatic (page-load) checks.

    Reuses the most recent fetch within ``_AUTO_CHECK_WINDOW_S`` so a page
    reload cannot hammer a rate-limited provider. If the last attempt in the
    window failed, the stored exception is re-raised without another request.
    """
    key = (settings.instrument, settings.historical_bar_size)
    with _auto_check_lock:
        hit = _auto_check_cache.get(key)
        if hit and _time.monotonic() - hit[0] < _AUTO_CHECK_WINDOW_S:
            if hit[2] is not None:
                raise hit[2]
            return hit[1]
    try:
        fetched = _fetch_after(settings, client, last_date)
    except Exception as exc:
        with _auto_check_lock:
            _auto_check_cache[key] = (_time.monotonic(), None, exc)
        raise
    with _auto_check_lock:
        _auto_check_cache[key] = (_time.monotonic(), fetched, None)
    return fetched


def _interior_candidates(have, first: date, last: date) -> List[date]:
    """Absent weekdays strictly between ``first`` and ``last``.

    Weekends are excluded; market holidays are still candidates here and are
    filtered out by the provider confirmation in the caller.
    """
    out: List[date] = []
    d = first + timedelta(days=1)
    while d < last:
        if d.weekday() < 5 and d not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


def _provider_span(
    settings: Settings, client: OpenBBClient, first: date, last: date
) -> Optional[pd.DataFrame]:
    """Provider frame over [first, last], throttled to _RECONCILE_WINDOW_S.

    Confirms which interior absent weekdays are REAL holes (the provider traded
    them) vs market holidays (the provider has no bar). Returns None when the
    provider can't be reached; the failed attempt is remembered for the window
    so a down/rate-limited provider is not retried on every status check.
    """
    key = (settings.instrument, settings.historical_bar_size)
    with _reconcile_lock:
        hit = _reconcile_cache.get(key)
        if hit and _time.monotonic() - hit[0] < _RECONCILE_WINDOW_S:
            return hit[1]
    end = (last + timedelta(days=1)).isoformat()  # inclusive end for providers
    try:
        frame = client.fetch_historical(
            symbol=settings.instrument,
            start_date=first.isoformat(),
            end_date=end,
            interval=settings.historical_bar_size,
            use_cache=False,
        )
    except Exception as exc:  # noqa: BLE001 - provider may be down/rate-limited
        logger.warning("Delta interior reconcile failed for %s: %s", settings.instrument, exc)
        frame = None
    with _reconcile_lock:
        _reconcile_cache[key] = (_time.monotonic(), frame)
    return frame


def _remember_provider_span(settings: Settings, frame: pd.DataFrame) -> None:
    """Seed the interior-reconcile cache after an explicit full fetch (sync)."""
    key = (settings.instrument, settings.historical_bar_size)
    with _reconcile_lock:
        _reconcile_cache[key] = (_time.monotonic(), frame)


def _recent_rows(df: pd.DataFrame, n: int = _RECENT) -> List[dict]:
    """Last ``n`` dataset rows as JSON-ready records (ascending)."""
    rows: List[dict] = []
    for ts, r in df.tail(n).iterrows():
        rows.append(
            {
                "date": str(ts.date()),
                "open": round(float(r.open), 4),
                "high": round(float(r.high), 4),
                "low": round(float(r.low), 4),
                "close": round(float(r.close), 4),
                "volume": int(r.volume),
            }
        )
    return rows
