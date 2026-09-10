"""Tests for the Web Portal dataset service + API endpoints.

The data layer is either exercised against a temp directory (service tests)
or mocked (endpoint tests) — no network access.
"""

import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.web.app import app
from src.web.services import dataset_service

client = TestClient(app)


def _settings(tmp_path) -> Settings:
    return Settings(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="1d",
        historical_start_date="2024-01-01",
    )


def _make_df(n: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
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


# ---------------------------------------------------------------------------
# service: status
# ---------------------------------------------------------------------------
def test_status_missing(tmp_path):
    st = _settings(tmp_path)
    s = dataset_service.dataset_status(st)
    assert s["exists"] is False
    assert s["rows"] == 0
    assert s["symbol"] == "AAPL"


def test_status_exists(tmp_path):
    st = _settings(tmp_path)
    save_dataset(st, _make_df(3), "AAPL", "1d")
    s = dataset_service.dataset_status(st)
    assert s["exists"] is True
    assert s["rows"] == 3
    assert s["start"] == "2024-01-01"
    assert s["end"] == "2024-01-03"
    assert s["last_price"] == pytest.approx(102.5)


# ---------------------------------------------------------------------------
# service: rows
# ---------------------------------------------------------------------------
def test_get_rows_pagination_newest_first(tmp_path):
    """Historical Data pages are NEWEST first: offset 0 is the latest bar and
    later offsets walk back in time (chart's limit=0 stays ascending)."""
    st = _settings(tmp_path)
    save_dataset(st, _make_df(10), "AAPL", "1d")  # dates 2024-01-01..01-10
    first = dataset_service.get_rows(st, limit=4, offset=0)
    assert first["total"] == 10
    assert [r["date"] for r in first["rows"]] == [
        "2024-01-10", "2024-01-09", "2024-01-08", "2024-01-07",
    ]
    page = dataset_service.get_rows(st, limit=4, offset=4)
    assert [r["date"] for r in page["rows"]] == [
        "2024-01-06", "2024-01-05", "2024-01-04", "2024-01-03",
    ]
    # pagination covers the whole set and never overlaps
    all_dates = []
    for off in (0, 4, 8):
        all_dates += [r["date"] for r in dataset_service.get_rows(st, limit=4, offset=off)["rows"]]
    assert len(all_dates) == len(set(all_dates)) == 10


def test_get_rows_all_ascending_for_chart(tmp_path):
    """limit=0 (chart payload) stays oldest-first/chronological."""
    st = _settings(tmp_path)
    save_dataset(st, _make_df(5), "AAPL", "1d")
    page = dataset_service.get_rows(st, limit=0)
    assert page["total"] == 5
    assert len(page["rows"]) == 5
    assert page["rows"][0]["date"] == "2024-01-01"
    assert page["rows"][-1]["date"] == "2024-01-05"


def test_get_rows_intraday_has_unique_times(tmp_path):
    """1h bars must get unique numeric chart times (date-collapsed duplicates
    left the candlestick chart blank) plus a readable date+time label."""
    st = Settings(
        historical_data_dir=str(tmp_path),
        instrument="NVDA",
        historical_bar_size="1h",
    )
    idx = pd.date_range("2024-01-02 09:30", periods=8, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(8)],
            "high": [101.0 + i for i in range(8)],
            "low": [99.0 + i for i in range(8)],
            "close": [100.5 + i for i in range(8)],
            "volume": [1000 + i for i in range(8)],
        },
        index=idx,
    )
    save_dataset(st, df, "NVDA", "1h")
    page = dataset_service.get_rows(st, limit=0)
    times = [r["time"] for r in page["rows"]]
    assert len(times) == 8
    assert all(isinstance(t, int) for t in times)
    assert len(set(times)) == 8  # strictly unique -> chart can draw
    assert [r["datetime"] for r in page["rows"]][0].startswith("2024-01-02")


# ---------------------------------------------------------------------------
# service: backfill (async, persists to dataset)
# ---------------------------------------------------------------------------
def _persisting_fetch(n: int, delay: float = 0.0):
    """Fake ``fetch_candles`` that also persists, like the real one."""

    def fake_fetch_candles(settings, **kwargs):
        if delay:
            time.sleep(delay)
        df = _make_df(n)
        save_dataset(settings, df, settings.instrument, settings.historical_bar_size)
        return df

    return fake_fetch_candles


def test_backfill_persists(tmp_path, monkeypatch):
    st = _settings(tmp_path)
    monkeypatch.setattr(dataset_service, "fetch_candles", _persisting_fetch(4))
    res = dataset_service.start_backfill(st)
    assert res["started"] is True

    for _ in range(100):  # wait for the background thread
        if not dataset_service._job_state()["running"]:
            break
        time.sleep(0.05)

    s = dataset_service.dataset_status(st)
    assert s["exists"] is True
    assert s["rows"] == 4


def test_backfill_blocks_second_start(tmp_path, monkeypatch):
    st = _settings(tmp_path)
    monkeypatch.setattr(dataset_service, "fetch_candles", _persisting_fetch(2, delay=0.5))
    first = dataset_service.start_backfill(st)
    second = dataset_service.start_backfill(st)
    assert first["started"] is True
    assert second["started"] is False
    assert "already running" in second["reason"]

    for _ in range(100):  # let it finish
        if not dataset_service._job_state()["running"]:
            break
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# endpoints (TestClient; service mocked to avoid real state)
# ---------------------------------------------------------------------------
def test_endpoint_status(monkeypatch):
    monkeypatch.setattr(
        dataset_service,
        "dataset_status",
        lambda settings=None: {
            "exists": True,
            "symbol": "AAPL",
            "interval": "1d",
            "rows": 1170,
            "start": "2022-01-03",
            "end": "2026-09-01",
            "last_price": 324.7,
            "job": {"running": False, "last_error": None, "last_run": None, "rows": 0},
        },
    )
    r = client.get("/api/v1/dataset/status")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    assert body["rows"] == 1170


def test_endpoint_health():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_endpoint_index_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "TRAIDER" in r.text
