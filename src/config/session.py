"""The trading session: the hours in which an order may be placed at all.

ONE definition of the window, used by everything that depends on the clock:

* the live poll refuses to fetch outside it (``src/data/live.py``),
* a bar whose own timestamp is outside it produces no decision
  (``src/model/simple_model.py``), so neither the backtest nor a live loop can act on
  a pre-market, after-hours or overnight bar,
* the daily delta reads its close as "today's bar is final" (``src/data/delta.py``).

The window is in ``MARKET_TIMEZONE``, the exchange's own local time, because that is
what the bars are timestamped in — the check is therefore a comparison of clock times
in one zone, with no conversion to the machine's timezone anywhere.

The window is a *bound on decisions*, not on holding: a position opened inside the
window keeps its stop and take-profit orders working until it is closed, and a
position still open at the end of the data is force-closed as usual.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

DEFAULT_START = "09:30"
DEFAULT_END = "16:00"


def parse_hhmm(value: str) -> time:
    """``"09:30"`` -> ``time(9, 30)``. Raises ValueError on anything else.

    ``Settings`` validates the field, so a bad value here means a caller built a
    config object by hand — a raise is the honest answer, because silently falling
    back to a default session would trade hours nobody asked for.
    """
    return time.fromisoformat(str(value).strip())


def window(settings) -> Tuple[time, time]:
    """``(start, end)`` of the session, as local times in ``MARKET_TIMEZONE``."""
    return (
        parse_hhmm(getattr(settings, "trading_start_hour", DEFAULT_START) or DEFAULT_START),
        parse_hhmm(getattr(settings, "trading_end_hour", DEFAULT_END) or DEFAULT_END),
    )


def describe(settings) -> str:
    """Human label for the window, e.g. ``"09:30–16:00 America/New_York"``."""
    start, end = window(settings)
    tz = getattr(settings, "market_timezone", "") or ""
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} {tz}".strip()


def is_open_at(settings, when: Optional[datetime] = None) -> bool:
    """True when ``when`` (default: now) is inside the session on a weekday.

    A window that crosses midnight is handled (unlikely for equities, but a session
    that ends before it starts would otherwise be read as permanently closed).
    """
    tz = ZoneInfo(getattr(settings, "market_timezone", None) or "UTC")
    now = when or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    now = now.astimezone(tz)

    if now.weekday() >= 5:  # Saturday / Sunday
        return False

    start, end = window(settings)
    if start <= end:
        return start <= now.time() <= end
    return now.time() >= start or now.time() <= end


def stamp(settings, when: Optional[datetime] = None) -> str:
    """WHICH session ``when`` belongs to, e.g. ``"2026-09-23 in session"``.

    One session per trading day, identified by the date in the exchange's own timezone and by
    whether the moment is inside the window — so ``"2026-09-23 out of session"`` (before the
    bell, or after it) and ``"2026-09-23 in session"`` are TWO different sessions rather than
    one. That distinction is the whole point of the stamp: the screener's numbers are the last
    completed session's, so a list screened before the bell is not the list the session trades
    on, even though it was screened on the same day.

    It exists so a cached list can say which session it is an answer for. Comparing stamps is
    how a reader — the tick, or the panel — sees that a list belongs to a session that has
    ended instead of trusting a number from it.
    """
    tz = ZoneInfo(getattr(settings, "market_timezone", None) or "UTC")
    now = when or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    now = now.astimezone(tz)
    return f"{now.date().isoformat()} {'in session' if is_open_at(settings, now) else 'out of session'}"


def is_in_session(stamp: Optional[str]) -> bool:
    """Whether a stamp was taken inside the session — the stamp's own words say so.

    Read back rather than stored twice: a document carries ONE session fact, and two copies of a
    fact are two things that can disagree. Used wherever a reader has to be told that the numbers
    in front of them are the last completed session's: a screen taken before the bell returns
    yesterday's close, and the list looks no different for it.
    """
    return str(stamp or "").endswith(" in session")


def last_weekday(day: date) -> date:
    """``day`` itself when it is a weekday, else the Friday before it."""
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day
