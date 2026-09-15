"""Tests for the configurable historical window (years/bar-size) feature.

Covers the years-from-now window resolver, dataset deletion, and the config
schema options for Historical Period + Historical Bar Size.
"""

from pathlib import Path

import pandas as pd

from src.config.settings import Settings
from src.data import dataset as ds
from src.data.historical import resolve_history_window
from src.web.services import config_service


def _settings(tmp_path, **kw) -> Settings:
    base = dict(
        _env_file=None,
        instrument="AAPL",
        historical_data_dir=str(tmp_path / "hist"),
        market_timezone="UTC",
    )
    base.update(kw)
    return Settings(**base)


def test_resolve_history_window_uses_years_from_now(tmp_path):
    s = _settings(tmp_path, historical_lookback_years=3)
    start, end = resolve_history_window(s)
    assert end is None
    expected_year = pd.Timestamp.now(tz="UTC").date().year - 3
    assert pd.Timestamp(start).date().year == expected_year


def test_resolve_history_window_falls_back_to_dates(tmp_path):
    s = _settings(tmp_path, historical_lookback_years=None, historical_start_date="2020-01-01")
    start, end = resolve_history_window(s)
    assert start == "2020-01-01"
    assert end is None


def test_delete_dataset_removes_local_file(tmp_path):
    s = _settings(tmp_path)
    path = ds.dataset_path(s, "AAPL", "1d")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"parquet-ish")
    assert ds.delete_dataset(s, "AAPL", "1d") is True
    assert not path.exists()
    assert ds.delete_dataset(s, "AAPL", "1d") is False  # already gone


def test_bar_size_and_years_are_select_options():
    bars = config_service._OPTIONS["HISTORICAL_BAR_SIZE"]
    assert {"label": "1 hour", "value": "1h"} in bars
    assert {"label": "1 day", "value": "1d"} in bars
    years = config_service._OPTIONS["HISTORICAL_LOOKBACK_YEARS"]
    assert any(o["value"] == "5" and o["label"] == "5 years" for o in years)


def test_period_and_bar_size_are_strategy_scoped():
    scoped = {k for _, keys in config_service._STRATEGY_SCOPE for k in keys}
    assert "HISTORICAL_BAR_SIZE" in scoped
    assert "HISTORICAL_LOOKBACK_YEARS" in scoped


def test_legacy_dates_are_not_an_account_setting():
    """The fetch window is not offered any more: the strategy's period and bar size
    answer it, and a second, quieter answer in Account Settings would drift from the
    first. The Settings fields stay — the dataset code still reads them."""
    assert "HISTORICAL_START_DATE" not in config_service.ACCOUNT_SCOPED_KEYS
    assert "HISTORICAL_END_DATE" not in config_service.ACCOUNT_SCOPED_KEYS
    by_key = {f["key"] for g in config_service.account_sections() for f in g["fields"]}
    assert "HISTORICAL_START_DATE" not in by_key
    assert "HISTORICAL_END_DATE" not in by_key
