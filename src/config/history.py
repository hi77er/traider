"""Bar sizes and the historical periods each of them may be fetched for.

ONE answer to "how much history, in bars of what size". The two are not
independent: a window is only useful if the bar size can fill it at a sane
number of bars, and — the harder constraint — the data PROVIDER only serves
intraday bars for a short trailing window, and how short depends on WHICH
provider answers. There is exactly one — ``DATA_PROVIDER`` (yfinance); the
premium fallbacks were removed — so a period is offered as a function of the bar
size and that provider's limits:

| Bar size            | Periods (provider cap)                  |
|---------------------|-----------------------------------------|
| 1m                  | provider's own limit (yfinance: 6 days) |
| 2m                  | 30d, 40d (60d is offered but never fits) |
| 5m, 15m             | 30d, 60d                                |
| 1h, 2h              | 1y, 2y                                  |
| 4h, 8h, 12h         | 1y, 2y (3y dropped: fetched as 1h)       |
| 1d                  | 2y, 3y, 4y, 5y                          |

A period the provider cannot fill is not a slow download — it is a fetch that
QUIETLY returns what the provider happens to have (a "30 days" of 1-minute bars
came back as five trading days) — so such a pair is never offered and is clamped
if it was stored before this rule existed. See ``PROVIDER_MAX_DAYS``.

The period is stored as ONE string with its unit — ``"2y"`` or ``"30d"`` — so the
window cannot be half-specified in two settings that then disagree about which
one wins. Everything that needs to know what a period means (the fetch window,
the settings schema, the dataset summary) reads it from here.

This module is deliberately free of pandas: it names the unit, and the caller
turns it into a date window (``src.data.historical`` does, via ``DateOffset``).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# (code, label) in the order they are offered in the dropdown. Bar codes are the
# provider's own notation and ARE case-significant: ``1m`` is one MINUTE while
# ``1M`` is one MONTH.
BAR_SIZES: List[Tuple[str, str]] = [
    ("1m", "1 minute"),
    ("2m", "2 minutes"),
    ("5m", "5 minutes"),
    ("15m", "15 minutes"),
    ("1h", "1 hour"),
    ("2h", "2 hours"),
    ("4h", "4 hours"),
    ("8h", "8 hours"),
    ("12h", "12 hours"),
    ("1d", "1 day"),
]

# Bar size -> the periods it may be combined with. Coarser bars carry more
# history per bar, so they are allowed longer windows. This is the OFFER; which of
# these actually reach back that far is decided per provider by
# ``allowed_periods`` against ``PROVIDER_MAX_DAYS``.
PERIODS_BY_BAR_SIZE: Dict[str, Tuple[str, ...]] = {
    "1m": ("15d", "30d"),
    "2m": ("30d", "40d", "60d"),
    "5m": ("30d", "60d"),
    "15m": ("30d", "60d"),
    "1h": ("1y", "2y"),
    "2h": ("1y", "2y"),
    "4h": ("1y", "2y", "3y"),
    "8h": ("1y", "2y", "3y"),
    "12h": ("1y", "2y", "3y"),
    "1d": ("2y", "3y", "4y", "5y"),
}

# Bar size -> the finer interval that is actually FETCHED and then resampled up
# (providers do not serve these sizes natively). The provider's limit binds on
# what is fetched, not on what is displayed: a 3-year 4h chart is still a 3-year
# 1-hour request, so it runs into the provider's 1h limit.
#
# ``openbb_client`` reads this table for the same reason, so the two can never
# disagree about what a bar size is made of.
FETCH_INTERVAL: Dict[str, str] = {
    "2h": "1h", "4h": "1h", "8h": "1h", "12h": "1h",
    "3d": "1d", "2W": "1W", "2M": "1M",
}

# Provider -> fetched interval -> the largest window we will ASK that provider
# for, in days. An interval missing from a provider's row has no practical limit
# there (daily bars go back decades); a PROVIDER missing from the table has limits
# this module does not know, and nothing is filtered for it — inventing a cap
# would block history that provider really does serve.
#
# The numbers are MEASURED, not quoted, and they are lower than the documented
# limits on purpose. Both effects below were reproduced against yfinance on
# 2026-09-17 (AAPL; requested window vs the oldest bar that came back):
#
#   * The boundary session is not available: "the last 60 days" means 60 days back
#     from NOW, so a window STARTING exactly on the boundary asks for a session
#     the provider has already dropped. Where that costs ~1 session out of ~40
#     (2–3%), the published limit is kept — a 60-day request for 5-minute bars
#     returns exactly what asking 58 days returns, so nothing is really missing.
#   * Where it costs a large share of the window, the cap goes BELOW the published
#     limit: 1-minute bars are published as 7 days, but the 7th day back is gone,
#     which is 1 of the 5 sessions the window holds (20%) — so 6.
#   * 2-minute bars are the outlier: yfinance stops at 31 sessions (~43 days) no
#     matter how far back the request goes, so a 60-day ask silently came back
#     43 days short (28%). 40 days is a window it serves whole.
#
# Add a row per provider as its limits become known, and re-measure rather than
# reasoning from the docs: a wrong cap either hides a window the provider can fill
# or lets one through that it cannot.
PROVIDER_MAX_DAYS: Dict[str, Dict[str, int]] = {
    "yfinance": {
        "1m": 6,
        "2m": 40,
        "5m": 60, "15m": 60, "30m": 60,
        "60m": 730, "1h": 730, "90m": 730,
    },
}

# The ONE data provider this bot fetches from. The choice was removed on purpose
# (2026-09-18): the fallback chain ended in keyless polygon/fmp, so every genuine
# yfinance message arrived wrapped in their "Missing credential" errors and the
# real cause (usually rate limiting) was the hardest part of the message to find.
# The client requests THIS provider and the limits above describe it, so a
# configured name can no longer disagree with the provider actually used.
DATA_PROVIDER = "yfinance"

_UNIT_DAYS = "d"
_UNIT_YEARS = "y"

# Exported for callers that turn a period into a date window.
UNIT_DAYS = _UNIT_DAYS
UNIT_YEARS = _UNIT_YEARS

# Spellings accepted for each unit, so a hand-edited .env can say "30 days" or
# "2 years" and still mean the same window as "30d"/"2y" (which is what is
# stored and what the dropdown offers).
_DAYS_WORDS = frozenset({"d", "day", "days"})
_YEARS_WORDS = frozenset({"", "y", "year", "years"})

_PERIOD_RE = re.compile(r"^(\d+)\s*([a-zA-Z]*)$")


def period_parts(period: Optional[str]) -> Optional[Tuple[int, str]]:
    """``"30d"`` -> ``(30, "d")``; ``"2 years"`` -> ``(2, "y")``; ``None`` if unusable.

    A bare number is read as YEARS, because that is what this setting used to
    mean (``HISTORICAL_LOOKBACK_YEARS=3``), so a value written before the unit
    existed still means what it always did.
    """
    if period is None:
        return None
    m = _PERIOD_RE.match(str(period).strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit in _YEARS_WORDS:
        return (n, _UNIT_YEARS)
    if unit in _DAYS_WORDS:
        return (n, _UNIT_DAYS)
    return None


# Days in a year for CAP COMPARISONS only. A period is compared before it is
# turned into a date, so it has to be a fixed number: measuring "2y" as 730 days
# keeps "2y" inside a 730-day provider limit whatever the calendar does, while
# ``resolve_history_window`` still moves the window by real calendar years.
_DAYS_PER_YEAR = 365


def period_days(period: Optional[str]) -> Optional[int]:
    """Nominal length of a period in days (``"2y"`` -> 730), or ``None``."""
    parts = period_parts(period)
    if parts is None:
        return None
    n, unit = parts
    return n * _DAYS_PER_YEAR if unit == _UNIT_YEARS else n


def fetch_interval(bar_size: Optional[str]) -> str:
    """The interval actually fetched for ``bar_size`` (itself when native)."""
    text = str(bar_size or "").strip()
    return FETCH_INTERVAL.get(text, text)


def provider_max_days(provider: Optional[str], bar_size: Optional[str]) -> Optional[int]:
    """Days of ``bar_size`` history ``provider`` serves, or ``None`` if unlimited/unknown.

    The lookup goes through ``fetch_interval`` because the limit binds on the
    interval requested from the provider, not on the resampled one the chart shows.
    """
    row = PROVIDER_MAX_DAYS.get(str(provider or "").strip().lower())
    if not row:
        return None
    return row.get(fetch_interval(bar_size))


def _sort_key(period: str) -> Tuple[int, int]:
    """Shortest window first: days before years, then by size."""
    parts = period_parts(period)
    if parts is None:
        return (99, 0)
    n, unit = parts
    return (0 if unit == _UNIT_DAYS else 1, n)


# Every period any bar size may use, shortest first — the fallback list for a bar
# size nobody has described here (a hand-edited value, or one added later).
# Offering everything beats offering nothing: a select with no options silently
# rewrites the operator's stored value to whatever happens to be first.
ALL_PERIODS: Tuple[str, ...] = tuple(
    sorted({p for periods in PERIODS_BY_BAR_SIZE.values() for p in periods}, key=_sort_key)
)


def allowed_periods(bar_size: Optional[str], provider: Optional[str] = None) -> Tuple[str, ...]:
    """The periods ``bar_size`` may be fetched for (all of them if unknown).

    ``provider`` is the data provider that will answer (``DATA_PROVIDER``).
    Periods longer than what it serves are dropped, so the dropdown cannot offer
    a window that would come back short; when EVERY offered period is too long the
    provider's own limit is offered instead, because that is the window it can
    actually fill (yfinance 1-minute bars: 6 days, where the static list says
    15/30).
    """
    bar = str(bar_size or "").strip()
    periods = PERIODS_BY_BAR_SIZE.get(bar, ALL_PERIODS)
    cap = provider_max_days(provider, bar)
    if cap is None:
        return periods
    fits = tuple(p for p in periods if (period_days(p) or 0) <= cap)
    return fits or (f"{cap}d",)


def normalize_period(period: Optional[str]) -> Optional[str]:
    """Canonical spelling of a period (``" 3 "`` -> ``"3y"``), or None."""
    parts = period_parts(period)
    if parts is None:
        return None
    n, unit = parts
    return f"{n}{unit}"


def format_period(period: Optional[str], *, unknown: str = "—") -> str:
    """Human label: ``"30d"`` -> ``"30 days"``, ``"2y"`` -> ``"2 years"``."""
    parts = period_parts(period)
    if parts is None:
        return str(period) if period not in (None, "") else unknown
    n, unit = parts
    word = "day" if unit == _UNIT_DAYS else "year"
    return f"{n} {word}{'' if n == 1 else 's'}"


def periods_for_options(bar_size: Optional[str], provider: Optional[str] = None) -> List[dict]:
    """``[{label, value}]`` for the dropdown, limited to what ``bar_size`` allows."""
    return [
        {"label": format_period(p), "value": p}
        for p in allowed_periods(bar_size, provider)
    ]


def periods_by_bar_size(provider: Optional[str] = None) -> Dict[str, List[dict]]:
    """The whole table as ``{bar_size: [{label, value}]}``.

    Sent to the browser alongside the current bar size's options so the period
    dropdown can be repopulated locally the moment the bar size changes, without
    a round trip — and so the panel can never show a combination the server
    would not accept. Built for the provider in use, because that is what decides
    which periods exist at all.
    """
    return {bar: periods_for_options(bar, provider) for bar in PERIODS_BY_BAR_SIZE}
