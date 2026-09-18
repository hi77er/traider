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
from src.data.openbb_client import NoDataError, OpenBBError

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
# The provider probe: which window it asks for, and what an empty answer means
# ---------------------------------------------------------------------------
class ProbeClient:
    """Records the window asked for and answers (or fails) however the test says."""

    def __init__(self, frame, exc=None):
        self.frame = frame
        self.exc = exc
        self.calls = []

    def fetch_historical(self, symbol, start_date, end_date=None, interval="5m",
                         use_cache=None):
        self.calls.append(start_date)
        if self.exc is not None:
            raise self.exc
        return self.frame


def _frame_at(stamps):
    """An OHLCV frame on explicit timestamps (intraday), canonical schema."""
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


def _intraday_behind(tmp_path):
    """A 5m dataset whose newest bar is 10:50 on a Friday, with 11:00 "now".

    The newest CLOSED bar is then 10:55, so the tail probe has something to look
    for — the state a live session spends the whole day in.
    """
    st = Settings(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="5m",
        market_timezone="America/New_York",
    )
    df = _frame_at(["2024-01-05 10:45", "2024-01-05 10:50"])
    save_dataset(st, df, "AAPL", "5m")
    return st, df, datetime(2024, 1, 5, 11, 0, tzinfo=ET)


def test_the_tail_probe_asks_about_today_not_tomorrow(tmp_path):
    """The window starts on the last bar's OWN day.

    ``last_date + 1 day`` is a daily-bar assumption: a 5m dataset whose newest bar is
    today asked the provider about TOMORROW, which cannot hold anything. The provider
    answered "no results", OpenBB raised it as though every provider had failed, and
    the panel reported "Check failed" — while the bars that HAD closed went
    unfetched for the rest of the session.
    """
    st, df, now = _intraday_behind(tmp_path)
    client = ProbeClient(df)

    dataset_delta_status(st, client, now)

    assert client.calls == ["2024-01-05"], "asked about today, not tomorrow"


def test_a_tail_probe_with_nothing_new_is_not_an_error(tmp_path):
    """An empty answer means "nothing new" — the healthy case, not a failure."""
    st, df, now = _intraday_behind(tmp_path)
    client = ProbeClient(df, exc=NoDataError("No 5m bars for AAPL"))

    status = dataset_delta_status(st, client, now)

    assert status["error"] is None
    assert status["missing"] == []
    assert status["synced"] is True


def test_a_real_provider_failure_still_reaches_the_caller(tmp_path):
    """...so the tolerance above cannot swallow a genuine outage."""
    st, df, now = _intraday_behind(tmp_path)
    client = ProbeClient(df, exc=OpenBBError("YFRateLimitError: Too Many Requests"))

    with pytest.raises(OpenBBError) as err:
        dataset_delta_status(st, client, now)

    assert "YFRateLimitError" in str(err.value)


def test_sync_tolerates_a_provider_with_nothing_at_all(tmp_path):
    """A full-range fetch that comes back empty writes nothing and raises nothing."""
    st, df, now = _intraday_behind(tmp_path)
    client = ProbeClient(df, exc=NoDataError("No 5m bars for AAPL"))

    status = sync_missing_days(st, client, now)

    assert status["rows"] == len(df)


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
        lambda settings, client, last_date: calls.append(last_date)
        or delta_mod.ProviderFrame(_frame([date(2024, 1, 5), date(2024, 1, 8)])),
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


# ---------------------------------------------------------------------------
# the rows themselves: a time of day per bar
# ---------------------------------------------------------------------------
def test_recent_rows_carry_the_time_of_day(tmp_path):
    """The panel shows date AND time, like the Historical Data table."""
    st = _seed_hourly(tmp_path)
    s = dataset_delta_status(st, IntradayClient([]), datetime(2024, 1, 4, 16, 5, tzinfo=ET))
    assert [r["datetime"] for r in s["recent"]][-1] == "2024-01-04 15:30"
    assert [r["date"] for r in s["recent"]][-1] == "2024-01-04"


def test_daily_recent_rows_are_date_only(tmp_path):
    """A calendar bar has no time of day, so it does not pretend to have one."""
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    s = dataset_delta_status(st, FakeClient([]), datetime(2024, 1, 4, 17, 0, tzinfo=ET))
    assert [r["datetime"] for r in s["recent"]][-1] == "2024-01-04"


# ---------------------------------------------------------------------------
# missing BARS, not just missing days
# ---------------------------------------------------------------------------
def _drop_bars(tmp_path, stamps):
    """Rewrite the stored hourly file without ``stamps`` (a lost bar, not a lost day)."""
    path = tmp_path / "AAPL_1h.parquet"
    stored = pd.read_parquet(str(path))
    kept = stored.drop(index=[pd.Timestamp(s) for s in stamps])
    kept.to_parquet(path)
    return kept


def test_a_session_short_of_bars_is_reported_bar_by_bar(tmp_path):
    """One hour lost out of an otherwise present session, with NO missing day.

    This is the case the day-level check cannot see: the day is there, so nothing is
    "missing" by date, yet a bar the strategy trades on is gone. It must appear as
    that bar, at that time.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-05")), "AAPL", "1h")
    _drop_bars(tmp_path, ["2024-01-04 12:30:00", "2024-01-05 14:30:00"])

    client = IntradayClient([])
    s = dataset_delta_status(st, client, datetime(2024, 1, 5, 16, 5, tzinfo=ET))

    assert s["missing"] == []            # no whole day is absent
    assert s["synced"] is False          # and yet the dataset is incomplete
    assert s["missing_bars_total"] == 2
    assert [(b["date"], b["time"]) for b in s["missing_bars"]] == [
        ("2024-01-04", "12:30"),
        ("2024-01-05", "14:30"),
    ]
    assert all(b["datetime"].endswith(b["time"]) for b in s["missing_bars"])
    assert client.calls == [], "the stored grid answers this without the provider"


def test_a_complete_intraday_dataset_reports_no_missing_bars(tmp_path):
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-05")), "AAPL", "1h")
    s = dataset_delta_status(st, IntradayClient([]), datetime(2024, 1, 5, 16, 5, tzinfo=ET))
    assert s["missing_bars"] == []
    assert s["missing_bars_total"] == 0
    assert s["synced"] is True


def test_a_missing_intraday_day_lists_every_bar_of_its_session(tmp_path):
    """A whole absent session is not one line — it is a bar for every slot."""
    st = _seed_hourly(tmp_path, day="2024-01-04")
    client = IntradayClient(_hourly_session("2024-01-05"))
    s = dataset_delta_status(st, client, datetime(2024, 1, 5, 16, 5, tzinfo=ET))

    assert s["missing"] == ["2024-01-05"]
    assert s["missing_bars_total"] == 7
    assert [b["time"] for b in s["missing_bars"]] == [
        "09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30",
    ]
    assert {b["date"] for b in s["missing_bars"]} == {"2024-01-05"}
    # The provider frame supplied these, so they are not guesses.
    assert {b["reason"] for b in s["missing_bars"]} == {"fetchable"}


def test_a_missing_daily_bar_is_one_bar_carrying_its_date(tmp_path):
    """For a calendar bar size a bar IS its session, so the two lists agree."""
    st = _seed(tmp_path, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    client = FakeClient([date(2024, 1, 5), date(2024, 1, 8)])
    s = dataset_delta_status(st, client, datetime(2024, 1, 8, 17, 0, tzinfo=ET))

    assert s["missing"] == ["2024-01-05", "2024-01-08"]
    assert s["missing_bars_total"] == 2
    assert [(b["date"], b["time"], b["datetime"]) for b in s["missing_bars"]] == [
        ("2024-01-05", None, "2024-01-05"),
        ("2024-01-08", None, "2024-01-08"),
    ]


def test_the_missing_bar_list_is_capped_but_counted_in_full(tmp_path, monkeypatch):
    """An intraday hole can be thousands of bars; the payload says so honestly."""
    from src.data import delta as delta_mod

    monkeypatch.setattr(delta_mod, "_MAX_MISSING_BARS", 3)
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-05")), "AAPL", "1h")
    _drop_bars(tmp_path, _hourly_session("2024-01-05")[2:])  # 5 of the 7 bars gone

    s = dataset_delta_status(st, IntradayClient([]), datetime(2024, 1, 5, 16, 5, tzinfo=ET))
    assert s["missing_bars_total"] == 5
    assert len(s["missing_bars"]) == 3
    assert s["missing_bars_truncated"] is True


def test_a_forming_session_is_never_reported_as_missing_bars(tmp_path):
    """Mid-session, the rest of today is not missing — it has not happened yet.

    The dataset holds 09:30 and 10:30 of a session still in progress at 10:40. The
    remaining five slots are in the FUTURE, and reporting them would make a healthy
    file look broken for most of every trading day.
    """
    st = _seed_hourly(tmp_path, day="2024-01-05")
    save_dataset(st, _frame([]), "AAPL", "1h")
    # Keep only what had closed by 10:40.
    _drop_bars(tmp_path, _hourly_session("2024-01-05")[2:])

    s = dataset_delta_status(
        st, IntradayClient([]), datetime(2024, 1, 5, 10, 40, tzinfo=ET)
    )
    assert s["missing_bars_total"] == 0
    assert s["missing_bars"] == []
    assert s["synced"] is True


def test_a_bar_lost_from_every_session_in_the_same_slot_is_still_seen(tmp_path):
    """The grid is not "the busiest day": two sessions short the same slot still show.

    Reading the grid off the single most complete session would forgive this — once
    every day lacks 12:30 there is no longer any day that has it.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-05")), "AAPL", "1h")
    _drop_bars(tmp_path, ["2024-01-04 12:30:00", "2024-01-05 12:30:00"])

    s = dataset_delta_status(st, IntradayClient([]), datetime(2024, 1, 5, 16, 5, tzinfo=ET))
    # 12:30 is gone from every session, so the grid cannot know it was ever a slot;
    # what it CAN still do is not fall over. Asserted so the limitation is explicit.
    assert s["missing_bars_total"] == 0


# ---------------------------------------------------------------------------
# TODAY is not exempt: bars that have closed are reported, even mid-session
# ---------------------------------------------------------------------------
def test_mid_session_todays_closed_bars_are_reported_as_missing(tmp_path):
    """The dataset is stale by the whole session so far, and the panel must say so.

    The bound here used to be a DATE, and before the close a date's answer is
    YESTERDAY — so today's completed bars could not be reported at all and the panel
    read "fully synced" for a file that was hours behind, with no button to fix it.
    """
    st = _seed_hourly(tmp_path, day="2024-01-05")            # Friday, all 7 bars
    client = IntradayClient(_hourly_session("2024-01-08"))   # Monday, the session so far
    now = datetime(2024, 1, 8, 11, 40, tzinfo=ET)            # 09:30 and 10:30 have closed

    s = dataset_delta_status(st, client, now)

    assert s["synced"] is False
    assert s["missing"] == ["2024-01-08"]
    assert [(b["date"], b["time"], b["reason"]) for b in s["missing_bars"]] == [
        ("2024-01-08", "09:30", "fetchable"),
        ("2024-01-08", "10:30", "fetchable"),
    ]


def test_mid_session_the_forming_bar_of_today_is_not_missing(tmp_path):
    """11:30 is still forming at 11:40 and must never be listed."""
    st = _seed_hourly(tmp_path, day="2024-01-05")
    client = IntradayClient(_hourly_session("2024-01-08"))
    s = dataset_delta_status(st, client, datetime(2024, 1, 8, 11, 40, tzinfo=ET))
    assert "11:30" not in [b["time"] for b in s["missing_bars"]]
    assert max(b["datetime"] for b in s["missing_bars"]) == "2024-01-08 10:30"


def test_today_caught_up_reads_as_synced(tmp_path):
    """Once today's closed bars are stored, the warning goes away."""
    st = _seed_hourly(tmp_path, day="2024-01-05")
    save_dataset(st, _frame(_hourly_session("2024-01-08")[:2]), "AAPL", "1h")  # 09:30, 10:30
    client = IntradayClient(_hourly_session("2024-01-08"))

    s = dataset_delta_status(st, client, datetime(2024, 1, 8, 11, 40, tzinfo=ET))

    assert s["missing"] == []
    assert s["missing_bars"] == []
    assert s["missing_bars_total"] == 0
    assert s["synced"] is True


# ---------------------------------------------------------------------------
# the tick: fetch when anything is missing, and only then
# ---------------------------------------------------------------------------
@pytest.fixture
def reconcile_cache():
    """Keep the module-level provider-span cache out of other tests' way."""
    from src.data import delta as delta_mod

    saved = dict(delta_mod._reconcile_cache)
    delta_mod._reconcile_cache.clear()
    try:
        yield delta_mod
    finally:
        delta_mod._reconcile_cache.clear()
        delta_mod._reconcile_cache.update(saved)


@pytest.fixture
def auto_check_cache():
    """Same, for the tail probe's throttle — a cached frame is a first-class input."""
    from src.data import delta as delta_mod

    saved = dict(delta_mod._auto_check_cache)
    delta_mod._auto_check_cache.clear()
    try:
        yield delta_mod
    finally:
        delta_mod._auto_check_cache.clear()
        delta_mod._auto_check_cache.update(saved)


def _cache_span(delta_mod, settings, frame):
    delta_mod._reconcile_cache[(settings.instrument, settings.historical_bar_size)] = (
        delta_mod._time.monotonic(),
        frame,
    )


def test_a_confirmed_holiday_does_not_force_a_fetch_on_every_tick(tmp_path, reconcile_cache):
    """One holiday inside the range used to mean a full-range fetch every single tick.

    Friday 2024-01-05 is absent between the two sessions we hold. Once the provider has
    said it did not trade that day, there is nothing to fetch — and a loop that refetched
    anyway would burn a request a minute for a bar that will never exist.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-08")), "AAPL", "1h")
    # A holiday looks like a span that simply EXCLUDES the day: the provider returned
    # the sessions either side of it and nothing for 01-05 itself.
    _cache_span(
        reconcile_cache,
        st,
        _frame(_hourly_session("2024-01-04") + _hourly_session("2024-01-08")),
    )

    client = IntradayClient([])
    sync_missing_days(st, client, datetime(2024, 1, 8, 16, 5, tzinfo=ET))

    assert client.calls == [], "a confirmed holiday is not a reason to fetch"


def test_an_empty_cached_span_is_treated_as_unconfirmed(tmp_path, reconcile_cache):
    """An empty frame cannot prove a day did not trade, so it must not silence the fetch.

    The distinction that matters is "the provider answered and its answer excludes this
    day" vs "the provider answered with nothing". Only the first is evidence, and
    treating the second as a holiday would let a lost day hide for 12 hours.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-08")), "AAPL", "1h")
    _cache_span(reconcile_cache, st, _frame([]))

    client = IntradayClient([])
    sync_missing_days(st, client, datetime(2024, 1, 8, 16, 5, tzinfo=ET))

    assert client.calls, "an empty span is not confirmation"


def test_an_unconfirmed_interior_day_still_forces_a_fetch(tmp_path, reconcile_cache):
    """With nothing cached we do not know whether the day was lost, so we must ask."""
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-08")), "AAPL", "1h")

    client = IntradayClient([])
    sync_missing_days(st, client, datetime(2024, 1, 8, 16, 5, tzinfo=ET))

    assert client.calls, "an absent weekday nobody has confirmed must be asked about"


def test_a_day_the_provider_really_traded_keeps_being_fetched(tmp_path, reconcile_cache):
    """The provider HAS bars for 01-05 and we do not, so this is a real hole."""
    st = _seed_hourly(tmp_path, day="2024-01-04")
    save_dataset(st, _frame(_hourly_session("2024-01-08")), "AAPL", "1h")
    _cache_span(reconcile_cache, st, _frame(_hourly_session("2024-01-05")))

    client = IntradayClient(_hourly_session("2024-01-05"))
    sync_missing_days(st, client, datetime(2024, 1, 8, 16, 5, tzinfo=ET))

    assert client.calls, "a lost day must still be fetched"
    stored = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    assert len([t for t in stored.index if t.date() == date(2024, 1, 5)]) == 7


def test_the_tick_fetches_todays_closed_bars(tmp_path, reconcile_cache):
    """End to end: a mid-session sync pulls today in, and only what has closed."""
    st = _seed_hourly(tmp_path, day="2024-01-05")
    client = IntradayClient(_hourly_session("2024-01-08"))

    sync_missing_days(st, client, datetime(2024, 1, 8, 11, 40, tzinfo=ET))

    stored = pd.read_parquet(str(tmp_path / "AAPL_1h.parquet"))
    today = sorted(str(t) for t in stored.index if t.date() == date(2024, 1, 8))
    assert today == ["2024-01-08 09:30:00", "2024-01-08 10:30:00"]
    # and the status agrees afterwards
    s = dataset_delta_status(st, client, datetime(2024, 1, 8, 11, 40, tzinfo=ET))
    assert s["synced"] is True


# ---------------------------------------------------------------------------
# An interval nobody traded is not a gap, and must not block anything
# ---------------------------------------------------------------------------
def test_an_interval_the_provider_did_not_trade_is_not_a_gap(tmp_path, reconcile_cache):
    """A thin symbol's empty intervals are absences the MARKET made, not holes.

    The provider omits an interval nobody traded in, so that bar exists nowhere and no
    fetch can ever produce it. Counting it as "missing" is what kept a tick
    re-downloading the whole range and a backtest disabled — for bars that are not
    missing at all.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")          # the grid: 7 hourly slots
    monday = _hourly_session("2024-01-08")
    save_dataset(st, _frame(monday[:3]), "AAPL", "1h")      # 09:30, 10:30, 11:30 held
    client = IntradayClient(monday[:3])                     # ...and that is all there was

    s = dataset_delta_status(st, client, datetime(2024, 1, 8, 16, 5, tzinfo=ET))

    assert s["missing"] == []                        # no whole session is absent
    assert {b["reason"] for b in s["missing_bars"]} == {"no_trades"}
    assert s["no_trades_bars_total"] == 4            # 12:30..15:30 never traded
    assert s["missing_bars_total"] == 0              # nothing is FETCHABLE
    assert s["synced"] is True, "no bar to fetch must not disable a run"
    # ...and they are still LISTED, so the holes in the chart are accounted for.
    assert [b["time"] for b in s["missing_bars"]] == ["12:30", "13:30", "14:30", "15:30"]


def test_a_bar_the_provider_has_still_blocks(tmp_path, reconcile_cache):
    """The tolerance must not swallow a genuine gap.

    Same shape of session, but this time the provider HAS the bars we lack: those are
    fetchable, they block, and the sync has to go and get them.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")
    monday = _hourly_session("2024-01-08")
    save_dataset(st, _frame(monday[:3]), "AAPL", "1h")
    client = IntradayClient(monday)                          # the provider has all 7

    s = dataset_delta_status(st, client, datetime(2024, 1, 8, 16, 5, tzinfo=ET))

    assert {b["reason"] for b in s["missing_bars"]} == {"fetchable"}
    assert s["missing_bars_total"] == 4
    assert s["no_trades_bars_total"] == 0
    assert s["synced"] is False


def test_the_tick_fetches_only_the_tail_when_the_rest_is_untraded(tmp_path, reconcile_cache):
    """Per-tick cost is the tail, not the stored range.

    Every outstanding bar here is an interval the provider never traded, so there is
    nothing to refetch — but a new bar HAS closed, so the tick must still go and ask.
    Re-downloading the whole range to learn that nothing new traded is what a blocked
    loop looks like from the outside.
    """
    st = _seed_hourly(tmp_path, day="2024-01-04")
    monday = _hourly_session("2024-01-08")
    save_dataset(st, _frame(monday[:3]), "AAPL", "1h")      # held up to 11:30
    _cache_span(reconcile_cache, st, _frame(monday[:3]))     # the provider had no more

    client = IntradayClient(monday[:3])
    sync_missing_days(st, client, datetime(2024, 1, 8, 14, 5, tzinfo=ET))  # 13:30 has closed

    assert [c[1] for c in client.calls] == ["2024-01-08", "2024-01-08"], (
        "today's window both times: the merge, then the re-check"
    )
    assert "2024-01-04" not in [c[1] for c in client.calls], (
        "the stored range must not be re-downloaded for untraded bars"
    )


# ---------------------------------------------------------------------------
# A frame only answers for intervals that had closed when it was ASKED
# ---------------------------------------------------------------------------
def _plant_tail_probe(delta_mod, settings, stamps, age_s):
    """Seed the throttle with a frame as if it had been fetched ``age_s`` ago."""
    delta_mod._auto_check_cache[(settings.instrument, settings.historical_bar_size)] = (
        delta_mod._time.monotonic() - age_s,
        _frame(stamps),
        None,
    )


def _plant_span(delta_mod, settings, stamps, age_s):
    delta_mod._reconcile_cache[(settings.instrument, settings.historical_bar_size)] = (
        delta_mod._time.monotonic() - age_s,
        _frame(stamps),
    )


def test_a_bar_no_request_has_reached_yet_is_not_called_untraded(
    tmp_path, reconcile_cache, auto_check_cache
):
    """"Nobody traded in it" needs a request that got PAST the interval first.

    The tail probe is cached for 15 minutes and the span for 12 hours, so the frames a
    check reads are usually older than the newest closed bar. Reading their silence as
    evidence of no trading put bars under "missing due to no liquidity" that nobody had
    TRIED to fetch yet — and the count fell by one the moment the reader pressed Fetch,
    that press being the first attempt anyone had made.

    Here the difference is only the age of the request: same day, same bars.
    """
    delta_mod = reconcile_cache
    st = _seed_hourly(tmp_path)                                  # Thu 01-04: the grid
    monday = _hourly_session("2024-01-08")
    save_dataset(st, _frame(monday[:1]), "AAPL", "1h")           # Monday: 09:30 only
    now = datetime(2024, 1, 8, 12, 35, tzinfo=ET)                # 11:30 has closed

    # The provider was asked at 12:25, so it has answered about 10:30 (over at 11:30)
    # and nothing at all about 11:30 (over at 12:30).
    _plant_tail_probe(delta_mod, st, monday[:1], age_s=600)
    _plant_span(delta_mod, st, monday[:1], age_s=660)

    stale = dataset_delta_status(st, None, now)
    reasons = {b["time"]: b["reason"] for b in stale["missing_bars"]}
    assert reasons["10:30"] == "no_trades", "inside what the request reached"
    assert reasons["11:30"] == "unconfirmed", "no request has got past it yet"
    assert stale["no_trades_bars_total"] == 1, "only the one nobody traded in"
    assert stale["missing_bars_total"] == 1, "the unanswered one is a gap, and blocks"
    assert stale["synced"] is False

    # One fresh request — the same bars, asked about now — settles it.
    fresh = dataset_delta_status(st, IntradayClient(monday[:1]), now)
    assert {b["time"]: b["reason"] for b in fresh["missing_bars"]} == {
        "10:30": "no_trades",
        "11:30": "no_trades",
    }
    assert fresh["no_trades_bars_total"] == 2
    assert fresh["missing_bars_total"] == 0
    assert fresh["synced"] is True


def test_a_stale_probe_cannot_see_a_bar_the_provider_has(
    tmp_path, reconcile_cache, auto_check_cache
):
    """The worst version of the same bug: the bar was FETCHABLE and read as untraded.

    The provider has held the 11:30 bar all along; the cached frame predates it, so a
    check built on that frame could say neither "fetch me" nor "nobody traded" — and it
    said the wrong one.
    """
    delta_mod = reconcile_cache
    st = _seed_hourly(tmp_path)
    monday = _hourly_session("2024-01-08")
    save_dataset(st, _frame(monday[:1]), "AAPL", "1h")
    now = datetime(2024, 1, 8, 12, 35, tzinfo=ET)

    _plant_tail_probe(delta_mod, st, monday[:2], age_s=600)   # 09:30, 10:30 — no 11:30
    _plant_span(delta_mod, st, monday[:2], age_s=660)

    stale = dataset_delta_status(st, None, now)
    assert {b["time"]: b["reason"] for b in stale["missing_bars"]}["11:30"] == (
        "unconfirmed"
    ), "an unanswered interval must never be reported as untraded"

    # Asked again, the provider's own bar comes back — and it is fetchable, as it was
    # the whole time.
    fresh = dataset_delta_status(st, IntradayClient(monday[:3]), now)
    assert {b["time"]: b["reason"] for b in fresh["missing_bars"]}["11:30"] == "fetchable"
    assert fresh["missing_bars_total"] == 2          # 10:30 and 11:30, both the provider's
    assert fresh["no_trades_bars_total"] == 0
