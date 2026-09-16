"""Tests for the daily delta data logic (src/data/delta.py).

Uses a fake OpenBB client and a temp Parquet dir — no network access.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config.settings import Settings
from src.data.delta import dataset_delta_status, eligible_until_date, sync_missing_days
from src.data.dataset import save_dataset

ET = ZoneInfo("America/New_York")


class FakeClient:
    """Mimics OpenBBClient.fetch_historical over a fixed set of available dates."""

    def __init__(self, dates):
        self.dates = dates  # list[date]
        self.calls = []

    def fetch_historical(self, symbol, start_date, end_date=None, interval="1d",
                         provider=None, use_cache=None):
        self.calls.append((symbol, start_date, end_date))
        start = pd.Timestamp(start_date)
        picked = [
            d for d in self.dates
            if pd.Timestamp(d) >= start  # provider fetch is inclusive of start_date
            and (end_date is None or pd.Timestamp(d) <= pd.Timestamp(end_date))
        ]
        return _frame(picked)


def _frame(dates):
    if not dates:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
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


def _settings(tmp_path) -> Settings:
    return Settings(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="1d",
    )


def _seed(tmp_path, dates):
    st = _settings(tmp_path)
    save_dataset(st, _frame(dates), "AAPL", "1d")
    return st


# ---------------------------------------------------------------------------
# eligible_until_date
# ---------------------------------------------------------------------------
def test_eligible_after_close_includes_today():
    st = _settings(".")  # dir unused here
    now = datetime(2024, 1, 5, 17, 0, tzinfo=ET)  # Friday after close
    assert eligible_until_date(st, now) == date(2024, 1, 5)


def test_eligible_before_close_excludes_today():
    st = _settings(".")
    now = datetime(2024, 1, 5, 10, 0, tzinfo=ET)  # Friday morning
    assert eligible_until_date(st, now) == date(2024, 1, 4)


def test_eligible_weekend_rolls_back():
    st = _settings(".")
    now = datetime(2024, 1, 6, 12, 0, tzinfo=ET)  # Saturday
    assert eligible_until_date(st, now) == date(2024, 1, 5)


# ---------------------------------------------------------------------------
# status: missing days detected (provider-grounded)
# ---------------------------------------------------------------------------
def test_status_lists_missing_days(tmp_path):
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    client = FakeClient([date(2024, 1, 5), date(2024, 1, 8)])  # Fri + next Mon
    now = datetime(2024, 1, 8, 17, 0, tzinfo=ET)
    s = dataset_delta_status(st, client, now)
    assert s["exists"] is True
    assert s["last_date"] == "2024-01-04"
    assert s["missing"] == ["2024-01-05", "2024-01-08"]
    assert s["synced"] is False
    assert s["recent"][-1]["date"] == "2024-01-04"
    assert len(s["recent"]) == 3


def test_status_synced_no_missing(tmp_path):
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    client = FakeClient([])
    now = datetime(2024, 1, 4, 17, 0, tzinfo=ET)  # dataset already current
    s = dataset_delta_status(st, client, now)
    assert s["missing"] == []
    assert s["synced"] is True
    assert not client.calls  # nothing after last_date -> no fetch


def test_status_midday_does_not_fetch_today(tmp_path):
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    client = FakeClient([date(2024, 1, 5)])  # today's partial available
    now = datetime(2024, 1, 5, 10, 0, tzinfo=ET)  # before close -> today not eligible
    s = dataset_delta_status(st, client, now)
    assert s["synced"] is True
    assert s["missing"] == []
    assert not client.calls  # last_date == eligible -> no fetch


def test_status_no_dataset(tmp_path):
    st = _settings(tmp_path)
    client = FakeClient([date(2024, 1, 5)])
    s = dataset_delta_status(st, client, datetime(2024, 1, 5, 17, 0, tzinfo=ET))
    assert s["exists"] is False
    assert s["synced"] is True


def test_status_lists_interior_hole(tmp_path):
    """A weekday lost in the MIDDLE of the dataset (later bars present on both
    sides) must be reported as missing, not silently skipped by tail logic."""
    from src.data import delta as delta_mod

    st = _seed(
        tmp_path,
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
         date(2024, 1, 9), date(2024, 1, 10)],  # 01-05 + 01-08 missing mid-set
    )
    client = FakeClient([date(2024, 1, 5), date(2024, 1, 8),
                         date(2024, 1, 9), date(2024, 1, 10)])
    delta_mod._reconcile_cache.clear()
    try:
        now = datetime(2024, 1, 10, 17, 0, tzinfo=ET)  # Wed after close
        s = dataset_delta_status(st, client, now)
        assert s["missing"] == ["2024-01-05", "2024-01-08"]
        assert s["synced"] is False
    finally:
        delta_mod._reconcile_cache.clear()


def test_status_ignores_non_trading_day_in_middle(tmp_path):
    """A weekday the provider simply doesn't trade (e.g. a market holiday like
    Labor Day) must NOT be reported as an interior hole."""
    from src.data import delta as delta_mod

    # 2024-01-05 is absent in the middle of the dataset; the provider has no bar
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
                          date(2024, 1, 8), date(2024, 1, 9)])
    client = FakeClient([date(2024, 1, 8), date(2024, 1, 9)])
    delta_mod._reconcile_cache.clear()
    try:
        now = datetime(2024, 1, 9, 17, 0, tzinfo=ET)
        s = dataset_delta_status(st, client, now)
        assert s["missing"] == []
        assert s["synced"] is True
    finally:
        delta_mod._reconcile_cache.clear()


def test_sync_fills_interior_hole(tmp_path):
    """'Fetch missing days' must repair a hole in the middle of the dataset."""
    from src.data import delta as delta_mod

    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
                          date(2024, 1, 9), date(2024, 1, 10)])
    client = FakeClient([date(2024, 1, 5), date(2024, 1, 8),
                         date(2024, 1, 9), date(2024, 1, 10)])
    delta_mod._reconcile_cache.clear()
    try:
        now = datetime(2024, 1, 10, 17, 0, tzinfo=ET)
        s = sync_missing_days(st, client, now)
        assert s["rows"] == 7
        assert s["missing"] == []
        assert s["synced"] is True
        assert s["last_date"] == "2024-01-10"
    finally:
        delta_mod._reconcile_cache.clear()


def test_auto_status_throttles_repeated_fetches(tmp_path, monkeypatch):
    """Live (client-less) status checks must not hit the provider on every
    call — the dashboard reloads often, so without a throttle each reload
    would burn another request and trip yfinance's rate limiter. A second
    call within the window reuses the last fetch; an explicit client (tests,
    post-sync status) always fetches fresh."""
    from src.data import delta as delta_mod

    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    now = datetime(2024, 1, 8, 17, 0, tzinfo=ET)  # Monday after close

    calls = []
    monkeypatch.setattr(
        delta_mod,
        "_fetch_after",
        lambda settings, client, last_date: calls.append(last_date) or _frame(
            [date(2024, 1, 5), date(2024, 1, 8)]
        ),
    )

    s1 = dataset_delta_status(st, None, now)  # auto path -> real fetch
    assert calls == [date(2024, 1, 4)]
    assert s1["missing"] == ["2024-01-05", "2024-01-08"]

    s2 = dataset_delta_status(st, None, now)  # within window -> served from cache
    assert calls == [date(2024, 1, 4)]  # no second provider hit
    assert s2["missing"] == s1["missing"]

    # An injected client bypasses the throttle (fresh fetch, no cache reuse).
    client = FakeClient([date(2024, 1, 5), date(2024, 1, 8)])
    s3 = dataset_delta_status(st, client, now)
    assert len(calls) == 2  # explicit path went to the provider, not the cache
    assert s3["missing"] == ["2024-01-05", "2024-01-08"]

    delta_mod._auto_check_cache.clear()  # don't leak into other tests


# ---------------------------------------------------------------------------
# sync: fetch missing days and merge into the dataset
# ---------------------------------------------------------------------------
def test_sync_fetches_missing_days(tmp_path):
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    client = FakeClient([date(2024, 1, 5)])
    now = datetime(2024, 1, 5, 17, 0, tzinfo=ET)

    s = sync_missing_days(st, client, now)
    assert s["synced"] is True
    assert s["missing"] == []
    assert s["rows"] == 4
    assert s["last_date"] == "2024-01-05"
    assert s["recent"][-1]["date"] == "2024-01-05"


def test_sync_is_idempotent(tmp_path):
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)])
    client = FakeClient([date(2024, 1, 5)])  # same date already present
    now = datetime(2024, 1, 5, 17, 0, tzinfo=ET)
    s = sync_missing_days(st, client, now)
    assert s["synced"] is True
    assert s["rows"] == 4  # no duplicates after merging


# ---------------------------------------------------------------------------
# intraday: the sync used to be daily-only, so an hourly dataset could never
# receive TODAY's bars during the session
# ---------------------------------------------------------------------------
# The bound was ``eligible_until_date``, which returns YESTERDAY before the close. So an
# hourly strategy's newest bar was always yesterday's last one: the first tick after arming
# decided on it, placed a market order at TODAY's price, and then no-opped until the next
# day's sync. One trade a day, at an arbitrary time, from the previous day's signal.
class IntradayClient:
    """A provider that serves whatever (symbol, interval) bars it was given."""

    def __init__(self, stamps):
        self.stamps = [pd.Timestamp(s) for s in stamps]
        self.calls = []

    def fetch_historical(self, symbol, start_date, end_date=None, interval="1h",
                         provider=None, use_cache=None):
        self.calls.append((symbol, start_date, interval))
        start = pd.Timestamp(start_date)
        picked = [s for s in self.stamps if s >= start]
        return _frame(picked)


def _hourly_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
        trading_start_hour="09:30",
        trading_end_hour="16:00",
    )


def _hourly_session(day: str):
    """The grid the provider actually uses: 09:30 through 15:30, hourly."""
    return [f"{day} {hh}:30:00" for hh in ("09", "10", "11", "12", "13", "14", "15")]


def _seed_hourly(tmp_path, day: str = "2024-01-04"):
    st = _hourly_settings(tmp_path)
    save_dataset(st, _frame(_hourly_session(day)), "AAPL", "1h")
    return st


def test_an_intraday_sync_adds_todays_closed_bars(tmp_path):
    """The fix: mid-session, today's FINISHED bars reach the dataset."""
    st = _seed_hourly(tmp_path)                       # last bar: Thu 15:30
    client = IntradayClient(_hourly_session("2024-01-05"))
    now = datetime(2024, 1, 5, 14, 5, tzinfo=ET)      # Friday, 14:05

    sync_missing_days(st, client, now)

    stored = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    today = sorted(str(t) for t in stored.index if t.date() == date(2024, 1, 5))
    # 09:30 and 10:30 and 11:30 closed by 14:05 (11:30 ends at 12:30); 12:30 ends at 13:30;
    # 13:30 is still forming at 14:05, and everything after it is in the future.
    assert today == ["2024-01-05 09:30:00", "2024-01-05 10:30:00", "2024-01-05 11:30:00", "2024-01-05 12:30:00"]


def test_an_intraday_sync_never_writes_the_forming_bar(tmp_path):
    """Deciding on a bar that is still forming is deciding on a price that is still moving.

    Asserted in BOTH directions on purpose: "the forming bar is absent" is satisfied by
    writing nothing at all, which is exactly the bug — so the closed bars must be present
    in the same breath.
    """
    st = _seed_hourly(tmp_path)
    client = IntradayClient(_hourly_session("2024-01-05"))
    now = datetime(2024, 1, 5, 14, 5, tzinfo=ET)

    sync_missing_days(st, client, now)

    stored = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    stamps = {str(t) for t in stored.index}
    assert "2024-01-05 12:30:00" in stamps, "the last bar that had finished forming"
    assert "2024-01-05 13:30:00" not in stamps, "the bar still forming at 14:05"
    assert "2024-01-05 14:30:00" not in stamps, "a bar in the future"


def test_an_intraday_sync_after_the_close_takes_the_whole_session(tmp_path):
    """The last bar of a session is short (15:30->16:00) and complete at the close."""
    st = _seed_hourly(tmp_path)
    client = IntradayClient(_hourly_session("2024-01-05"))
    now = datetime(2024, 1, 5, 16, 5, tzinfo=ET)

    sync_missing_days(st, client, now)

    stored = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    today = sorted(str(t) for t in stored.index if t.date() == date(2024, 1, 5))
    assert len(today) == 7 and today[-1] == "2024-01-05 15:30:00"


def test_an_intraday_sync_is_idempotent_bar_by_bar(tmp_path):
    """Per-BAR membership, not per-day: a day with one bar is not a day that is complete.

    Synced twice as the session progresses, so the second pass has both things to do — add
    the bars that closed in between, and leave the ones already stored alone.
    """
    st = _seed_hourly(tmp_path)
    client = IntradayClient(_hourly_session("2024-01-05"))

    sync_missing_days(st, client, datetime(2024, 1, 5, 12, 5, tzinfo=ET))
    noon = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    sync_missing_days(st, client, datetime(2024, 1, 5, 14, 5, tzinfo=ET))
    later = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))

    today = [t for t in noon.index if t.date() == date(2024, 1, 5)]
    assert len(today) == 2, "09:30 and 10:30 had closed by 12:05"
    today_later = [t for t in later.index if t.date() == date(2024, 1, 5)]
    assert len(today_later) == 4, "and 11:30 and 12:30 by 14:05"
    assert len(later) == len(noon) + 2, "the earlier bars were not duplicated"
    assert list(later.index) == sorted(set(later.index)), "no duplicates at all"


def test_an_intraday_sync_does_not_fetch_when_the_newest_bar_is_already_closed(tmp_path):
    """Nothing can be missing -> do not touch the provider at all."""
    st = _seed_hourly(tmp_path, day="2024-01-05")     # today's session, all closed
    client = IntradayClient(_hourly_session("2024-01-05"))
    now = datetime(2024, 1, 5, 16, 5, tzinfo=ET)

    sync_missing_days(st, client, now)

    assert client.calls == [], "the newest closed bar was already stored"


def test_a_weekend_sync_uses_fridays_close(tmp_path):
    """Saturday has no bars of its own, and Friday's last one is the newest there is."""
    st = _seed_hourly(tmp_path, day="2024-01-04")
    client = IntradayClient(_hourly_session("2024-01-05"))
    now = datetime(2024, 1, 6, 12, 0, tzinfo=ET)      # Saturday

    sync_missing_days(st, client, now)

    stored = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    assert max(t.date() for t in stored.index) == date(2024, 1, 5)
    assert len([t for t in stored.index if t.date() == date(2024, 1, 5)]) == 7
