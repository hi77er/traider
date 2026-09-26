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
from datetime import date, datetime, timedelta
from typing import List, NamedTuple, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import session
from src.config.settings import Settings
from src.data import dataset
from src.data.dataset import load_dataset, save_dataset
from src.data.openbb_client import NoDataError, OpenBBClient

logger = logging.getLogger(__name__)

_RECENT = 5  # bars shown in the "All data synced" state

# The missing-bar list is meant to be read bar by bar, but an intraday dataset can
# be short by thousands of bars (a whole 1m session is 390). The payload carries the
# first this many, oldest first, plus the true total, so the panel can say "+N more"
# without shipping an unbounded list on every dashboard reload.
_MAX_MISSING_BARS = 500

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


def bar_level_bound(settings: Settings, now: Optional[datetime] = None) -> pd.Timestamp:
    """The newest bar that has FINISHED FORMING, as a canonical stamp.

    This is the correct bound for anything measured in BARS, and the reason it exists
    rather than reusing ``eligible_until_date``: that answers in DAYS, and before the
    close its answer is *yesterday*. Comparing bars against yesterday's date makes
    today's already-closed bars invisible — the dataset would read "fully synced"
    while being stale by however long the session has been open, and the panel could
    offer no way to pull them in. ``last_closed_bar`` is the intraday-aware answer.

    A calendar bar is unaffected: there the newest closed bar IS the eligible date.
    """
    stamp = dataset.last_closed_bar(settings, now)
    if stamp is None:
        # Cannot happen for a readable bar size; falling back to the daily rule keeps
        # a caller from comparing against NaT, which silently matches nothing.
        return dataset.bar_stamp(settings, eligible_until_date(settings, now))
    return dataset.bar_stamp(settings, stamp)


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
    # Two bounds, because there are two questions. Days are complete when they are
    # `eligible`; BARS are complete when they have closed, which mid-session includes
    # hours of today. Using the day bound for bars is what made the panel blind to a
    # dataset that was stale by the whole session so far.
    eligible = eligible_until_date(settings, now)
    until = bar_level_bound(settings, now)
    last_stamp = dataset.bar_stamp(settings, df.index.max())
    have = {ts.date() for ts in df.index}
    missing: List[date] = []
    last_available: Optional[str] = last_date.isoformat()
    # Provider frames this check happened to fetch (the tail probe and the interior
    # reconciliation). They are reused for the bar-level answer instead of being
    # thrown away, so listing missing BARS costs no extra provider request. Each is
    # paired with how far its request reached — see ProviderFrame.
    frames: List[ProviderFrame] = []

    # 1) Tail — bars that have CLOSED and are not in the dataset. Intraday this is what
    #    notices today's completed bars, so it has to ask about bars, not whole days.
    if last_stamp < until:
        try:
            fetched = (
                _auto_fetch_after(settings, client, last_date)
                if auto_check
                else _fetch_after(settings, client, last_date)
            )
        except NoDataError as exc:
            # The provider has nothing in that window. For a probe whose whole job
            # is "is there anything newer?" that is the NORMAL answer — a thin
            # symbol in a quiet stretch, or a session that has not traded yet — so
            # it must not surface as a failure. It used to arrive as an
            # "All providers failed" error and turned a healthy dataset into
            # "Check failed" with the backtest gate shut behind it.
            logger.debug("Delta tail probe found nothing for %s: %s", symbol, exc)
            fetched = None
        if fetched is not None and not fetched.df.empty:
            frames.append(fetched)
            frame = fetched.df
            # Deduplicated: the frame is a list of BARS, so a single missing intraday
            # session contributes one entry per bar for that day. Without the set, a
            # 1m dataset one day behind reported 390 identical dates and the panel
            # drew 390 identical chips.
            pending = {
                ts.date()
                for ts in frame.index
                if last_stamp < dataset.bar_stamp(settings, ts) <= until
                and ts.date() not in have
            }
            missing = sorted(pending)
            last_available = frame.index.max().date().isoformat()
        elif fetched is not None:
            last_available = last_date.isoformat()

    # 2) Interior — holes in the MIDDLE of the stored range. A tail-only check
    #    never notices these once later bars arrive (e.g. a bar lost mid-set).
    #    Absent weekdays are candidates; each is confirmed against the provider
    #    so genuine market holidays (e.g. Labor Day) are never misreported.
    interior = _interior_candidates(have, first_date, last_date)
    if interior:
        span = _provider_span(settings, client, first_date, last_date)
        if span is not None and not span.df.empty:
            frames.append(span)
            prov_dates = {ts.date() for ts in span.df.index}
            holes = sorted(d for d in interior if d in prov_dates)
            if holes:
                missing = sorted(set(missing) | set(holes))

    # 3) Bars — the same gaps at the granularity the strategy actually trades, so a
    #    session that is short by an hour is as visible as a session that is absent,
    #    and today's closed bars are not exempt for being today.
    missing_bars, bar_totals = _missing_bar_rows(settings, df, frames, missing, until, now)
    # A slot the file lacks is not yet proof of a gap: the provider omits an interval
    # nobody traded in, and for a thin symbol that is most of the session. Settling it
    # needs a provider frame over the days in question — the same full-span
    # reconciliation the interior check uses, cached for _RECONCILE_WINDOW_S, so it
    # costs one request per window rather than one per panel refresh.
    if bar_totals[REASON_UNCONFIRMED]:
        span = _provider_span(settings, client, first_date, last_date)
        if span is not None and not span.df.empty:
            frames.append(span)
            missing_bars, bar_totals = _missing_bar_rows(
                settings, df, frames, missing, until, now
            )
    # What still BLOCKS a run: bars the provider has, plus bars nothing has answered
    # for yet. Bars nobody traded are counted separately — worth seeing, because they
    # are real absences an operator should know about, but there is nothing to fetch
    # and nothing to wait for, so they must not hold a backtest hostage.
    blocking_bars = bar_totals[REASON_FETCHABLE] + bar_totals[REASON_UNCONFIRMED]
    listed_blocking = sum(1 for r in missing_bars if r["reason"] != REASON_NO_TRADES)

    return {
        **base,
        "exists": True,
        "last_date": last_date.isoformat(),
        "rows": int(len(df)),
        "recent": _recent_rows(df, interval),
        "missing": [d.isoformat() for d in missing],
        # A day gap and a FETCHABLE bar gap are both "not synced": the panel's Fetch
        # bars button refetches the range either way. An untraded interval is not — it
        # is not something the dataset is missing, it is something the market did not
        # do, and reporting it as "not synced" kept a run disabled forever.
        "synced": not missing and not blocking_bars,
        "missing_bars": missing_bars,
        "missing_bars_total": blocking_bars,
        "missing_bars_truncated": blocking_bars > listed_blocking,
        "no_trades_bars_total": bar_totals[REASON_NO_TRADES],
        "no_trades_bars_truncated": bar_totals[REASON_NO_TRADES]
        > (len(missing_bars) - listed_blocking),
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
    until = bar_level_bound(settings, now)
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
    # ONE place decides whether the provider is needed, and it asks for everything at
    # once: is a closed bar missing (the tail, today's completed bars included), is a
    # session we hold short by bars the provider actually HAS, or is there an absent
    # weekday nobody has confirmed yet? A holiday answers the last one once and is then
    # remembered, so an up-to-date dataset stops touching the provider on every tick.
    #
    # The span is ALWAYS consulted, and that is deliberate: it is the one pass that still
    # looks at the whole stored range, so a bar a provider revises days later is picked
    # up. It is cached for _RECONCILE_WINDOW_S, so it is one range-sized request per
    # window rather than one per tick, and it is also what tells an untraded interval
    # apart from a real hole.
    frames: List[ProviderFrame] = []
    span = _provider_span(settings, client, first_date, last_date)
    if span is not None and not span.df.empty:
        frames = [span]
    _rows, gaps = _missing_bar_rows(settings, df, frames, [], until, now)
    # Only bars a frame HOLDS force the whole stored range to be re-downloaded. An
    # interval no frame has reached yet is a gap but not that kind of gap: it closed
    # inside the newest bar's own day, which the tail window below already covers, so
    # treating it as a reason for a range-sized fetch would put the old per-tick
    # download back. Interior holes nobody has confirmed are caught by need_interior.
    blocking = gaps[REASON_FETCHABLE]
    need_interior = _interior_needs_provider(settings, interior)
    if last_ts >= until and not blocking and not need_interior:
        return dataset_delta_status(settings, client, now)

    # The WHOLE stored range only when something inside it might be fillable or is
    # still unanswered. Otherwise the newest bar's own day is the only thing that can
    # have changed since the last tick, and re-downloading 60 days of 5m bars every
    # tick to learn that nothing new traded is what a blocked loop looks like from the
    # outside. An interval nobody traded is not a reason to refetch: nothing will ever
    # arrive for it.
    fetch_from = (
        first_date if (blocking or need_interior) else _tail_start(settings, last_date)
    )
    fetched = None
    try:
        fetched = client.fetch_historical(
            symbol=symbol,
            start_date=fetch_from.isoformat(),
            end_date=None,
            interval=interval,
            use_cache=False,
        )
    except NoDataError as exc:
        # Nothing at the provider anywhere in the asked-for range: there is nothing to
        # fill and nothing to confirm. Not an error.
        logger.debug("Provider has no bars for %s: %s", symbol, exc)
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
def _interior_needs_provider(settings: Settings, interior: List[date]) -> bool:
    """True when an absent weekday has NOT yet been confirmed as a non-trading day.

    A weekday can be absent because it is a market holiday or because a whole day was
    lost. Telling those apart costs a full-range provider fetch, so the answer is
    cached — and it is consulted here rather than re-fetched. Without this, one holiday
    inside the stored range (e.g. Labor Day) meant a full-range fetch on EVERY sync,
    which for a live loop is one provider request every tick that adds nothing.

    An unconfirmed candidate always returns True, so a genuinely lost day is never
    skipped: only a provider frame that positively has no bar for the day can settle it.
    """
    if not interior:
        return False
    key = (settings.instrument, settings.historical_bar_size)
    with _reconcile_lock:
        hit = _reconcile_cache.get(key)
    if hit is None:
        return True  # never asked -> must ask
    age, frame = hit
    if _time.monotonic() - age >= _RECONCILE_WINDOW_S:
        return True  # stale -> ask again
    if frame is None or frame.empty:
        return True  # the last attempt failed, or returned nothing -> try again
    traded = {ts.date() for ts in frame.index}
    return any(day in traded for day in interior)


def _tail_start(settings: Settings, last_date: date) -> date:
    """The date a "what is new?" fetch should start from.

    Intraday: the last bar's OWN day — the next bar is minutes away in the same
    session, so ``last_date + 1 day`` (a daily-bar assumption) asked about tomorrow,
    which cannot hold anything. The provider answered "no results", the whole check
    was reported as an outage, and today's own closed bars were never fetched.
    Calendar bars keep the next-day rule.

    Clamped to today there as well: a window that starts in the future cannot hold
    anything, so asking about it is guaranteed to come back empty.
    """
    day = (
        last_date
        if dataset.is_intraday(settings.historical_bar_size)
        else last_date + timedelta(days=1)
    )
    return min(day, pd.Timestamp.now(tz=settings.market_timezone).date())


def _fetch_after(settings: Settings, client: OpenBBClient, last_date: date) -> ProviderFrame:
    """Fetch provider bars from :func:`_tail_start` on (no dataset write)."""
    start = _tail_start(settings, last_date).isoformat()
    logger.info("Fetching bars from %s for delta check (%s)", start, settings.instrument)
    return ProviderFrame(
        client.fetch_historical(
            symbol=settings.instrument,
            start_date=start,
            end_date=None,
            interval=settings.historical_bar_size,
            use_cache=False,
        ),
        # Age zero: the request's end is the present moment.
        0.0,
    )


def _auto_fetch_after(
    settings: Settings, client: OpenBBClient, last_date: date
) -> ProviderFrame:
    """Like ``_fetch_after`` but throttled for automatic (page-load) checks.

    Reuses the most recent fetch within ``_AUTO_CHECK_WINDOW_S`` so a page
    reload cannot hammer a rate-limited provider. If the last attempt in the
    window failed, the stored exception is re-raised without another request.

    A reused frame keeps ITS OWN ``asked_until``. That is the whole point of carrying
    one: a fifteen-minute-old fetch is a perfectly good answer about the intervals that
    had closed when it was made, and no answer at all about the ones that closed after
    it — which is exactly how a bar nobody had fetched yet got counted as untraded.
    """
    key = (settings.instrument, settings.historical_bar_size)
    with _auto_check_lock:
        hit = _auto_check_cache.get(key)
        if hit and _time.monotonic() - hit[0] < _AUTO_CHECK_WINDOW_S:
            if hit[2] is not None:
                raise hit[2]
            return ProviderFrame(hit[1], _time.monotonic() - hit[0])
    try:
        fetched = _fetch_after(settings, client, last_date)
    except Exception as exc:
        with _auto_check_lock:
            _auto_check_cache[key] = (_time.monotonic(), None, exc)
        raise
    with _auto_check_lock:
        _auto_check_cache[key] = (_time.monotonic(), fetched.df, None)
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
) -> Optional[ProviderFrame]:
    """Provider frame over [first, last], throttled to _RECONCILE_WINDOW_S.

    Confirms which interior absent weekdays are REAL holes (the provider traded
    them) vs market holidays (the provider has no bar). Returns None when the
    provider can't be reached; the failed attempt is remembered for the window
    so a down/rate-limited provider is not retried on every status check.

    The frame comes back paired with when it was asked, so its silence is read
    against the intervals that had closed by then and no further — a twelve-hour-old
    frame says nothing about an interval that closed five minutes ago.
    """
    key = (settings.instrument, settings.historical_bar_size)
    with _reconcile_lock:
        hit = _reconcile_cache.get(key)
        if hit and _time.monotonic() - hit[0] < _RECONCILE_WINDOW_S:
            if hit[1] is None:
                return None
            return ProviderFrame(hit[1], _time.monotonic() - hit[0])
    end = (last + timedelta(days=1)).isoformat()  # inclusive end for providers
    try:
        frame = client.fetch_historical(
            symbol=settings.instrument,
            start_date=first.isoformat(),
            end_date=end,
            interval=settings.historical_bar_size,
            use_cache=False,
        )
        asked = 0.0
    except NoDataError as exc:
        # No bars at the provider across the stored range at all — there is nothing
        # to confirm an interior day against. Not a failure worth a warning.
        logger.info("No provider bars to reconcile %s: %s", settings.instrument, exc)
        frame, asked = None, None
    except Exception as exc:  # noqa: BLE001 - provider may be down/rate-limited
        logger.warning("Delta interior reconcile failed for %s: %s", settings.instrument, exc)
        frame, asked = None, None
    with _reconcile_lock:
        _reconcile_cache[key] = (_time.monotonic(), frame)
    return None if frame is None else ProviderFrame(frame, asked)


def _remember_provider_span(settings: Settings, frame: pd.DataFrame) -> None:
    """Seed the interior-reconcile cache after an explicit full fetch (sync).

    Stamped with the moment of THIS call, which is when the fetch behind ``frame`` was
    made — the sync fetched it and is handing it over immediately, so age 0 is right.
    """
    key = (settings.instrument, settings.historical_bar_size)
    with _reconcile_lock:
        _reconcile_cache[key] = (_time.monotonic(), frame)


def _remember_provider_span(settings: Settings, frame: pd.DataFrame) -> None:
    """Seed the interior-reconcile cache after an explicit full fetch (sync)."""
    key = (settings.instrument, settings.historical_bar_size)
    with _reconcile_lock:
        _reconcile_cache[key] = (_time.monotonic(), frame)


def _recent_rows(df: pd.DataFrame, interval: str) -> List[dict]:
    """Last ``n`` dataset rows as JSON-ready records (ascending).

    ``datetime`` is the row's full label (``2026-09-17 15:55`` for an intraday bar,
    the date alone for a calendar bar) — the same string the Historical Data table
    shows, so the two panels read alike and a bare date can no longer hide which
    bar of the session was the last one stored.
    """
    rows: List[dict] = []
    for ts, r in df.tail(_RECENT).iterrows():
        rows.append(
            {
                "date": str(ts.date()),
                "datetime": dataset.bar_label(ts, interval),
                "open": round(float(r.open), 4),
                "high": round(float(r.high), 4),
                "low": round(float(r.low), 4),
                "close": round(float(r.close), 4),
                "volume": int(r.volume),
            }
        )
    return rows


# The grid itself lives in ``dataset.session_grid`` — the file's own shape, read off its
# sessions. It is shared with the CHART, which fills the grid's empty slots so a quiet
# stretch is drawn rather than left as a hole, and the two answers have to come from one
# definition or the panel would call a bar missing that the chart is drawing.
#
# Why a bar the grid expects is not in the file. Three genuinely different things, and
# the file alone cannot tell them apart — which is why a thin symbol used to report
# hundreds of "missing" bars that do not exist anywhere, and blocked a backtest on
# them. A PROVIDER FRAME is what settles it: an interval nobody traded in simply has no
# bar at any provider, so it can never be fetched.
#
#   fetchable    a provider frame HAS this bar and the file does not -> a real gap, and
#                "Fetch bars" fills it.
#   no_trades    the provider returned bars ON THAT DAY, and its own series has swept
#                PAST this interval, yet has no bar for it: the interval traded nothing.
#                Nothing to fetch, ever, and nothing to wait for — it must not block
#                anything. Both halves matter: a frame that STOPS before the interval
#                (the newest bar of the session, not published yet) never answered the
#                question, and calling that "no liquidity" wrote off a bar that was
#                merely not fetched yet — the count dropped by one the moment the reader
#                pressed Fetch.
#   unconfirmed  no frame covers that day, or none has reached that interval yet, so
#                nothing has answered the question. Counted as a gap until a frame
#                settles it: the cheap direction to be wrong in.
REASON_FETCHABLE = "fetchable"
REASON_NO_TRADES = "no_trades"
REASON_UNCONFIRMED = "unconfirmed"


class ProviderFrame(NamedTuple):
    """A frame from the provider, together with the age of the request behind it.

    Silence needs a timestamp to mean anything. A request asks for bars over
    ``[start, end]`` and the provider answers with the ones it has; the intervals the
    frame omits are untraded ONLY where the request reached past them. Drop that second
    half and a CACHED frame answers questions about intervals that closed after it was
    fetched — the tail probe is reused for ``_AUTO_CHECK_WINDOW_S`` (15 minutes) and the
    span for ``_RECONCILE_WINDOW_S`` (12 hours), so a bar nobody had tried to fetch yet
    came back labelled "no liquidity" and the count fell by one the moment the reader
    pressed Fetch, precisely because that press was the first attempt.

    The age is in seconds, not an absolute moment, so the reader resolves it against the
    same clock it used for every other bound (the tick's clock, or a test's).
    """

    df: pd.DataFrame
    age_s: float = 0.0


def _missing_bar_rows(
    settings: Settings,
    df: pd.DataFrame,
    frames: List["ProviderFrame"],
    absent_days: List[date],
    until: pd.Timestamp,
    now: Optional[datetime] = None,
) -> tuple:
    """One row per completed bar that is absent, oldest first.

    Every row carries a ``reason`` saying what kind of absence it is, and those are not
    interchangeable — see ``REASON_*``:

    * ``fetchable`` — a provider frame returned this bar and the dataset lacks it.
      Certain, and fillable.
    * ``no_trades`` — the provider traded that day, its own request reached past this
      interval, and it returned no bar for it, so nobody traded in it. A thin symbol is
      mostly made of these.
    * ``unconfirmed`` — a normal slot of a session we hold bars for, and no frame has
      said either way: either no frame covers that day, or none was asked as far as this
      interval's end. Inferred from the file's own shape, so a genuinely short session
      (a half-day close) is indistinguishable from a gap here.

    ``until`` is the newest bar that has CLOSED, as a canonical stamp — a bar bound,
    not a date. Anything after it is still forming or in the future and is never
    reported, which is what lets today's already-closed bars be listed without
    claiming the rest of the session is missing.

    Nothing here calls the provider: it uses the frames the day-level check already
    fetched (the tail probe and the interior reconciliation) plus the stored grid.

    Returns ``(rows, totals)``: ``rows`` is the display list, capped at
    ``_MAX_MISSING_BARS`` with the blocking rows always kept, and ``totals`` counts
    each reason over the WHOLE set.
    """
    interval = settings.historical_bar_size
    tz = ZoneInfo(settings.market_timezone)
    intraday = dataset.is_intraday(interval)
    size = pd.Timedelta(minutes=dataset.interval_minutes(interval) or 60)

    have = {dataset.bar_stamp(settings, ts) for ts in df.index}
    absent = set(absent_days)
    present: dict = {}
    for ts in df.index:
        t = pd.Timestamp(ts)
        present.setdefault(t.date(), set()).add(t.time())

    found: dict = {}

    def add(local: pd.Timestamp, reason: str) -> None:
        """Record one missing bar.

        ``local`` is the bar's own stamp in the form the file stores: naive
        market-local for an intraday bar, a plain date for a calendar bar. Keeping
        it local (rather than converting a UTC instant) is what stops a daily bar
        from sliding to the previous day in a timezone behind UTC.
        """
        stamp = pd.Timestamp(local)
        if dataset.bar_stamp(settings, stamp) > until:
            return  # still forming, or in the future — never "missing"
        found.setdefault(
            dataset.bar_stamp(settings, stamp),
            {
                "date": str(stamp.date()),
                "time": stamp.strftime("%H:%M") if intraday else None,
                "datetime": dataset.bar_label(stamp, interval),
                "reason": reason,
            },
        )

    # Days a provider frame actually traded, and the age of the FRESHEST request behind
    # them. A frame that carries bars for a day watched that whole session, so a grid
    # slot it does not contain is a slot nobody traded in — but only where a request
    # actually reached past that slot. The newest closed interval, which nothing has
    # asked about yet, is not written off just because a fifteen-minute-old frame is
    # silent about it.
    traded_days: set = set()
    freshest = None
    for probe in frames:
        if probe is None or probe.df is None or probe.df.empty:
            continue
        if freshest is None or probe.age_s < freshest:
            freshest = probe.age_s
        for ts in probe.df.index:
            raw = pd.Timestamp(ts)
            traded_days.add((raw.tz_convert(tz) if raw.tz is not None else raw).date())
    # "As far as the provider has been asked", on the caller's clock.
    asked_until = (
        None
        if freshest is None
        else dataset.bar_stamp(settings, now or datetime.now(tz))
        - pd.Timedelta(seconds=freshest)
    )

    # 1) Bars a provider frame holds and the dataset does not.
    for probe in frames:
        if probe is None or probe.df is None or probe.df.empty:
            continue
        for ts in probe.df.index:
            if dataset.bar_stamp(settings, ts) not in have:
                # A provider frame is UTC-aware (``_canonical``); a stub or a local
                # frame is naive. Only the aware one needs converting.
                raw = pd.Timestamp(ts)
                add(raw.tz_convert(tz).tz_localize(None) if raw.tz is not None else raw,
                    REASON_FETCHABLE)

    # 2) Slots of the stored grid that a day we hold bars for is missing, and the
    #    full grid of a day the provider confirmed it traded but we hold nothing of.
    #    Only days we KNOW traded take part — a weekday the provider never traded
    #    (a holiday) is not in either set, or every holiday would be reported as a
    #    session's worth of missing bars.
    if intraday:
        grid = dataset.session_grid(df, interval)
        close_at = session.window(settings)[1]
        for day in sorted(set(present) | absent):
            times = present.get(day, set())
            if day in absent and times:
                continue  # already covered by the provider frame above
            # A session's LAST bar is cut short by the close, so an interval reaches as
            # far as its own end or the close, whichever comes first.
            close_stamp = pd.Timestamp(datetime.combine(day, close_at))
            for slot in grid:
                if slot in times:
                    continue
                stamp = pd.Timestamp(datetime.combine(day, slot))
                # "Nobody traded in it" is only provable where a request actually
                # REACHED past the interval's end. A frame fetched before then (the
                # caches make that the common case) has not answered the question, and
                # calling that "no liquidity" quietly wrote off a bar that was merely
                # not fetched yet. It stays a gap, so the panel still asks for it.
                reached = (
                    asked_until is not None
                    and dataset.bar_stamp(settings, min(stamp + size, close_stamp))
                    <= asked_until
                )
                add(
                    stamp,
                    REASON_NO_TRADES
                    if day in traded_days and reached
                    else REASON_UNCONFIRMED,
                )
    else:
        # A calendar bar IS its session: an absent day is one absent bar, and the
        # day-level check already confirmed the provider traded it.
        for day in sorted(absent):
            add(pd.Timestamp(day), REASON_FETCHABLE)

    ordered = [found[key] for key in sorted(found)]
    blocking = [r for r in ordered if r["reason"] != REASON_NO_TRADES]
    untraded = [r for r in ordered if r["reason"] == REASON_NO_TRADES]
    # The blocking rows are never truncated away: they are the ones that need an
    # answer, and a long tail of untraded intervals must not push them off the list.
    kept = blocking[:_MAX_MISSING_BARS]
    rows = kept + untraded[: max(0, _MAX_MISSING_BARS - len(kept))]
    rows.sort(key=lambda r: r["datetime"])
    totals = {
        REASON_FETCHABLE: sum(1 for r in ordered if r["reason"] == REASON_FETCHABLE),
        REASON_NO_TRADES: len(untraded),
        REASON_UNCONFIRMED: sum(
            1 for r in ordered if r["reason"] == REASON_UNCONFIRMED
        ),
    }
    return rows, totals

    rows = [found[k] for k in sorted(found)]
    return rows[:_MAX_MISSING_BARS], len(rows)
