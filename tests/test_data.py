"""Unit tests for the data layer (OpenBB client, historical, live).

The OpenBB SDK is mocked at the ``client.obb`` level — no network access.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config.settings import Settings
from src.data.dataset import dataset_path, load_dataset, save_dataset
from src.data.historical import fetch_candles
from src.data.live import get_latest_candle, is_market_open
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
