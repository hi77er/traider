"""Tests for the strategy store schema + JSON persistence (src/model/rules.py).

Pure file/validation tests on a temp directory — no network, no .env reliance.
"""

import json

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.model import rules as R


def _settings(tmp_path) -> Settings:
    return Settings(
        strategy_rules_file=str(tmp_path / "rules" / "active.json"),
        instrument="AAPL",
    )


def test_default_ruleset_is_valid_example():
    rs = R.default_ruleset("AAPL")
    assert rs.instrument == "AAPL"
    assert [r.side for r in rs.rules] == ["BUY", "SELL"]
    # feature-vs-number and feature-vs-feature conditions both used
    assert rs.rules[0].conditions[0].ref == "sma_50"
    assert rs.rules[0].conditions[1].value == 30.0


def test_load_returns_empty_store_when_no_file(tmp_path):
    st = _settings(tmp_path)
    store = R.load_store(st)
    assert isinstance(store, R.StrategyStore)
    assert store.strategies == {}
    assert store.active is None
    assert not R.rules_file_path(st).exists()  # no auto-seed: create-first form


def test_save_store_roundtrip_preserves_multiple_strategies(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(active="a", strategies={
        "a": R.empty_strategy("a", "AAPL"),
        "b": R.default_ruleset("AAPL").model_copy(update={"name": "b"}),
    })
    R.save_store(st, store)
    loaded = R.load_store(st)
    assert set(loaded.strategies) == {"a", "b"}
    assert loaded.active == "a"
    assert loaded.strategies["a"].rules == []
    assert [r.side for r in loaded.strategies["b"].rules] == ["BUY", "SELL"]
    # active strategy gets its updated_at stamped on save
    assert loaded.strategies["a"].updated_at is not None


def test_save_stamps_only_active_strategy(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(active="x", strategies={"x": R.empty_strategy("x")})
    R.save_store(st, store)
    loaded = R.load_store(st)
    assert loaded.strategies["x"].updated_at is not None


def test_legacy_single_ruleset_file_is_migrated(tmp_path):
    st = _settings(tmp_path)
    path = R.rules_file_path(st)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(R.default_ruleset("AAPL").model_dump()), encoding="utf-8")
    store = R.load_store(st)  # triggers migration + persist
    assert store.active == "default"
    assert "default" in store.strategies
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "strategies" in on_disk and "active" in on_disk  # new wrapper format
    assert on_disk["strategies"]["default"]["rules"]


def test_normalize_active_fixes_stale_pointer(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(active="ghost", strategies={"real": R.empty_strategy("real")})
    R.save_store(st, store)
    assert R.load_store(st).active == "real"


def test_soft_deleted_strategies_are_excluded_from_live(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(active="a", strategies={
        "a": R.empty_strategy("a"),
        "gone": R.empty_strategy("gone").model_copy(update={"deleted": True}),
    })
    live = store.live_strategies()
    assert set(live) == {"a"}


def test_normalize_active_skips_deleted_and_none_when_all_deleted(tmp_path):
    st = _settings(tmp_path)
    # deleted strategy flagged active -> picks the first live one
    store = R.StrategyStore(active="gone", strategies={
        "gone": R.empty_strategy("gone").model_copy(update={"deleted": True}),
        "a": R.empty_strategy("a"),
    })
    R.normalize_active(store)
    assert store.active == "a"
    # all deleted -> active None
    store2 = R.StrategyStore(active="gone", strategies={
        "gone": R.empty_strategy("gone").model_copy(update={"deleted": True}),
    })
    R.normalize_active(store2)
    assert store2.active is None


def test_soft_delete_flag_roundtrips_to_disk(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(active="a", strategies={
        "a": R.empty_strategy("a"),
        "gone": R.empty_strategy("gone"),
    })
    store.strategies["gone"].deleted = True
    R.save_store(st, store)
    loaded = R.load_store(st)
    assert loaded.strategies["gone"].deleted is True
    assert loaded.strategies["a"].deleted is False


def test_strategy_config_roundtrips_to_disk(tmp_path):
    st = _settings(tmp_path)
    rs = R.empty_strategy("a", "AAPL")
    rs.config = {"MODEL_BUY_THRESHOLD": "0.7", "FEATURE_RSI_ENABLED": "False"}
    store = R.StrategyStore(active="a", strategies={"a": rs})
    R.save_store(st, store)
    loaded = R.load_store(st)
    assert loaded.strategies["a"].config["MODEL_BUY_THRESHOLD"] == "0.7"
    assert loaded.strategies["a"].config["FEATURE_RSI_ENABLED"] == "False"


def test_corrupt_file_returns_empty_store_and_reports_error(tmp_path):
    st = _settings(tmp_path)
    path = R.rules_file_path(st)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    store = R.load_store(st)
    assert store.strategies == {}
    assert R.store_error(st) is not None
    assert path.read_text(encoding="utf-8") == "{ not valid json"  # not overwritten


def test_invalid_conditions_rejected():
    with pytest.raises(ValidationError):
        R.RuleCondition(feature="close", op="<", value=1.0, ref="sma_50")  # both targets
    with pytest.raises(ValidationError):
        R.RuleCondition(feature="close", op="<")  # no target
    with pytest.raises(ValidationError):
        R.RuleCondition(feature="close", op="sideways")  # bad op
    with pytest.raises(ValidationError):
        R.RuleCondition(feature="close", op="<", ref="close")  # self compare


def test_rule_needs_a_condition():
    with pytest.raises(ValidationError):
        R.Rule(side="BUY", conditions=[])


def test_value_only_and_ref_only_conditions_are_valid():
    v = R.RuleCondition(feature="rsi_14", op="<", value=30.0)
    f = R.RuleCondition(feature="close", op="<", ref="sma_50")
    assert v.value == 30.0 and v.ref is None
    assert f.ref == "sma_50" and f.value is None

