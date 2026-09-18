"""Tests for the risk layer: the sizing the backtest and a live run share, and the
backtest's replay of it (plan tasks 22, 23, 24, 24b).

All offline. The engine-level tests use synthetic candles and a temp store.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.backtest import metrics
from src.backtest.engine import run_backtest, simulate_frame
from src.backtest.risk_sim import RiskConfig, apply_risk_layer
from src.config.settings import Settings
from src.model import rules as rules_mod
from src.risk.position_sizing import (
    MIN_VOLATILITY_PERCENT,
    realized_volatility_percent,
    size_position,
    target_weight,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _settings(tmp_path, **kw) -> Settings:
    defaults = dict(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="TEST",
        historical_bar_size="1d",
    )
    defaults.update(kw)
    return Settings(**defaults)


def _store_rule(tmp_path, settings, side="BUY") -> None:
    store = rules_mod.load_store(settings)
    rs = rules_mod.empty_strategy("Test", "TEST")
    if side == "BUY":
        cond = rules_mod.RuleCondition(feature="close", op="<", value=1e12)
    else:
        cond = rules_mod.RuleCondition(feature="close", op=">", value=-1e12)
    rs.rules = [rules_mod.Rule(side=side, conditions=[cond])]
    store.strategies["Test"] = rs
    store.active = "Test"
    rules_mod.save_store(settings, store)


def _ohlc(prices, half_range=0.01):
    """Build an OHLC frame whose high/low straddle each close by ``half_range``."""
    idx = pd.date_range("2024-01-02", periods=len(prices), freq="B", tz="America/New_York")
    px = pd.Series(prices, dtype=float)
    return pd.DataFrame(
        {
            "open": px.to_numpy(),
            "high": (px * (1 + half_range)).to_numpy(),
            "low": (px * (1 - half_range)).to_numpy(),
            "close": px.to_numpy(),
            "volume": [1e6] * len(prices),
        },
        index=idx,
    )


def _zigzag(n=60, amp=0.06, period=12):
    """A market that swings enough to hit both a 2% stop and a 4% take."""
    return [100.0 * (1 + amp * math.sin(2 * math.pi * i / period)) for i in range(n)]


def _declining(n=40, step=0.01):
    """A steadily falling market: a long entry stops out almost every time."""
    return [100.0 * (1 - step) ** i for i in range(n)]


# ---------------------------------------------------------------------------
# position sizing
# ---------------------------------------------------------------------------
def test_target_weight_is_risk_over_stop_capped_by_exposure():
    # Risking 2% behind a 4% stop = half the account.
    assert target_weight(2.0, 4.0, 100.0) == pytest.approx(0.5)
    # 2% risk behind a 2% stop = fully invested (as before the risk layer).
    assert target_weight(2.0, 2.0, 100.0) == pytest.approx(1.0)
    # …but never more than the exposure cap.
    assert target_weight(2.0, 1.0, 50.0) == pytest.approx(0.5)
    # No stop distance => nothing can be sized.
    assert target_weight(2.0, 0.0, 100.0) == 0.0


def test_size_position_matches_the_documented_formula():
    # (account x risk%) / stop_distance  with a 4% stop on a 100 price:
    # 10_000 x 2% = 200 of risk; 200 / 4 = 50 units.
    sizing = size_position(10_000.0, 100.0, stop_loss_percent=4.0, risk_limit_percent=2.0)
    assert sizing.quantity == 50
    assert sizing.notional == pytest.approx(5_000.0)
    assert sizing.weight == pytest.approx(0.5)
    assert sizing.risk_amount == pytest.approx(200.0)
    assert sizing.stop_price == pytest.approx(96.0)
    assert sizing.capped is False


def test_size_position_take_profit_and_short_levels():
    long = size_position(10_000.0, 100.0, 2.0, take_profit_percent=4.0, risk_limit_percent=1.0)
    assert long.take_profit_price == pytest.approx(104.0)
    short = size_position(
        10_000.0, 100.0, 2.0, take_profit_percent=4.0, risk_limit_percent=1.0, side="short"
    )
    # A short's stop sits ABOVE the entry and its target BELOW.
    assert short.stop_price == pytest.approx(102.0)
    assert short.take_profit_price == pytest.approx(96.0)


def test_size_position_respects_max_exposure_and_existing_positions():
    sizing = size_position(
        10_000.0, 100.0, stop_loss_percent=2.0, risk_limit_percent=2.0, max_exposure_percent=50.0
    )
    assert sizing.weight == pytest.approx(0.5)
    assert sizing.capped is True

    used = size_position(
        10_000.0, 100.0, 2.0, risk_limit_percent=2.0, existing_notional=4_000.0
    )
    # Only the leftover allowance (100% cap - 4000 already committed) is usable.
    assert used.notional == pytest.approx(6_000.0)
    assert used.capped is True


def test_size_position_refuses_unusable_inputs():
    assert size_position(10_000.0, 100.0, stop_loss_percent=0.0).quantity == 0
    assert size_position(0.0, 100.0, stop_loss_percent=2.0).quantity == 0
    assert size_position(10_000.0, 0.0, stop_loss_percent=2.0).quantity == 0


def test_realized_volatility_is_floored_and_needs_history():
    assert realized_volatility_percent([100.0, 101.0]) is None
    quiet = realized_volatility_percent([100.0] * 20)
    assert quiet == pytest.approx(MIN_VOLATILITY_PERCENT)
    jumpy = realized_volatility_percent([100.0, 110.0, 100.0, 110.0, 100.0, 110.0])
    assert jumpy is not None and jumpy > 1.0


def test_volatility_target_mode_uses_the_volatility_stop():
    sizing = size_position(
        10_000.0, 100.0, stop_loss_percent=9.0, risk_limit_percent=2.0,
        mode="volatility_target", volatility_percent=4.0,
    )
    # The volatility (4%) replaces the fixed stop (9%) -> half the account.
    assert sizing.stop_percent == pytest.approx(4.0)
    assert sizing.weight == pytest.approx(0.5)




# ---------------------------------------------------------------------------
# the backtest applies the risk layer (task 24b)
# ---------------------------------------------------------------------------
# Every risk field named explicitly rather than left to defaults: a repo ``.env`` or an
# exported variable overrides a default, so "no risk settings" has to be spelled out.
_NO_RISK = dict(
    stop_loss_percent=None,
    take_profit_percent=None,
    risk_limit_percent=None,
    max_loss_percent=None,
    max_consecutive_losses=None,
    max_exposure_percent=100.0,
)


def test_an_empty_risk_config_reproduces_the_raw_engine(tmp_path):
    """The invariant that stops the two code paths drifting apart.

    "No risk settings" used to be a master switch turned off. It is now simply the
    configuration with every risk box empty — no stop, no target, no risk per trade,
    the whole account — and it must still reproduce the raw replay exactly, because
    that is what the old switch meant.
    """
    settings = _settings(tmp_path, **_NO_RISK)
    _store_rule(tmp_path, settings, "BUY")
    df = _ohlc(_zigzag())

    res = run_backtest(settings, dataset=df)
    assert res["inputs"]["risk"]["applied"] is False
    assert res["inputs"]["risk"]["stop_loss_percent"] is None

    sig = ["BUY"] * len(df)
    returns, trades, in_pos, _ = simulate_frame(
        df["open"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        sig,
        slippage=float(settings.backtest_slippage_percent) / 100.0,
        commission=float(settings.backtest_commission_per_trade),
        allow_short=False,
    )
    raw = metrics.compute_metrics(
        returns, list(df.index), trades, periods_per_year=252.0, in_position_legs=in_pos
    )
    assert res["metrics"]["num_trades"] == raw["num_trades"]
    assert res["metrics"]["total_return_pct"] == pytest.approx(raw["total_return_pct"], rel=1e-9)
    assert res["metrics"]["max_drawdown_pct"] == pytest.approx(raw["max_drawdown_pct"], rel=1e-9)
    # Same final equity (the stored curve is rounded to 6 dp per point).
    assert res["equity_curve"][-1]["equity"] == pytest.approx(raw_equity(returns), abs=1e-6)


def raw_equity(returns) -> float:
    eq = 1.0
    for r in returns:
        eq *= 1.0 + r
    return eq


def test_stop_loss_caps_the_loss_and_truncates_the_leg(tmp_path):
    settings = _settings(tmp_path, stop_loss_percent=2.0, risk_limit_percent=None,
                         take_profit_percent=None, max_exposure_percent=100.0)
    _store_rule(tmp_path, settings, "BUY")
    df = _ohlc(_declining())

    res = run_backtest(settings, dataset=df)
    risk = res["inputs"]["risk"]
    assert risk["applied"] is True
    assert risk["stop_exits"] > 0
    # Every long was stopped out for about -2% (the last one may still be open
    # at the end of the window and is force-closed instead).
    assert res["trades"], "expected trades"
    assert all(t["exit_reason"] in ("stop", "forced") for t in res["trades"])
    stopped = [t for t in res["trades"] if t["exit_reason"] == "stop"]
    assert stopped and max(t["ret_pct"] for t in stopped) < -1.5


def test_take_profit_closes_early_and_the_rule_re_enters(tmp_path):
    # Both levels are named: an empty box means NOT APPLIED, so a test that relies on
    # ambient .env values would be testing the developer's configuration.
    settings = _settings(
        tmp_path,
        stop_loss_percent=2.0,
        take_profit_percent=4.0,
        risk_limit_percent=2.0,
        max_exposure_percent=100.0,
    )
    _store_rule(tmp_path, settings, "BUY")
    df = _ohlc(_zigzag())

    res = run_backtest(settings, dataset=df)
    assert res["inputs"]["risk"]["take_exits"] > 0
    takes = [t for t in res["trades"] if t["exit_reason"] == "take"]
    assert takes and all(t["ret_pct"] > 3.0 for t in takes)
    # More round-trips than the strategy's own signals would produce alone.
    assert res["metrics"]["num_trades"] > 1


def test_sizing_scales_returns_by_the_target_weight(tmp_path):
    settings = _settings(
        tmp_path, apply_risk_layer=True, risk_limit_percent=1.0, stop_loss_percent=2.0
    )
    _store_rule(tmp_path, settings, "BUY")
    df = _ohlc(_zigzag())

    res = run_backtest(settings, dataset=df)
    # 1% risk behind a 2% stop = half the account in play.
    assert res["inputs"]["risk"]["weight"] == pytest.approx(0.5)
    # The trade log keeps the raw price move AND the equity contribution.
    for t in res["trades"]:
        assert t["equity_ret_pct"] == pytest.approx(t["ret_pct"] * 0.5, rel=1e-6, abs=1e-3)


def test_apply_risk_layer_returns_zero_returns_when_flat():
    # No signals at all -> flat, no legs, no exposure.
    out = apply_risk_layer(
        ["HOLD"] * 10, [1.0] * 10, [1.0] * 10, [1.0] * 10, [1.0] * 10, 10,
        config=RiskConfig(),
    )
    assert out.returns == [0.0] * 9
    assert out.trades == []
    assert out.in_position_bars == 0
