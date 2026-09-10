"""Tests for the signals API service (src/web/services/signal_service.py).

Uses monkeypatched effective settings + dataset so no network or real strategy
file is touched. Covers the MODEL_TYPE=rule_based gate and the rule-skipping
for disabled features, end to end.
"""

import numpy as np
import pandas as pd
import pytest

from src.config.settings import Settings
from src.model import rules as R
from src.web.services import signal_service


def _settings(tmp_path, model_type="rule_based", feature_sma_enabled=True,
              apply_risk_layer=True) -> Settings:
    return Settings(
        _env_file=None,
        strategy_rules_file=str(tmp_path / "rules" / "active.json"),
        instrument="AAPL",
        historical_bar_size="1d",
        model_type=model_type,
        feature_sma_enabled=feature_sma_enabled,
        apply_risk_layer=apply_risk_layer,
    )


def _candles(n=40, step=0.5):
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    close = np.arange(n, dtype=float) * step + 100.0
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


def _spike_candles():
    """Flat, then a 10% gap down — every long is stopped out on entry.

    The 2% stop of an entry at 100 sits at 98, and every bar's low is 98 or
    lower, so each entry is stopped on its own bar and the strategy re-enters on
    the next signal. That is exactly the divergence this module must reflect: the
    raw state machine sees ONE leg, the risk layer sees nine stop-outs.
    """
    idx = pd.date_range("2026-01-01", periods=10, freq="D", tz="UTC")
    close = np.array([100, 100, 100, 100, 100, 100, 90, 90, 90, 90], dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 2.0,
            "close": close,
            "volume": np.full(len(close), 1_000_000.0),
        },
        index=idx,
    )


# The rule that fires on every bar: open < high is always true.
_ALWAYS_BUY = [R.Rule(side="BUY", mode="all", confidence=0.75,
                      conditions=[R.RuleCondition(feature="open", op="<", ref="high")])]


def _save_store(settings, rules):
    rs = R.RuleSet(name="a", instrument=settings.instrument, rules=rules)
    R.save_store(settings, R.StrategyStore(active="a", strategies={"a": rs}))


def test_not_rule_based_returns_reason_without_signals(monkeypatch, tmp_path):
    settings = _settings(tmp_path, model_type="logistic_regression")
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    payload = signal_service.signal_payload()
    assert payload["available"] is False
    assert payload["model_type"] == "logistic_regression"
    assert "MODEL_TYPE=rule_based" in payload["reason"]
    assert payload["latest"] is None and payload["series"] == [] and payload["counts"] == {}


def test_rule_based_runs_and_reports_signals(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _save_store(
        settings,
        [R.Rule(side="BUY", mode="all", confidence=0.75,
                conditions=[R.RuleCondition(feature="open", op="<", ref="high")])],
    )
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: _candles())

    payload = signal_service.signal_payload()
    assert payload["available"] is True
    assert payload["counts"].get("BUY", 0) > 0
    assert payload["latest"]["signal"] == "BUY"
    assert len(payload["series"]) == len(_candles())
    assert len(payload["enabled_rules"]) == 1


def test_rule_based_without_dataset_reports_reason(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _save_store(settings, [])
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: pd.DataFrame())
    payload = signal_service.signal_payload()
    assert payload["available"] is True
    assert payload["latest"] is None
    assert payload["reason"]  # explains the missing dataset


def test_disabled_feature_rule_is_skipped_in_service(monkeypatch, tmp_path):
    # sma features disabled, but the stored rule references sma_20 -> skipped.
    settings = _settings(tmp_path, feature_sma_enabled=False)
    _save_store(
        settings,
        [R.Rule(side="BUY", mode="all", confidence=0.9,
                conditions=[R.RuleCondition(feature="close", op="<", ref="sma_20")])],
    )
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: _candles())

    payload = signal_service.signal_payload()
    assert payload["available"] is True
    assert payload["enabled_rules"] == []  # the only rule is skipped
    assert payload["counts"] == {"HOLD": len(_candles())}  # never evaluated / fired


def test_fills_are_replayed_through_the_risk_layer(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _save_store(settings, _ALWAYS_BUY)
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: _spike_candles())

    payload = signal_service.signal_payload()
    assert payload["risk"]["applied"] is True
    # Nine entries, each stopped out on its own bar — not one long raw leg.
    assert payload["risk"]["stop_exits"] == 9
    rounds = [f for f in payload["fills"] if f["kind"] == "close"]
    assert len(rounds) == 9
    assert {f["reason"] for f in rounds} == {"stop"}
    # A stopped-out leg is shaded only up to its stop bar, never to the raw exit.
    stop_sides = [f for f in payload["fills"] if f.get("reason") == "stop"]
    assert all(f["side"] == "long" for f in stop_sides)
    # ...and every band is red, because every one of them lost.
    assert all(f["win"] is False and f["ret_pct"] < 0 for f in rounds)


def test_fills_carry_the_round_trips_outcome(monkeypatch, tmp_path):
    """The chart colours each held-period band by this flag (green/red)."""
    settings = _settings(tmp_path)
    _save_store(settings, _ALWAYS_BUY)
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: _candles())

    payload = signal_service.signal_payload()
    rounds = [f for f in payload["fills"] if f["kind"] == "close"]
    assert rounds
    takes = [f for f in rounds if f["reason"] == "take"]
    assert takes  # a rising market reaches the take profit
    assert all(f["win"] is True and f["ret_pct"] > 0 for f in takes)
    assert all(isinstance(f["win"], bool) for f in rounds)
    # equity_ret_pct is the return after sizing, so it can never exceed the
    # price move it came from.
    assert all(abs(f["equity_ret_pct"]) <= abs(f["ret_pct"]) + 1e-9 for f in rounds)


def test_risk_layer_off_shows_the_raw_strategy(monkeypatch, tmp_path):
    settings = _settings(tmp_path, apply_risk_layer=False)
    _save_store(settings, _ALWAYS_BUY)
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: _spike_candles())

    payload = signal_service.signal_payload()
    assert payload["risk"] == {"applied": False}
    assert payload["vetoed"] == []
    # The raw state machine holds ONE position from the first fill to the end.
    rounds = [f for f in payload["fills"] if f["kind"] == "close"]
    assert len(rounds) == 1
    assert rounds[0]["reason"] == "forced"
    # The raw path reports the outcome too (a long leg into a 10% drop loses).
    assert rounds[0]["win"] is False
    assert rounds[0]["ret_pct"] < 0


def test_breaker_vetoed_entries_are_not_reported_as_fills(monkeypatch, tmp_path):
    """A breaker-skipped entry must never appear as a fill or a shade band."""
    settings = _settings(tmp_path)
    _save_store(settings, _ALWAYS_BUY)
    monkeypatch.setattr(signal_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(signal_service, "load_dataset", lambda *a, **k: _candles())

    real = signal_service.risk_sim

    class _Stub:
        RiskConfig = real.RiskConfig

        @staticmethod
        def apply_risk_layer(*_a, **_k):
            return real.RiskResult(
                legs=[
                    {"entry_idx": 1, "exit_idx": 1, "direction": "long",
                     "skipped": True, "reason": "circuit_breaker", "weight": 0.0, "bars": 0},
                    {"entry_idx": 2, "exit_idx": 3, "direction": "long",
                     "entry_price": 100.0, "exit_price": 101.0, "raw_entry_price": 100.0,
                     "reason": "signal", "weight": 1.0, "bars": 1, "skipped": False},
                ],
                stats={"weight": 1.0, "signal_exits": 1, "breaker_skips": 1,
                       "breaker_trips": 1},
            )

    monkeypatch.setattr(signal_service, "risk_sim", _Stub)
    payload = signal_service.signal_payload()

    # Only the real leg became fills: one open + one close.
    assert [f["kind"] for f in payload["fills"]] == ["open", "close"]
    assert payload["fills"][1]["reason"] == "signal"
    # The vetoed entry is reported separately, at the bar it was refused on.
    rows = _candles()
    assert payload["vetoed"] == [
        {
            "time": signal_service.chart_time(
                rows.index[1], settings.historical_bar_size, settings.market_timezone
            ),
            "side": "long",
            "reason": "circuit_breaker",
        }
    ]
    assert payload["risk"]["breaker_skips"] == 1
    assert payload["risk"]["breaker_trips"] == 1
