"""The chart's bars: the session grid, and the slots a provider published nothing for.

A provider omits the bar nobody traded in, so a thin instrument's file is not a grid, and
read straight a chart draws a break where the market was merely quiet. `fill_session_gaps`
gives every slot of the session a bar. These tests pin both halves of that: the bar that
appears, and the file the engine still reads unchanged.
"""

from datetime import date

import pandas as pd

from src.config.settings import Settings
from src.data.dataset import (
    fill_session_gaps,
    load_dataset,
    save_dataset,
    session_grid,
)


def _settings(tmp_path, interval="5m") -> Settings:
    return Settings(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size=interval,
        market_timezone="America/New_York",
    )


def _frame(stamps) -> pd.DataFrame:
    """OHLCV on explicit stamps; each bar's close is its own marker, so a filled bar's
    price can be traced to the bar it was copied from."""
    idx = pd.DatetimeIndex([pd.Timestamp(s) for s in stamps])
    n = len(idx)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


#: Two sessions of five-minute bars, the Friday short of 09:40 (the grid knows it because
#: Thursday has it), so the file holds seven bars where the session has eight slots.
_TWO_SESSIONS = [
    "2024-01-04 09:30", "2024-01-04 09:35", "2024-01-04 09:40", "2024-01-04 09:45",
    "2024-01-05 09:30", "2024-01-05 09:35", "2024-01-05 09:45",
]


def test_a_slot_nobody_traded_in_gets_a_bar(tmp_path):
    """The gap is drawn, as a bar with no trade in it."""
    st = _settings(tmp_path)
    out = fill_session_gaps(st, _frame(_TWO_SESSIONS))

    assert len(out) == len(_TWO_SESSIONS) + 1
    filled = out.loc[pd.Timestamp("2024-01-05 09:40")]
    assert set(filled[["open", "high", "low", "close"]]) == {filled["close"]}
    assert filled["volume"] == 0


def test_the_filled_bar_carries_the_price_last_seen(tmp_path):
    """Not a guess and not an interpolation: the price the market was left at."""
    st = _settings(tmp_path)
    before = _frame(_TWO_SESSIONS)
    out = fill_session_gaps(st, before)

    assert out.loc[pd.Timestamp("2024-01-05 09:40"), "open"] == before.loc[
        pd.Timestamp("2024-01-05 09:35"), "close"
    ]


def test_the_newest_bar_is_never_chased_with_the_rest_of_the_session(tmp_path):
    """Slots after the last stored bar are not a gap — they are the future, and a session
    still forming looks exactly like this every trading day."""
    st = _settings(tmp_path)
    df = _frame(_TWO_SESSIONS + ["2024-01-08 09:30", "2024-01-08 09:35"])
    out = fill_session_gaps(st, df)

    assert out.index.max() == pd.Timestamp("2024-01-08 09:35")
    assert len(out) == len(df)


def test_a_day_the_file_has_no_bars_for_is_not_invented(tmp_path):
    """Monday 01-08 is absent, with bars either side of it. The grid may know its times,
    but there is no session to fill: nothing traded that day, and that is not a bar."""
    st = _settings(tmp_path)
    out = fill_session_gaps(st, _frame(_TWO_SESSIONS + ["2024-01-09 09:30"]))

    assert not any(pd.Timestamp(ts).date() == date(2024, 1, 8) for ts in out.index)


def test_the_slots_come_from_the_file_not_from_a_session_open(tmp_path):
    """The phase is the data's own. A grid stepped from the session open would invent
    slots no provider serves, and would never match the bars that are there."""
    st = _settings(tmp_path)
    out = fill_session_gaps(
        st,
        _frame(["2024-01-04 09:04", "2024-01-04 09:06", "2024-01-04 09:08",
                "2024-01-05 09:04", "2024-01-05 09:08"]),
    )

    assert pd.Timestamp("2024-01-05 09:06") in out.index   # the slot the file has
    assert pd.Timestamp("2024-01-05 09:05") not in out.index  # a :30-stepped grid would


def test_a_calendar_dataset_is_untouched(tmp_path):
    """A daily bar IS its session, so the day-level answer is already the bar-level one —
    and a weekday the provider never traded (a holiday) must not become a bar."""
    st = _settings(tmp_path, interval="1d")
    df = _frame(["2024-01-02", "2024-01-03", "2024-01-05"])

    assert fill_session_gaps(st, df).equals(df)


def test_a_complete_dataset_gains_nothing(tmp_path):
    """The rule has to be silent when there is nothing to fill, or every chart read
    would rewrite a series the engine and the chart agree on."""
    st = _settings(tmp_path)
    df = _frame(["2024-01-04 09:30", "2024-01-04 09:35", "2024-01-05 09:30", "2024-01-05 09:35"])

    assert fill_session_gaps(st, df).equals(df)


def test_the_file_is_left_as_the_provider_published_it(tmp_path):
    """Display only: the engine reads the stored bars, so the chart and the strategy still
    agree on every bar they share (and the delta panel still lists the empty slots)."""
    st = _settings(tmp_path)
    save_dataset(st, _frame(_TWO_SESSIONS), "AAPL", "5m")

    stored = load_dataset(st, "AAPL", "5m")
    assert len(fill_session_gaps(st, stored)) == len(stored) + 1
    assert len(load_dataset(st, "AAPL", "5m")) == len(stored)


def test_the_grid_is_read_off_the_sessions(tmp_path):
    """One definition, two readers: the delta panel calls a bar missing and the chart
    draws a bar for it, and they have to be talking about the same slots."""
    st = _settings(tmp_path)
    df = _frame(_TWO_SESSIONS)

    assert pd.Timestamp("09:40").time() in session_grid(df, "5m")
    assert session_grid(df, "1d") == []  # a calendar bar size has no times of day
