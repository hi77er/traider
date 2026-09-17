"""Is the bar possible, and is it odd? — the two questions, kept apart.

The split is the thing worth testing: an IMPOSSIBLE bar refuses the tick and an UNUSUAL one
does not. A check that collapsed the two would either stop the bot on a quiet afternoon (no
volume is common) or decide silently on a bar whose high is under its low (which cannot
happen, and which every number downstream would inherit).

Everything here is a plain mapping, so these are the cheapest tests in the suite — no dataset,
no broker, no clock.
"""

from __future__ import annotations

import pytest

from src.data.quality import broken_reason, notes


def _bar(**over) -> dict:
    values = {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000.0}
    values.update(over)
    return values


def test_an_ordinary_bar_is_neither_broken_nor_odd() -> None:
    """The baseline. Without it the other tests could all pass against a check that always
    fires."""
    assert broken_reason(_bar()) == ""
    assert notes(_bar()) == []


# --- impossible ------------------------------------------------------------
@pytest.mark.parametrize("missing", ["open", "high", "low", "close"])
def test_a_missing_price_is_a_broken_bar(missing) -> None:
    """Every one of the four, because a check that only looked at ``close`` would pass a bar
    with no low and refuse nothing."""
    reason = broken_reason(_bar(**{missing: None}))

    assert reason and missing in reason, reason
    assert "half-written" in reason, "the message says what is wrong with it"


def test_a_nan_price_counts_as_missing() -> None:
    """NaN compares False against everything, so a NaN that reached a decision would take
    whichever branch the comparison happened to fall through."""
    assert "high" in broken_reason(_bar(high=float("nan")))


def test_a_price_that_is_not_a_number_is_missing() -> None:
    """A blank cell from a CSV is not a price of zero."""
    assert broken_reason(_bar(low=""))
    assert broken_reason(_bar(low="n/a"))


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_non_positive_price_is_a_broken_bar(bad) -> None:
    """Zero is not a cheap stock, it is a broken field — and the divide that follows it is
    not a number either."""
    assert broken_reason(_bar(close=bad))


def test_an_inverted_bar_is_broken() -> None:
    reason = broken_reason(_bar(high=98.0, low=99.0))

    assert "high (98) is below its low (99)" in reason, reason


def test_a_range_that_excludes_its_own_open_or_close_is_broken() -> None:
    """Still impossible, and still not an inverted bar — a bar can be ordered high-low and
    contradict itself anyway."""
    above = broken_reason(_bar(close=103.0))
    below = broken_reason(_bar(open=98.0))

    assert "does not contain" in above and "103" in above, above
    assert "does not contain" in below and "98" in below, below


def test_a_row_missing_its_keys_entirely_does_not_raise() -> None:
    """A half-written line must be refused, not crash the loop that found it."""
    assert broken_reason({}) == broken_reason(_bar(open=None, high=None, low=None, close=None))
    assert broken_reason({"close": "1"}) != ""


def test_string_prices_are_read_rather_than_refused() -> None:
    """The dataset is JSON, so numbers arrive as strings often enough to matter."""
    assert broken_reason(_bar(open="100", high="102", low="99", close="101")) == ""


# --- unusual, and decided on anyway ---------------------------------------
def test_a_bar_with_no_volume_is_odd_and_not_broken() -> None:
    """THE line this module draws. A quiet afternoon is not a broken feed, and refusing here
    would stop the bot on the day nothing was wrong with it."""
    assert broken_reason(_bar(volume=0)) == "", "usable"
    assert notes(_bar(volume=0)) == ["the bar has a volume of 0"]


def test_a_bar_with_no_volume_field_is_odd_and_not_broken() -> None:
    bar = _bar()
    del bar["volume"]

    assert broken_reason(bar) == ""
    assert notes(bar) == ["the bar has no volume"]


def test_a_bar_with_no_range_is_odd_and_not_broken() -> None:
    """``high == low`` is possible in a dead market and impossible in most, which is exactly
    the kind of fact a person reading the log wants and a rule should not act on."""
    flat = _bar(high=100.0, low=100.0, open=100.0, close=100.0)

    assert broken_reason(flat) == ""
    assert notes(flat) == ["the bar has no range (high and low are both 100)"]


def test_more_than_one_oddity_is_reported_as_more_than_one_note() -> None:
    assert len(notes(_bar(volume=0, high=100.0, low=100.0, open=100.0, close=100.0))) == 2
