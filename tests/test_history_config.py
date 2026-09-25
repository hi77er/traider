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


def test_resolve_history_window_needs_a_period(tmp_path):
    """No period is an ERROR, not another window.

    The start/end dates that used to answer this are gone. A silent fallback is how a
    strategy set to "30 days" came back fetched from 2022-01-01, and the panel had no way
    to show the disagreement — so an unreadable period is refused where the operator can
    see it (the backfill job reports this message) rather than turned into something else.
    """
    s = _settings(tmp_path, historical_lookback=None)
    with pytest.raises(ValueError, match="HISTORICAL_LOOKBACK"):
        resolve_history_window(s)


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


# The table the whole feature is: which periods a bar size may be fetched for
# FROM THE DEFAULT PROVIDER (yfinance). The static offer is the ceiling; what the
# configured provider can actually fill decides the list the panel shows.
EXPECTED_PERIODS = {
    "1m": ("6d",),  # Yahoo serves 5 sessions of 1-minute bars -> 15d/30d/7d are all out
    "2m": ("30d", "40d"),  # yfinance stops at ~31 sessions (measured), so 60d never fits
    "5m": ("30d", "60d"),
    "15m": ("30d", "60d"),
    "1h": ("1y", "2y"),
    "2h": ("1y", "2y"),
    "4h": ("1y", "2y"),  # fetched as 1h -> 730-day limit drops 3y
    "8h": ("1y", "2y"),
    "12h": ("1y", "2y"),
    "1d": ("2y", "3y", "4y", "5y"),
}


def test_each_bar_size_offers_exactly_its_periods():
    for bar, periods in EXPECTED_PERIODS.items():
        assert history.allowed_periods(bar, "yfinance") == periods, bar


def test_a_period_the_provider_cannot_fill_is_never_offered():
    """The whole point of the table: a "30 days" of 1-minute bars request came back
    as five trading days because the provider stops serving 1m after a week, and a
    60-day 2-minute request came back 43 days because yfinance stops at 31 sessions.
    The dropdown must not offer a window that silently arrives short."""
    assert history.allowed_periods("1m", "yfinance") == ("6d",)
    periods = {o["value"] for o in history.periods_for_options("1m", "yfinance")}
    assert periods == {"6d"}
    assert "60d" not in history.allowed_periods("2m", "yfinance")


def test_the_cap_is_the_window_the_provider_serves_whole():
    """The caps are measurements, not the provider's published limits: Yahoo's
    "last 60 days" starts 60 days back from NOW, so a window starting exactly there
    asks for a session it has already dropped. Where that costs one session out of
    ~40 the published limit stands (5m/15m/1h-2y); where it costs a fifth of the
    window (1m: the 7th day back is 1 of 5 sessions) the cap goes below it."""
    assert history.provider_max_days("yfinance", "1m") == 6
    assert history.provider_max_days("yfinance", "2m") == 40
    assert history.provider_max_days("yfinance", "5m") == 60
    assert history.provider_max_days("yfinance", "1h") == 730
    assert history.provider_max_days("yfinance", "1d") is None  # decades of daily bars


def test_a_resampled_bar_size_inherits_its_base_intervals_limit():
    """4h bars are fetched as 1h bars, so the provider's 1h limit binds on them —
    otherwise a 3-year request would be silently truncated to 2."""
    assert history.fetch_interval("4h") == "1h"
    assert history.provider_max_days("yfinance", "4h") == 730
    assert "3y" in history.PERIODS_BY_BAR_SIZE["4h"], "the static offer still lists it"
    assert "3y" not in history.allowed_periods("4h", "yfinance")


def test_a_provider_whose_limits_are_unknown_filters_nothing():
    """No cap is invented for a provider this module has no numbers for: a guess
    would hide history the provider really does serve."""
    assert history.allowed_periods("1m", "some-broker") == history.PERIODS_BY_BAR_SIZE["1m"]
    assert history.allowed_periods("1m", None) == history.PERIODS_BY_BAR_SIZE["1m"]


def test_the_provider_is_matched_case_insensitively():
    assert history.allowed_periods("1m", "YFinance") == ("6d",)


def test_every_offered_bar_size_has_a_period_list():
    """A bar size in the dropdown with no periods would render an empty select,
    which a browser resolves by silently choosing something for the operator."""
    for code, _ in history.BAR_SIZES:
        assert history.allowed_periods(code, "yfinance"), code


def test_an_unknown_bar_size_falls_back_to_every_period():
    """A hand-edited or future bar size must not be left with no options at all."""
    assert set(history.allowed_periods("3h")) == set(history.ALL_PERIODS)


def test_bar_size_and_period_are_select_options():
    bars = {o["value"]: o["label"] for o in history.periods_for_options("1d")}
    assert bars  # sanity
    codes = [c for c, _ in history.BAR_SIZES]
    assert codes[:4] == ["1m", "2m", "5m", "15m"], "the minute bars the user asked for"
    assert "1d" in codes
    labels = {o["value"]: o["label"] for o in history.periods_for_options("1d", "yfinance")}
    assert labels == {"2y": "2 years", "3y": "3 years", "4y": "4 years", "5y": "5 years"}
    minute = {o["value"]: o["label"] for o in history.periods_for_options("1m", "yfinance")}
    assert minute == {"6d": "6 days"}


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


def test_the_period_field_carries_one_short_line():
    """The dropdown shows which periods are on offer by LISTING them, so the only
    line under the setting is what the setting means — no second explanation of a
    constraint the control already states."""
    groups = config_service.strategy_config_groups(Settings(_env_file=None))
    field = [
        f for g in groups for f in g["fields"] if f["key"] == "HISTORICAL_LOOKBACK"
    ][0]
    assert field["hints"] == [
        "How much history to fetch, as <N>y years or <N>d days (e.g. 2y, 30d) up to now"
    ]


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
