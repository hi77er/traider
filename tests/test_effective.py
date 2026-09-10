"""Tests for the effective-settings resolver (src/config/effective.py).

The resolver returns a Settings object equal to the global .env base overlaid
with the ACTIVE strategy's config. These tests point the strategy store at a
temp file via a monkeypatched ``get_settings`` and never touch the repo files.
"""

from src.config.settings import Settings
from src.config import effective
from src.model import rules as R


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
        historical_bar_size="1d",
    )


def _fake_get_settings(settings):
    def fake():
        return settings
    fake.cache_clear = lambda: None  # effective calls this on .env change
    return fake


def test_no_strategies_returns_global(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    eff = effective.resolve_effective()
    assert eff.instrument == "AAPL"
    assert eff.historical_bar_size == "1d"


def test_overlay_applies_active_strategy(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    rs = R.empty_strategy("momentum", "MSFT")
    rs.config = {
        "INSTRUMENT": "msft",  # should be uppercased on resolve
        "MODEL_BUY_THRESHOLD": "0.3",
        "FEATURE_RSI_ENABLED": "False",
    }
    R.save_store(st, R.StrategyStore(active="momentum", strategies={"momentum": rs}))
    eff = effective.resolve_effective()
    assert eff.instrument == "MSFT"
    assert eff.model_buy_threshold == 0.3
    assert eff.feature_rsi_enabled is False


def test_missing_keys_fall_back_to_global_base(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    rs = R.empty_strategy("a", "AAPL")
    rs.config = {"MODEL_BUY_THRESHOLD": "0.3"}  # no instrument -> global AAPL stays
    R.save_store(st, R.StrategyStore(active="a", strategies={"a": rs}))
    eff = effective.resolve_effective()
    assert eff.instrument == "AAPL"
    assert eff.model_buy_threshold == 0.3


def test_deleted_active_is_ignored(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    rs = R.empty_strategy("gone", "TSLA").model_copy(update={"deleted": True})
    rs.config = {"INSTRUMENT": "TSLA"}
    R.save_store(st, R.StrategyStore(active="gone", strategies={"gone": rs}))
    eff = effective.resolve_effective()
    assert eff.instrument == "AAPL"  # active deleted -> no overlay


def test_invalid_config_falls_back_to_global(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    rs = R.empty_strategy("a", "AAPL")
    rs.config = {"MODEL_BUY_THRESHOLD": "5.0"}  # out of 0..1
    R.save_store(st, R.StrategyStore(active="a", strategies={"a": rs}))
    eff = effective.resolve_effective()
    assert eff.model_buy_threshold == 0.6  # default kept, no crash


def test_invalidate_reflects_new_active(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    a = R.empty_strategy("a", "MSFT")
    a.config = {"INSTRUMENT": "MSFT"}
    R.save_store(st, R.StrategyStore(active="a", strategies={"a": a}))
    assert effective.get_effective_settings().instrument == "MSFT"

    # switch active to b (global AAPL) and force re-resolution
    b = R.empty_strategy("b", "AAPL")
    b.config = {}
    R.save_store(st, R.StrategyStore(active="b", strategies={"a": a, "b": b}))
    effective.invalidate()
    assert effective.get_effective_settings().instrument == "AAPL"
