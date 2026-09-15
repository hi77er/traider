"""Tests for the configurable historical window (period/bar-size) feature.

Covers the period-from-now window resolver, dataset deletion, and the rule that
binds each bar size to the periods it may be fetched for.
"""

from pathlib import Path

import pandas as pd
import pytest

from src.config import history
from src.config.settings import Settings
from src.data import dataset as ds
from src.data.historical import resolve_history_window
from src.model import rules as rules_mod
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
    s = _settings(tmp_path, historical_lookback="3y")
    start, end = resolve_history_window(s)
    assert end is None
    expected_year = pd.Timestamp.now(tz="UTC").date().year - 3
    assert pd.Timestamp(start).date().year == expected_year


def test_resolve_history_window_uses_days_from_now(tmp_path):
    """Day-based windows are what the intraday bar sizes use: the provider only
    serves a short trailing window of minute bars, so those periods are measured
    in days rather than years."""
    s = _settings(tmp_path, historical_bar_size="15m", historical_lookback="30d")
    start, end = resolve_history_window(s)
    assert end is None
    expected = (pd.Timestamp.now(tz="UTC").normalize() - pd.DateOffset(days=30)).date()
    assert pd.Timestamp(start).date() == expected


def test_resolve_history_window_falls_back_to_dates(tmp_path):
    s = _settings(tmp_path, historical_lookback=None, historical_start_date="2020-01-01")
    start, end = resolve_history_window(s)
    assert start == "2020-01-01"
    assert end is None


def test_a_bare_number_still_means_years(tmp_path):
    """The setting used to be an int number of years. A value written then must
    keep meaning what it always did, or a 5-year strategy would silently become
    the 2-year default."""
    assert _settings(tmp_path, historical_lookback="5").historical_lookback == "5y"
    assert _settings(tmp_path, historical_lookback="30 days").historical_lookback == "30d"
    assert _settings(tmp_path, historical_lookback=" 2Y ").historical_lookback == "2y"


def test_an_unreadable_period_is_rejected(tmp_path):
    """Quietly reverting to "2 years" would fetch a different window than the one
    that was asked for, so an unreadable value is an error instead."""
    with pytest.raises(Exception):
        _settings(tmp_path, historical_lookback="nope")


def test_delete_dataset_removes_local_file(tmp_path):
    s = _settings(tmp_path)
    path = ds.dataset_path(s, "AAPL", "1d")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"parquet-ish")
    assert ds.delete_dataset(s, "AAPL", "1d") is True
    assert not path.exists()
    assert ds.delete_dataset(s, "AAPL", "1d") is False  # already gone


# The table the whole feature is: which periods a bar size may be fetched for.
EXPECTED_PERIODS = {
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


def test_each_bar_size_offers_exactly_its_periods():
    assert history.PERIODS_BY_BAR_SIZE == EXPECTED_PERIODS
    for bar, periods in EXPECTED_PERIODS.items():
        assert history.allowed_periods(bar) == periods, bar


def test_every_offered_bar_size_has_a_period_list():
    """A bar size in the dropdown with no periods would render an empty select,
    which a browser resolves by silently choosing something for the operator."""
    for code, _ in history.BAR_SIZES:
        assert history.allowed_periods(code), code


def test_an_unknown_bar_size_falls_back_to_every_period():
    """A hand-edited or future bar size must not be left with no options at all."""
    assert set(history.allowed_periods("3h")) == set(history.ALL_PERIODS)


def test_bar_size_and_period_are_select_options():
    bars = {o["value"]: o["label"] for o in history.periods_for_options("1d")}
    assert bars  # sanity
    codes = [c for c, _ in history.BAR_SIZES]
    assert codes[:4] == ["1m", "2m", "5m", "15m"], "the minute bars the user asked for"
    assert "1d" in codes
    labels = {o["value"]: o["label"] for o in history.periods_for_options("1m")}
    assert labels == {"15d": "15 days", "30d": "30 days"}


def test_period_and_bar_size_are_strategy_scoped():
    scoped = {k for _, keys in config_service._STRATEGY_SCOPE for k in keys}
    assert "HISTORICAL_BAR_SIZE" in scoped
    assert "HISTORICAL_LOOKBACK" in scoped


def test_the_bar_size_comes_before_the_period_in_the_panel():
    """The period's options depend on the bar size, so the pair has to read in the
    order the decision is made: pick the candle, then how far back it reaches."""
    groups = config_service.strategy_config_groups(Settings(_env_file=None))
    instrument = [g for g in groups if g["name"] == "Instrument"][0]
    keys = [f["key"] for f in instrument["fields"]]
    assert keys.index("HISTORICAL_BAR_SIZE") < keys.index("HISTORICAL_LOOKBACK")


def test_the_panel_offers_only_the_periods_the_bar_size_allows():
    for bar, periods in EXPECTED_PERIODS.items():
        groups = config_service.strategy_config_groups(
            Settings(_env_file=None), {"HISTORICAL_BAR_SIZE": bar}
        )
        field = [
            f for g in groups for f in g["fields"] if f["key"] == "HISTORICAL_LOOKBACK"
        ][0]
        assert [o["value"] for o in field["options"]] == list(periods), bar
        # ...and the whole table travels with it, so the browser can repopulate
        # the list locally when the bar size changes.
        assert field["depends_on"] == "HISTORICAL_BAR_SIZE"
        assert set(field["options_by"]) == set(EXPECTED_PERIODS)


def test_a_stored_period_outside_the_list_does_not_hide_the_choice(tmp_path):
    """A strategy stored with a pair that is no longer allowed still renders: the
    list is the bar size's own, and the client moves the selection onto it rather
    than leaving a select with nothing selected."""
    groups = config_service.strategy_config_groups(
        Settings(_env_file=None),
        {"HISTORICAL_BAR_SIZE": "1h", "HISTORICAL_LOOKBACK": "5y"},
    )
    field = [f for g in groups for f in g["fields"] if f["key"] == "HISTORICAL_LOOKBACK"][0]
    assert [o["value"] for o in field["options"]] == ["1y", "2y"]
    assert field["value"] == "5y", "the stored value is reported as-is, not rewritten"


def test_a_legacy_years_value_is_translated_not_dropped(tmp_path):
    """The key was renamed to carry a unit. Dropping the old one would silently
    change the window (5 years -> the 2-year default), so it is translated on
    load and the next save writes only the new key."""
    settings = _settings(tmp_path, strategy_rules_file=str(tmp_path / "store.json"))
    Path(settings.strategy_rules_file).write_text(
        '{"active": "s", "strategies": {"s": {"name": "s", '
        '"config": {"HISTORICAL_LOOKBACK_YEARS": "5", "INSTRUMENT": "AAPL"}}}}',
        encoding="utf-8",
    )
    store = rules_mod.load_store(settings)
    config = store.strategies["s"].config
    assert config["HISTORICAL_LOOKBACK"] == "5y"
    assert "HISTORICAL_LOOKBACK_YEARS" not in config


def test_a_translated_key_does_not_overwrite_a_new_one(tmp_path):
    """When both spellings are present the NEW one is the operator's latest word."""
    settings = _settings(tmp_path, strategy_rules_file=str(tmp_path / "store.json"))
    Path(settings.strategy_rules_file).write_text(
        '{"active": "s", "strategies": {"s": {"name": "s", "config": '
        '{"HISTORICAL_LOOKBACK_YEARS": "5", "HISTORICAL_LOOKBACK": "30d"}}}}',
        encoding="utf-8",
    )
    config = rules_mod.load_store(settings).strategies["s"].config
    assert config["HISTORICAL_LOOKBACK"] == "30d"
    assert "HISTORICAL_LOOKBACK_YEARS" not in config


def test_legacy_dates_are_not_an_account_setting():
    """The fetch window is not offered any more: the strategy's period and bar size
    answer it, and a second, quieter answer in Account Settings would drift from the
    first. The Settings fields stay — the dataset code still reads them."""
    assert "HISTORICAL_START_DATE" not in config_service.ACCOUNT_SCOPED_KEYS
    assert "HISTORICAL_END_DATE" not in config_service.ACCOUNT_SCOPED_KEYS
    by_key = {f["key"] for g in config_service.account_sections() for f in g["fields"]}
    assert "HISTORICAL_START_DATE" not in by_key
    assert "HISTORICAL_END_DATE" not in by_key
