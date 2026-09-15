"""Bar sizes and the historical periods each of them may be fetched for.

ONE answer to "how much history, in bars of what size". The two are not
independent: a window is only useful if the bar size can fill it at a sane
number of bars, and — the harder constraint — the data PROVIDER only serves
intraday bars for a short trailing window (yfinance: ~7 days of 1-minute bars,
~60 days of 2/5/15/30/60-minute bars, ~730 days of hourly, everything for
daily). A period is therefore offered as a function of the bar size:

| Bar size            | Periods                 |
|---------------------|-------------------------|
| 1m                  | 15d, 30d                |
| 2m, 5m, 15m         | 30d, 60d                |
| 1h, 2h              | 1y, 2y                  |
| 4h, 8h, 12h         | 1y, 2y, 3y              |
| 1d                  | 2y, 3y, 4y, 5y          |

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
# history per bar, so they are allowed longer windows.
PERIODS_BY_BAR_SIZE: Dict[str, Tuple[str, ...]] = {
    "1m": ("15d", "30d"),
    "2m": ("30d", "60d"),
    "5m": ("30d", "60d"),
    "15m": ("30d", "60d"),
    "1h": ("1y", "2y"),
    "2h": ("1y", "2y"),
    "4h": ("1y", "2y", "3y"),
    "8h": ("1y", "2y", "3y"),
    "12h": ("1y", "2y", "3y"),
    "1d": ("2y", "3y", "4y", "5y"),
}

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


def allowed_periods(bar_size: Optional[str]) -> Tuple[str, ...]:
    """The periods ``bar_size`` may be fetched for (all of them if unknown)."""
    return PERIODS_BY_BAR_SIZE.get(str(bar_size or "").strip(), ALL_PERIODS)


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


def periods_for_options(bar_size: Optional[str]) -> List[dict]:
    """``[{label, value}]`` for the dropdown, limited to what ``bar_size`` allows."""
    return [
        {"label": format_period(p), "value": p} for p in allowed_periods(bar_size)
    ]


def periods_by_bar_size() -> Dict[str, List[dict]]:
    """The whole table as ``{bar_size: [{label, value}]}``.

    Sent to the browser alongside the current bar size's options so the period
    dropdown can be repopulated locally the moment the bar size changes, without
    a round trip — and so the panel can never show a combination the server
    would not accept.
    """
    return {bar: periods_for_options(bar) for bar in PERIODS_BY_BAR_SIZE}
