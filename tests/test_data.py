"""Unit tests for the data layer (OpenBB client, historical, live).

The OpenBB SDK is mocked at the ``client.obb`` level — no network access.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config.settings import Settings
from src.data.dataset import (
    bar_key,
    bar_stamp,
    dataset_path,
    interval_minutes,
    last_closed_bar,
    load_dataset,
    save_dataset,
)
from src.data.historical import fetch_candles
from src.data.live import (
    days_for_bars,
    get_history_window,
    get_latest_candle,
    is_market_open,
    required_bars,
)
from src.data.openbb_client import OHLCV_COLUMNS, OpenBBClient, OpenBBError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeResult:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def to_df(self) -> pd.DataFrame:
        return self._df


class FakePrice:
    """Records provider calls (incl. interval); returns queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.intervals = []

    def historical(self, symbol, start_date=None, end_date=None, interval=None, provider=None):
        self.calls.append(provider)
        self.intervals.append(interval)
        item = self.responses.pop(0) if self.responses else FakeResult(pd.DataFrame())
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResult) else FakeResult(item)


class FakeOBB:
    def __init__(self, price):
        self.equity = type("Eq", (), {"price": price})()


def make_raw_df(rows: int = 3) -> pd.DataFrame:
    """A provider-style frame (uppercase cols, date column) for normalization."""
    dates = pd.date_range("2024-01-01", periods=rows, freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": [100 + i for i in range(rows)],
            "High": [101 + i for i in range(rows)],
            "Low": [99 + i for i in range(rows)],
            "Close": [100.5 + i for i in range(rows)],
            "Volume": [1000 + i for i in range(rows)],
        }
    )


def make_settings(tmp_path) -> Settings:
    return Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=True,
        cache_dir=str(tmp_path),
        market_timezone="America/New_York",
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def test_normalize_uppercase_columns():
    df = OpenBBClient._normalize(make_raw_df())
    assert list(df.columns) == OHLCV_COLUMNS
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None  # naive provider data gets a tz via to_datetime


def test_normalize_missing_columns_raises():
    bad = pd.DataFrame({"open": [1.0], "close": [1.0]})
    with pytest.raises(OpenBBError):
        OpenBBClient._normalize(bad)


def test_normalize_drops_bars_without_a_price():
    """A placeholder bar (open/high but no close) is not a bar — drop it."""
    raw = make_raw_df(3)
    raw.loc[raw.index[-1], "Close"] = float("nan")
    df = OpenBBClient._normalize(raw)
    assert len(df) == 2
    assert not df[["open", "high", "low", "close"]].isna().any().any()


def test_normalize_keeps_bars_without_volume():
    """Volume is optional: a bar can legitimately carry none."""
    raw = make_raw_df(3)
    raw.loc[raw.index[-1], "Volume"] = float("nan")
    assert len(OpenBBClient._normalize(raw)) == 3


def test_trailing_bar_without_a_price_is_recovered(tmp_path):
    """A placeholder FINAL row is re-requested on its own, not silently lost.

    yfinance returns the last row of a multi-day range with a NaN close while a
    request starting on that date returns the settled close — so the newest bar
    is recoverable instead of leaving the dataset permanently a day short.
    """
    settings = Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=False,
        cache_dir=str(tmp_path),
    )
    partial = make_raw_df(3)
    partial.loc[partial.index[-1], "Close"] = float("nan")
    last_date = partial["Date"].iloc[-1]
    settled = pd.DataFrame(
        {
            "Date": [last_date],
            "Open": [500.0],
            "High": [505.0],
            "Low": [499.0],
            "Close": [504.0],
            "Volume": [7.0],
        }
    )
    client = OpenBBClient(settings)
    client._obb = FakeOBB(FakePrice([partial, settled]))

    df = client.fetch_historical("AAPL", "2024-01-01", interval="4h")
    assert len(df) == 3  # the trailing bar was recovered, not dropped
    assert df["close"].notna().all()
    assert df["close"].iloc[-1] == 504.0
    # Same provider asked twice: the wide range, then that single day.
    assert client._obb.equity.price.calls == ["yfinance", "yfinance"]


def test_trailing_bar_is_dropped_when_the_retry_has_no_price(tmp_path):
    """If the day has no settled bar at all, drop it (never store a null)."""
    settings = Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=False,
        cache_dir=str(tmp_path),
    )
    partial = make_raw_df(3)
    partial.loc[partial.index[-1], "Close"] = float("nan")
    client = OpenBBClient(settings)
    client._obb = FakeOBB(FakePrice([partial, partial.tail(1)]))

    df = client.fetch_historical("AAPL", "2024-01-01", interval="4h")
    assert len(df) == 2
    assert df["close"].notna().all()


# ---------------------------------------------------------------------------
# fetch_historical + caching + failover
# ---------------------------------------------------------------------------
def test_fetch_historical_normalizes_and_caches(tmp_path):
    settings = make_settings(tmp_path)
    client = OpenBBClient(settings)
    client._obb = FakeOBB(FakePrice([make_raw_df()]))

    df1 = client.fetch_historical("AAPL", "2024-01-01", "2024-01-10", interval="4h")
    assert not df1.empty
    assert list(df1.columns) == OHLCV_COLUMNS

    # Second call reads from cache — the provider is hit only once.
    df2 = client.fetch_historical("AAPL", "2024-01-01", "2024-01-10", interval="4h")
    assert not df2.empty
    assert client._obb.equity.price.calls.count("yfinance") == 1


def test_fetch_historical_failover(tmp_path):
    settings = Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=False,
        cache_dir=str(tmp_path),
    )
    client = OpenBBClient(settings)
    client._obb = FakeOBB(FakePrice([RuntimeError("primary down"), make_raw_df()]))

    df = client.fetch_historical("AAPL", "2024-01-01", interval="4h")
    assert not df.empty
    assert client._obb.equity.price.calls == ["yfinance", "polygon"]


def test_fetch_historical_all_providers_fail(tmp_path):
    settings = Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=False,
        cache_dir=str(tmp_path),
    )
    client = OpenBBClient(settings)
    client._obb = FakeOBB(
        FakePrice([RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
    )
    with pytest.raises(OpenBBError):
        client.fetch_historical("AAPL", "2024-01-01", interval="4h")


def test_failover_continues_past_an_empty_provider(tmp_path):
    """An empty primary response must not abort the chain (backups still run)."""
    settings = Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=False,
        cache_dir=str(tmp_path),
    )
    client = OpenBBClient(settings)
    client._obb = FakeOBB(FakePrice([pd.DataFrame(), make_raw_df()]))

    df = client.fetch_historical("AAPL", "2024-01-01", interval="4h")
    assert not df.empty
    assert client._obb.equity.price.calls == ["yfinance", "polygon"]
def test_resample_4h_from_1h():
    # 12 x 1h bars starting on a day boundary -> 3 x 4h bars
    dates = pd.date_range("2024-01-01 00:00", periods=12, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100 + i for i in range(12)],
            "high": [101 + i for i in range(12)],
            "low": [99 + i for i in range(12)],
            "close": [100.5 + i for i in range(12)],
            "volume": [1000 + i for i in range(12)],
        },
        index=dates,
    )
    out = OpenBBClient._resample(df, "4h")
    assert len(out) == 3
    assert out.iloc[0]["open"] == 100.0
    assert out.iloc[0]["high"] == 104.0
    assert out.iloc[0]["low"] == 99.0
    assert out.iloc[0]["close"] == 103.5
    assert out.iloc[0]["volume"] == 1000 + 1001 + 1002 + 1003
    assert out.iloc[1]["open"] == 104.0


def test_fetch_historical_resamples_4h(tmp_path):
    settings = Settings(
        openbb_provider="yfinance",
        openbb_backup_providers="yfinance,polygon,fmp",
        data_cache_enabled=False,
        cache_dir=str(tmp_path),
    )
    client = OpenBBClient(settings)
    raw = make_raw_df(12)
    raw["Date"] = pd.date_range("2024-01-01 00:00", periods=12, freq="1h", tz="UTC")
    client._obb = FakeOBB(FakePrice([raw]))

    df = client.fetch_historical("AAPL", "2024-01-01", "2024-01-02", interval="4h")
    assert client._obb.equity.price.intervals == ["1h"]  # fetched at base interval
    assert len(df) == 3


def test_lazy_import_raises_helpful_error(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openbb":
            raise ImportError("No module named 'openbb'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    settings = make_settings(tmp_path)
    client = OpenBBClient(settings)
    with pytest.raises(OpenBBError, match="pip install openbb"):
        _ = client.obb


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
def test_is_market_open_weekday_hours():
    settings = Settings(market_timezone="America/New_York", trading_start_hour="09:30", trading_end_hour="16:00")
    tz = ZoneInfo("America/New_York")
    assert is_market_open(settings, datetime(2024, 1, 2, 10, 0, tzinfo=tz)) is True   # Tue 10:00
    assert is_market_open(settings, datetime(2024, 1, 2, 17, 0, tzinfo=tz)) is False  # Tue 17:00
    assert is_market_open(settings, datetime(2024, 1, 6, 12, 0, tzinfo=tz)) is False  # Sat


class FakeClient:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.called = False

    def fetch_historical(self, symbol, start_date, end_date, interval):
        self.called = True
        return self.df


def test_get_latest_candle_returns_last_bar(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data.live.is_market_open", lambda settings, now=None: True)
    settings = make_settings(tmp_path)
    df = OpenBBClient._normalize(make_raw_df(5))
    client = FakeClient(df)
    result = get_latest_candle(settings, client=client)
    assert client.called is True
    assert len(result) == 1
    assert result.index[-1] == df.index[-1]


def test_get_latest_candle_skips_when_market_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data.live.is_market_open", lambda settings, now=None: False)
    settings = make_settings(tmp_path)
    client = FakeClient(OpenBBClient._normalize(make_raw_df(3)))
    result = get_latest_candle(settings, client=client)
    assert result.empty
    assert client.called is False


# ---------------------------------------------------------------------------
# the bar grid: canonical stamps, and which bar has closed
# ---------------------------------------------------------------------------
def _bars(tmp_path, **kw) -> Settings:
    values = dict(
        _env_file=None,
        historical_data_dir=str(tmp_path),
        market_timezone="America/New_York",
        trading_start_hour="09:30",
        trading_end_hour="16:00",
    )
    values.update(kw)
    return Settings(**values)


def test_interval_minutes_reads_the_bar_size():
    assert interval_minutes("15m") == 15
    assert interval_minutes("1h") == 60
    assert interval_minutes("4h") == 240
    assert interval_minutes("1d") == 1440
    assert interval_minutes("") is None and interval_minutes("nonsense") is None


def test_a_bar_stamp_is_canonical_whatever_the_file_holds(tmp_path):
    """The dataset does not agree with itself: daily comes back UTC-aware, hourly naive.

    Comparing the two, or keying an idempotency check on ``str(ts)``, silently depends on
    which provider wrote the file.
    """
    hourly = _bars(tmp_path, historical_bar_size="1h")
    daily = _bars(tmp_path, historical_bar_size="1d")

    assert (bar_stamp(hourly, "2024-01-05 15:30:00")
            == bar_stamp(hourly, "2024-01-05 15:30:00-05:00")), "naive hourly is exchange-local"
    assert (bar_stamp(daily, "2024-01-05 00:00:00")
            == bar_stamp(daily, "2024-01-05 00:00:00+00:00")), "naive daily is a UTC date"

    # And a daily bar is NOT shifted by the session offset into the previous day.
    assert bar_stamp(daily, "2024-01-05 00:00:00").date().isoformat() == "2024-01-05"


def test_the_bar_key_is_the_same_bar_in_every_representation(tmp_path):
    """The one failure here that costs money: a key that changes shape re-fires a decision."""
    hourly = _bars(tmp_path, historical_bar_size="1h")
    as_written_by_a_naive_provider = bar_key(hourly, "2024-01-05 15:30:00")
    as_written_by_an_aware_one = bar_key(hourly, "2024-01-05 15:30:00-05:00")
    assert as_written_by_a_naive_provider == as_written_by_an_aware_one
    # ...and a different bar is still a different key.
    assert bar_key(hourly, "2024-01-05 14:30:00") != as_written_by_a_naive_provider


@pytest.mark.parametrize(
    "when,expected",
    [
        # An hourly grid on a 09:30 open: the 12:30 bar covers 12:30-13:30, so at 13:00 it
        # is still forming and 11:30 is the newest complete one.
        ("2024-01-05 10:00", "2024-01-04 15:30"),
        ("2024-01-05 11:00", "2024-01-05 09:30"),
        ("2024-01-05 13:00", "2024-01-05 11:30"),
        ("2024-01-05 14:05", "2024-01-05 12:30"),
        # After the close the session's short last bar is complete too.
        ("2024-01-05 16:05", "2024-01-05 15:30"),
        # Before the open, and on a weekend, it is the previous session's last bar.
        ("2024-01-05 08:00", "2024-01-04 15:30"),
        ("2024-01-06 12:00", "2024-01-05 15:30"),
    ],
)
def test_last_closed_bar_on_an_hourly_grid(tmp_path, when, expected):
    """10:00 shows the bug in miniature: nothing has closed yet today, so the answer is
    yesterday's last bar — which is why the sync bound has to be a BAR and not a date."""
    settings = _bars(tmp_path, historical_bar_size="1h")
    moment = datetime.fromisoformat(when).replace(tzinfo=ZoneInfo("America/New_York"))
    assert str(last_closed_bar(settings, moment))[:16] == expected


@pytest.mark.parametrize(
    "when,expected",
    [
        ("2024-01-05 10:00", "2024-01-04"),   # today's daily bar is still forming
        ("2024-01-05 16:05", "2024-01-05"),   # and after the close it is complete
        ("2024-01-05 08:00", "2024-01-04"),
        ("2024-01-06 12:00", "2024-01-05"),   # Saturday -> Friday
    ],
)
def test_last_closed_bar_for_a_daily_bar_follows_the_date_rule(tmp_path, when, expected):
    """Calendar bars keep the behaviour the dataset already had — this must not regress."""
    settings = _bars(tmp_path, historical_bar_size="1d")
    moment = datetime.fromisoformat(when).replace(tzinfo=ZoneInfo("America/New_York"))
    assert last_closed_bar(settings, moment).date().isoformat() == expected


def test_a_four_hour_grid_closes_fewer_bars_a_day(tmp_path):
    """The grid follows the bar size, so a 4h strategy gets four bars a session, not seven."""
    settings = _bars(tmp_path, historical_bar_size="4h")
    moment = datetime.fromisoformat("2024-01-05 14:05").replace(tzinfo=ZoneInfo("America/New_York"))
    assert str(last_closed_bar(settings, moment))[:16] == "2024-01-05 09:30"


def test_the_live_lookback_setting_is_a_floor_not_a_window(tmp_path):
    """A window shorter than the features need is not a small decision — it is no decision.

    Too few bars evaluates every indicator to NaN, so every rule is skipped and the signal
    is HOLD for ever, which is indistinguishable from a quiet market. So the bar count
    DERIVED from the configured features sets the floor, and LIVE_LOOKBACK_DAYS can only
    ever raise it. This is the reconcile between the setting and that derivation: the
    derivation is authoritative, the setting is slack.
    """

    class WindowClient:
        """Records how far back the fetch actually asked for."""

        def __init__(self, df):
            self.df = df
            self.days = None

        def fetch_historical(self, symbol, start_date, end_date, interval):
            self.days = (datetime.now(ZoneInfo("America/New_York")).date()
                         - datetime.fromisoformat(start_date).date()).days
            return self.df

    df = OpenBBClient._normalize(make_raw_df(10))

    # A window far too small for the features: the derivation wins.
    tiny = make_settings(tmp_path).model_copy(update={"live_lookback_days": 1})
    client = WindowClient(df)
    get_history_window(tiny, client=client)
    needed = days_for_bars(tiny, required_bars(tiny))
    assert client.days >= needed, "a too-small setting must not shrink the window"
    assert client.days > 1, "LIVE_LOOKBACK_DAYS=1 did not shorten anything, so it was a floor"

    # A generous window is honoured as-is: the setting can still buy tolerance for a
    # missed run, which is the only thing it is for.
    roomy = make_settings(tmp_path).model_copy(update={"live_lookback_days": 365})
    client = WindowClient(df)
    get_history_window(roomy, client=client)
    assert 364 <= client.days <= 365, client.days

    # And an explicit bar count is the caller's business, not the setting's.
    client = WindowClient(df)
    get_history_window(roomy, client=client, bars=required_bars(roomy) + 50)
    assert client.days >= days_for_bars(roomy, required_bars(roomy) + 50)


# ---------------------------------------------------------------------------
# canonical dataset store (Parquet)
# ---------------------------------------------------------------------------
def test_dataset_roundtrip(tmp_path):
    settings = Settings(historical_data_dir=str(tmp_path))
    df = OpenBBClient._normalize(make_raw_df(3))
    save_dataset(settings, df, "AAPL", "1d")
    assert dataset_path(settings, "AAPL", "1d").exists()
    loaded = load_dataset(settings, "AAPL", "1d")
    assert len(loaded) == 3
    assert list(loaded.columns) == OHLCV_COLUMNS
    assert loaded.index.equals(df.index)


def test_dataset_merge_dedupes(tmp_path):
    settings = Settings(historical_data_dir=str(tmp_path))
    idx1 = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    df1 = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0],
            "high": [2.0, 3.0, 4.0],
            "low": [0.0, 1.0, 2.0],
            "close": [1.5, 2.5, 3.5],
            "volume": [100.0, 200.0, 300.0],
        },
        index=idx1,
    )
    save_dataset(settings, df1, "AAPL", "1d")

    # Second write overlaps 2024-01-03 (updated close) and adds 2024-01-04.
    idx2 = pd.date_range("2024-01-03", periods=2, freq="D", tz="UTC")
    df2 = pd.DataFrame(
        {
            "open": [9.0, 9.0],
            "high": [10.0, 10.0],
            "low": [8.0, 8.0],
            "close": [9.5, 10.5],
            "volume": [900.0, 1000.0],
        },
        index=idx2,
    )
    save_dataset(settings, df2, "AAPL", "1d")

    loaded = load_dataset(settings, "AAPL", "1d")
    assert len(loaded) == 4  # 3 original + 1 new (01-03 deduped, last-write-wins)
    assert loaded.loc["2024-01-03"]["close"] == 9.5
    assert loaded.index.is_monotonic_increasing


def test_load_dataset_drops_bars_with_missing_price(tmp_path):
    """A bad bar already at rest is filtered on read (self-healing)."""
    settings = Settings(historical_data_dir=str(tmp_path))
    df = OpenBBClient._normalize(make_raw_df(3))
    df.loc[df.index[-1], "close"] = float("nan")
    path = dataset_path(settings, "AAPL", "1d")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)  # written straight to disk, bypassing save_dataset

    loaded = load_dataset(settings, "AAPL", "1d")
    assert len(loaded) == 2
    assert loaded["close"].notna().all()


def test_save_dataset_never_persists_a_bar_without_a_price(tmp_path):
    settings = Settings(historical_data_dir=str(tmp_path))
    df = OpenBBClient._normalize(make_raw_df(3))
    df.loc[df.index[-1], "close"] = float("nan")
    save_dataset(settings, df, "AAPL", "1d")
    assert len(load_dataset(settings, "AAPL", "1d")) == 2


def test_bad_refetch_cannot_overwrite_a_good_bar(tmp_path):
    """Dedupe keeps the LAST row per timestamp — so a price-less re-fetch of a
    bar we already hold must be dropped instead of clobbering the good value."""
    settings = Settings(historical_data_dir=str(tmp_path))
    good = OpenBBClient._normalize(make_raw_df(3))
    save_dataset(settings, good, "AAPL", "1d")

    placeholder = good.copy()
    placeholder.loc[placeholder.index[-1], "close"] = float("nan")
    save_dataset(settings, placeholder, "AAPL", "1d")

    loaded = load_dataset(settings, "AAPL", "1d")
    assert len(loaded) == 3
    assert loaded["close"].notna().all()
    assert loaded.loc[loaded.index[-1], "close"] == float(good["close"].iloc[-1])


def test_fetch_candles_persists_dataset(tmp_path):
    settings = Settings(historical_data_dir=str(tmp_path))

    class FakeClient:
        def __init__(self, df):
            self.df = df

        def fetch_historical(self, symbol, start_date, end_date, interval, provider=None):
            return self.df

    df = OpenBBClient._normalize(make_raw_df(3))
    fetch_candles(
        settings,
        client=FakeClient(df),
        symbol="AAPL",
        start_date="2024-01-01",
        bar_size="1d",
    )
    loaded = load_dataset(settings, "AAPL", "1d")
    assert len(loaded) == 3


def test_fetch_candles_empty_end_date_becomes_none(tmp_path):
    """An empty HISTORICAL_END_DATE (i.e. "until now") must reach the
    provider as None — not "" — or OpenBB rejects the request."""
    settings = Settings(
        historical_data_dir=str(tmp_path),
        historical_start_date="2024-01-01",
        historical_end_date="",
    )
    seen = {}

    class RecordingClient:
        def fetch_historical(self, symbol, start_date, end_date, interval, provider=None):
            seen["start_date"] = start_date
            seen["end_date"] = end_date
            return OpenBBClient._normalize(make_raw_df(1))

    fetch_candles(settings, client=RecordingClient(), symbol="AAPL", bar_size="1d")
    assert seen["start_date"] == "2024-01-01"
    assert seen["end_date"] is None


def test_empty_optional_date_fields_normalize_to_none():
    s = Settings(
        historical_start_date="",
        historical_end_date="",
        backtest_start_date="  ",
        backtest_end_date="",
    )
    assert s.historical_start_date is None
    assert s.historical_end_date is None
    assert s.backtest_start_date is None
    assert s.backtest_end_date is None


# ---------------------------------------------------------------------------
# S3 sync (durable source of truth) — boto3 is mocked
# ---------------------------------------------------------------------------
def test_dataset_uploads_to_s3_when_enabled(tmp_path, monkeypatch):
    import src.data.dataset as ds

    calls = {}

    class FakeS3:
        def upload_file(self, filename, bucket, key):
            calls["upload"] = (filename, bucket, key)

        def download_file(self, bucket, key, filename):
            calls["download"] = (bucket, key, filename)

    monkeypatch.setattr(ds, "_s3_client", lambda settings: FakeS3())
    settings = Settings(
        historical_data_dir=str(tmp_path),
        s3_enabled=True,
        s3_bucket="traider-dataset",
        s3_prefix="traider/historical",
    )
    df = OpenBBClient._normalize(make_raw_df(3))
    save_dataset(settings, df, "AAPL", "1d")

    assert dataset_path(settings, "AAPL", "1d").exists()  # local always written
    assert calls["upload"][1] == "traider-dataset"
    assert calls["upload"][2] == "traider/historical/AAPL_1d.parquet"


def test_dataset_restores_from_s3_when_local_missing(tmp_path, monkeypatch):
    import src.data.dataset as ds

    class FakeS3:
        def download_file(self, bucket, key, filename):
            # Simulate S3 holding the file: materialize it locally.
            df = OpenBBClient._normalize(make_raw_df(3))
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(filename)

    monkeypatch.setattr(ds, "_s3_client", lambda settings: FakeS3())
    settings = Settings(
        historical_data_dir=str(tmp_path / "hist"),
        s3_enabled=True,
        s3_bucket="traider-dataset",
        s3_prefix="traider/historical",
    )
    loaded = load_dataset(settings, "AAPL", "1d")
    assert len(loaded) == 3
    assert dataset_path(settings, "AAPL", "1d").exists()  # restored to local
