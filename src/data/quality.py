"""Is this bar possible? — the sanity check the loop runs before it decides on one.

A decision is made on ONE bar: the newest closed one. If that bar cannot describe a real
market, the decision is about a market that never existed, and every number downstream of it
— the signal, the size, the stop — inherits the mistake. So the bar is checked, and the check
is split in two on purpose:

* :func:`broken_reason` — the bar is **impossible**: a price is missing, zero or negative, the
  high is under the low, or the range does not contain the open and the close. The loop
  refuses the tick. There is nothing to salvage and nothing to explain to a reader afterwards.
* :func:`notes` — the bar is **unusual** but possible: no volume, or no range at all. The tick
  goes ahead and the note travels with the record, because a bar like that is worth a look and
  is not worth skipping a decision over.

That split is the whole design, and the line between the two is deliberately narrow: a bar
either contradicts itself or it does not. Anything softer — "a bigger move than usual", "more
volume than normal" — would need a threshold, and a threshold nobody configured is this module
inventing policy. The loop is not the place for that; a rule like it belongs in the strategy,
where it can be backtested.

Nothing here reads a file, knows a symbol, or cares what a bar is FOR. Both functions take one
row of OHLCV and answer a question about it.
"""

from __future__ import annotations

import math
from typing import Any, List, Mapping, Optional

__all__ = ["broken_reason", "notes"]

#: The four prices a bar cannot be missing and still mean anything. Volume is deliberately
#: NOT one of them — see :func:`notes`.
PRICES = ("open", "high", "low", "close")


def _number(row: Mapping[str, Any], key: str) -> Optional[float]:
    """``row[key]`` as a float, or ``None`` when it is absent, empty or not a number.

    NaN counts as absent rather than as a value: it compares False against everything, so a
    NaN that reached the decision would silently take whichever branch the comparison
    happened to fall through, which is the worst possible behaviour for a check.
    """
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return None
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def broken_reason(row: Mapping[str, Any]) -> str:
    """Why this bar cannot be decided on, or ``""`` when it is possible.

    A sentence rather than a bool: the loop puts it in the tick record, and "the bar is
    broken" without saying HOW sends someone to read the dataset by hand to find out.
    """
    missing = [key for key in PRICES if _number(row, key) is None]
    if missing:
        return (
            f"the bar is missing its {', '.join(missing)} price"
            + ("s" if len(missing) > 1 else "")
            + " — refusing to decide on a half-written bar"
        )

    open_px, high, low, close = (float(row[key]) for key in PRICES)
    if min(open_px, high, low, close) <= 0:
        # A zero or negative price is not a cheap stock, it is a broken field.
        worst = min(open_px, high, low, close)
        return f"the bar has a price of {worst:g} — refusing to decide on it"
    if high < low:
        return (
            f"the bar's high ({high:g}) is below its low ({low:g}) — "
            "refusing to decide on an inverted bar"
        )
    if high < max(open_px, close) or low > min(open_px, close):
        return (
            f"the bar's range ({low:g}–{high:g}) does not contain its open and close "
            f"({open_px:g}/{close:g}) — refusing to decide on an impossible bar"
        )
    return ""


def notes(row: Mapping[str, Any]) -> List[str]:
    """What is unusual about this bar without being impossible. Possibly empty.

    Kept to two facts that need no threshold to state, because a note is read by a person
    deciding whether to trust a line in the log:

    * **no volume.** A bar with no trades in it is usually a feed artifact — the price was
      carried forward from the bar before — and it is also what a market holiday looks like.
      A decision made on it is a decision on a price nothing traded at.
    * **no range.** ``high == low`` means the price did not move at all inside the bar. It is
      possible in a dead market and impossible in most, so it is worth knowing which.
    """
    out: List[str] = []

    volume = _number(row, "volume")
    if volume is None:
        out.append("the bar has no volume")
    elif volume <= 0:
        out.append(f"the bar has a volume of {volume:g}")

    high, low = _number(row, "high"), _number(row, "low")
    if high is not None and low is not None and high == low:
        out.append(f"the bar has no range (high and low are both {high:g})")

    return out
