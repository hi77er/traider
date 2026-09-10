"""Tests for the rule-based signal model (src/model/simple_model.py).

Pure evaluation tests use plain row dicts (no network, no files). A few
wiring tests exercise the generator against a synthetic candle frame and the
strategy store on a temp directory.
"""

import numpy as np
import pandas as pd
import pytest

from src.config.settings import Settings
from src.features.schema import allowed_series
from src.model import rules as R
from src.model import simple_model as M

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cond(feature, op, value=None, ref=None):
    return R.RuleCondition(feature=feature, op=op, value=value, ref=ref)


def _rule(side="BUY", mode="all", conf=0.75, enabled=True, conditions=()):
    return R.Rule(side=side, mode=mode, confidence=conf, enabled=enabled, conditions=list(conditions))


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        strategy_rules_file=str(tmp_path / "rules" / "active.json"),
        instrument="AAPL",
        model_type="rule_based",
        model_buy_threshold=0.6,
        model_sell_threshold=0.6,
    )


# ---------------------------------------------------------------------------
# condition evaluation
# ---------------------------------------------------------------------------


def test_condition_numeric_ops():
    row = {"rsi_14": 30.0}
    assert M.condition_holds(_cond("rsi_14", "<", value=31), row)
    assert not M.condition_holds(_cond("rsi_14", "<", value=30), row)
    assert M.condition_holds(_cond("rsi_14", "<=", value=30), row)
    assert M.condition_holds(_cond("rsi_14", ">", value=29), row)
    assert M.condition_holds(_cond("rsi_14", ">=", value=30), row)
    assert M.condition_holds(_cond("rsi_14", "==", value=30), row)
    assert M.condition_holds(_cond("rsi_14", "!=", value=29), row)


def test_condition_ref_compare():
    row = {"close": 99.0, "sma_50": 100.0}
    assert M.condition_holds(_cond("close", "<", ref="sma_50"), row)
    row2 = {"close": 101.0, "sma_50": 100.0}
    assert not M.condition_holds(_cond("close", "<", ref="sma_50"), row2)


def test_condition_missing_or_nan_is_false():
    assert not M.condition_holds(_cond("rsi_14", "<", value=30), {})
    assert not M.condition_holds(_cond("close", "<", ref="sma_50"), {"close": 10.0})
    nan_row = {"rsi_14": np.nan}
    assert not M.condition_holds(_cond("rsi_14", "<", value=30), nan_row)


def test_crosses_above_and_below():
    cur = {"close": 100.0, "sma_20": 99.0}
    above_prev = {"close": 95.0, "sma_20": 99.0}  # diff -4 -> +1
    below_prev = {"close": 102.0, "sma_20": 99.0}  # diff +3 -> +1 (no cross)
    assert M.condition_holds(_cond("close", "crosses_above", ref="sma_20"), cur, above_prev)
    assert not M.condition_holds(_cond("close", "crosses_above", ref="sma_20"), cur, below_prev)

    cur2 = {"close": 99.0, "sma_20": 100.0}
    bprev = {"close": 101.0, "sma_20": 100.0}  # diff +1 -> -1
    assert M.condition_holds(_cond("close", "crosses_below", ref="sma_20"), cur2, bprev)
    assert not M.condition_holds(_cond("close", "crosses_below", ref="sma_20"), cur2, above_prev)


def test_cross_against_number():
    cur = {"rsi_14": 32.0}
    prev = {"rsi_14": 28.0}
    assert M.condition_holds(_cond("rsi_14", "crosses_above", value=30), cur, prev)
    assert not M.condition_holds(_cond("rsi_14", "crosses_above", value=30), cur, None)


# ---------------------------------------------------------------------------
# rule evaluation
# ---------------------------------------------------------------------------


def test_rule_mode_all_and_any():
    cond1 = _cond("close", "<", ref="sma_50")
    cond2 = _cond("rsi_14", "<", value=30)
    row = {"close": 99.0, "sma_50": 100.0, "rsi_14": 28.0}  # both true
    row2 = {"close": 101.0, "sma_50": 100.0, "rsi_14": 28.0}  # cond1 false
    assert M.rule_fires(_rule(mode="all", conditions=[cond1, cond2]), row)
    assert not M.rule_fires(_rule(mode="all", conditions=[cond1, cond2]), row2)
    assert M.rule_fires(_rule(mode="any", conditions=[cond1, cond2]), row2)  # cond2 true
    assert not M.rule_fires(_rule(mode="any", conditions=[cond1, cond2]), {"close": 101.0, "sma_50": 100.0, "rsi_14": 40.0})


def test_disabled_rule_never_fires():
    rule = _rule(enabled=False, conditions=[_cond("close", "<", value=999)])
    assert not M.rule_fires(rule, {"close": 1.0})
    assert M.fired_candidates([rule], {"close": 1.0}) == []


# ---------------------------------------------------------------------------
# candidate resolution / gating
# ---------------------------------------------------------------------------


def test_resolve_no_candidates_is_hold():
    sig = M.resolve_signal([])
    assert sig.signal == "HOLD"
    assert sig.confidence == 0.0


def test_highest_confidence_side_wins():
    sig = M.resolve_signal([("BUY", 0.7), ("SELL", 0.85)])
    assert sig.signal == "SELL" and sig.confidence == 0.85
    sig2 = M.resolve_signal([("BUY", 0.9), ("SELL", 0.7)])
    assert sig2.signal == "BUY" and sig2.confidence == 0.9


def test_equal_buy_sell_is_hold_conflict():
    sig = M.resolve_signal([("BUY", 0.8), ("SELL", 0.8)])
    assert sig.signal == "HOLD"


def test_threshold_gate_suppresses_low_confidence():
    sig = M.resolve_signal([("BUY", 0.55)], buy_threshold=0.6)
    assert sig.signal == "HOLD"
    assert "suppressed" in sig.reason
    # above threshold -> fires
    assert M.resolve_signal([("BUY", 0.6)], buy_threshold=0.6).signal == "BUY"


def test_multiple_rules_same_side_use_max_confidence():
    rules = [
        _rule("BUY", conf=0.6, conditions=[_cond("close", "<", value=200)]),
        _rule("BUY", conf=0.9, conditions=[_cond("close", "<", value=200)]),
    ]
    row = {"close": 100.0}
    sig = M.decide_row(rules, row, buy_threshold=0.6, sell_threshold=0.6)
    assert sig.signal == "BUY" and sig.confidence == 0.9


def test_decide_row_no_rules_hold():
    sig = M.decide_row([], {"close": 1.0}, buy_threshold=0.6, sell_threshold=0.6)
    assert sig.signal == "HOLD"


# ---------------------------------------------------------------------------
# wiring: generator over candle frames (feature merge + warmup + live==last)
# ---------------------------------------------------------------------------


def _candles(n=25, step=0.5):
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    close = np.arange(n, dtype=float) * step + 100.0  # strictly increasing
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def test_evaluate_frame_raw_rule_and_hold():
    # BUY when open < high (fires every bar except when equal) with conf .75.
    rules = [_rule("BUY", conf=0.75, conditions=[_cond("open", "<", ref="high")])]
    settings = Settings(_env_file=None, model_type="rule_based", model_buy_threshold=0.6, model_sell_threshold=0.6)
    gen = M.RuleBasedSignalGenerator(settings=settings, rules=rules)
    df = _candles()
    out = gen.evaluate_frame(df)
    assert list(out.columns) == ["signal", "confidence", "reason"]
    assert (out["signal"] == "BUY").all()  # open < high on every crafted bar

    # A bar where open == high must NOT fire (and no other rule -> HOLD).
    df2 = df.copy()
    df2.iloc[5, df2.columns.get_loc("open")] = df2.iloc[5]["high"]
    out2 = gen.evaluate_frame(df2)
    assert out2.iloc[5]["signal"] == "HOLD"
    assert out2.iloc[0]["signal"] == "BUY"


def test_evaluate_frame_feature_warmup_and_latest_identical():
    # SELL when close > sma_20 (feature from the shared FeatureEngineer).
    rules = [_rule("SELL", conf=0.75, conditions=[_cond("close", ">", ref="sma_20")])]
    settings = Settings(_env_file=None, model_type="rule_based", model_buy_threshold=0.6, model_sell_threshold=0.6)
    gen = M.RuleBasedSignalGenerator(settings=settings, rules=rules)
    df = _candles()
    out = gen.evaluate_frame(df)
    assert out["signal"].iloc[0] == "HOLD"  # inside sma_20 warmup
    assert out["signal"].iloc[19:].eq("SELL").all()  # fires once sma is stable
    assert out["confidence"].iloc[-1] == 0.75

    latest = gen.evaluate_latest(df)
    assert latest.signal == out["signal"].iloc[-1]
    assert latest.confidence == out["confidence"].iloc[-1]


def test_evaluate_latest_empty_input():
    gen = M.RuleBasedSignalGenerator(settings=Settings(_env_file=None, model_type="rule_based"), rules=[])
    sig = gen.evaluate_latest(pd.DataFrame())
    assert sig.signal == "HOLD"


def test_no_rules_always_hold_over_frame():
    gen = M.RuleBasedSignalGenerator(settings=Settings(_env_file=None, model_type="rule_based"), rules=[])
    out = gen.evaluate_frame(_candles())
    assert (out["signal"] == "HOLD").all()


# ---------------------------------------------------------------------------
# strategy store resolution
# ---------------------------------------------------------------------------


def test_resolve_ruleset_uses_active_and_excludes_deleted(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(
        active="a",
        strategies={
            "a": R.default_ruleset("AAPL").model_copy(update={"name": "a"}),
            "gone": R.default_ruleset("AAPL").model_copy(update={"name": "gone", "deleted": True}),
        },
    )
    R.save_store(st, store)

    rs = M.resolve_ruleset(st)
    assert rs is not None and rs.name == "a"  # active, non-deleted
    assert M.resolve_ruleset(st, "gone") is None  # soft-deleted
    assert M.resolve_ruleset(st, "nope") is None  # unknown
    assert M.resolve_ruleset(st, "a") is not None


def test_generator_for_strategy_loads_named_rules(tmp_path):
    st = _settings(tmp_path)
    rs = R.default_ruleset("AAPL").model_copy(update={"name": "b"})
    store = R.StrategyStore(active="a", strategies={
        "a": R.empty_strategy("a", "AAPL"),
        "b": rs,
    })
    R.save_store(st, store)
    gen = M.RuleBasedSignalGenerator(settings=st, rules=list(rs.rules))
    assert len(gen.rules) == 2  # default BUY + SELL
    # active strategy "a" has no rules -> empty generator
    gen_empty = M.RuleBasedSignalGenerator(settings=st)
    assert gen_empty.rules == []
    df = _candles()
    sig = gen.evaluate_latest(df)
    assert sig.signal in {"BUY", "SELL", "HOLD"}


# ---------------------------------------------------------------------------
# MODEL_TYPE gate + disabled-feature skipping
# ---------------------------------------------------------------------------


def test_rule_based_generator_rejects_other_model_types(tmp_path):
    st = _settings(tmp_path)
    store = R.StrategyStore(active="a", strategies={"a": R.default_ruleset("AAPL").model_copy(update={"name": "a"})})
    R.save_store(st, store)
    logistic = Settings(
        _env_file=None,
        strategy_rules_file=str(tmp_path / "rules" / "active.json"),
        model_type="logistic_regression",
    )
    # loader path (reads the active strategy) must refuse…
    with pytest.raises(ValueError):
        M.RuleBasedSignalGenerator(settings=logistic)
    # …and so must explicitly-provided rules under a different model type.
    with pytest.raises(ValueError):
        M.RuleBasedSignalGenerator(
            settings=logistic,
            rules=[_rule("BUY", conditions=[_cond("close", "<", value=999)])],
        )


def test_skips_rules_referencing_disabled_features():
    settings = Settings(_env_file=None, model_type="rule_based", feature_sma_enabled=False)
    avail = allowed_series(settings)
    assert "close" in avail and "sma_20" not in avail  # sma disabled

    good = _rule("BUY", conf=0.75, conditions=[_cond("open", "<", ref="high")])  # raw only
    bad = _rule("BUY", conf=0.9, conditions=[_cond("close", "<", ref="sma_20")])  # disabled feature
    gen = M.RuleBasedSignalGenerator(settings=settings, rules=[good, bad])
    assert good in gen.rules and bad not in gen.rules
    assert bad in gen.skipped_rules

    # The disabled-feature rule never fires and never contributes confidence.
    row = {"open": 5.0, "high": 10.0, "close": 1.0, "sma_20": 100.0}
    sig = gen.decide(row)
    assert sig.signal == "BUY" and sig.confidence == 0.75  # the 0.9 rule is excluded


def test_allowed_series_follows_feature_toggles():
    on = Settings(_env_file=None, model_type="rule_based")
    assert "sma_50" in allowed_series(on) and "rsi_14" in allowed_series(on)
    off = Settings(_env_file=None, model_type="rule_based", feature_rsi_enabled=False)
    assert "rsi_14" not in allowed_series(off)
    assert "sma_50" in allowed_series(off)
    for raw in ("open", "high", "low", "close", "volume"):  # always available
        assert raw in allowed_series(off)
