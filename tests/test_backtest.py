"""Tests for the backtest engine, metrics and web service (no network).

The engine is exercised with synthetic candles and a temp strategy store; the
web service is exercised with monkeypatched dependencies.
"""

import re
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest import metrics
from src.backtest.engine import position_intervals, run_backtest, simulate_frame
from src.config.settings import Settings
from src.model import rules as rules_mod


# A run with NO risk settings: the raw strategy replay, which is what these tests
# pin. Written out because "empty" is a real instruction now, and because the
# repo's .env carries risk values that would otherwise be picked up.
_NO_RISK = dict(
    stop_loss_percent=None,
    take_profit_percent=None,
    risk_limit_percent=None,
    max_loss_percent=None,
    max_consecutive_losses=None,
    max_exposure_percent=100.0,
)


def _settings(tmp_path, **kw) -> Settings:
    defaults = dict(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="TEST",
        historical_bar_size="1d",
        model_type="rule_based",
        # These tests pin the RAW strategy simulation (no sizing, no stops),
        # which is what simulate_frame() does. A raw run is now expressed by
        # leaving the risk settings EMPTY rather than by a master switch, so they
        # are stated explicitly — an init value beats the repo's .env.
        **_NO_RISK,
    )
    defaults.update(kw)
    return Settings(**defaults)


def _store_buy_all(tmp_path, settings) -> None:
    """Seed a strategy with an always-true BUY rule (close < 1e12)."""
    store = rules_mod.load_store(settings)
    rs = rules_mod.empty_strategy("Test", "TEST")
    rs.rules = [rules_mod.Rule(side="BUY", conditions=[rules_mod.RuleCondition(feature="close", op="<", value=1e12)])]
    store.strategies["Test"] = rs
    store.active = "Test"
    rules_mod.save_store(settings, store)


def _store_sell_all(tmp_path, settings) -> None:
    """Seed a strategy with an always-true SELL rule (close > -1e12)."""
    store = rules_mod.load_store(settings)
    rs = rules_mod.empty_strategy("Test", "TEST")
    rs.rules = [rules_mod.Rule(side="SELL", conditions=[rules_mod.RuleCondition(feature="close", op=">", value=-1e12)])]
    store.strategies["Test"] = rs
    store.active = "Test"
    rules_mod.save_store(settings, store)


def _falling_df(n=260) -> pd.DataFrame:
    """A steadily declining market (short trades should profit)."""
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz="America/New_York")
    px = (100.0 * (1.0 - 0.0008) ** pd.Series(range(n))).to_numpy()
    return pd.DataFrame(
        {"open": px * 0.999, "high": px * 1.001, "low": px * 0.998, "close": px, "volume": [1e6] * n},
        index=idx,
    )


def _rising_df(n=260, start="2024-01-02", step=0.0008) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="B", tz="America/New_York")
    px = (100.0 * (1.0 + step) ** pd.Series(range(n))).to_numpy()
    return pd.DataFrame(
        {"open": px * 0.999, "high": px * 1.001, "low": px * 0.998, "close": px, "volume": [1e6] * n},
        index=idx,
    )


# ---------------------------------------------------------------------------
# engine: simulation
# ---------------------------------------------------------------------------
def test_simulate_known_trade_no_cost():
    opens = [100.0, 101.0, 102.0, 103.0, 104.0]
    closes = list(opens)
    sig = ["BUY", "HOLD", "SELL", "HOLD", "HOLD"]
    returns, trades, in_pos, _ = simulate_frame(opens, closes, sig, 0.0, 0.0)
    # enter open1=101 -> exit open3=103
    assert returns == pytest.approx([0.0, 102.0 / 101.0 - 1, 103.0 / 102.0 - 1, 0.0])
    assert len(trades) == 1
    assert trades[0]["ret"] == pytest.approx(103.0 / 101.0 - 1)
    assert in_pos == 2


def test_simulate_costs_reduce_pnl():
    opens = [100.0, 101.0, 102.0, 103.0, 104.0]
    sig = ["BUY", "HOLD", "SELL", "HOLD", "HOLD"]
    _, trades, _, _ = simulate_frame(opens, opens, sig, slippage=0.005, commission=0.001)
    assert trades[0]["ret"] < 103.0 / 101.0 - 1


def test_simulate_flat_when_no_signal():
    opens = [100.0, 101.0, 102.0]
    returns, trades, in_pos, _ = simulate_frame(opens, opens, ["HOLD", "HOLD", "HOLD"], 0.0, 0.0)
    assert returns == [0.0, 0.0]
    assert trades == []
    assert in_pos == 0


# ---------------------------------------------------------------------------
# engine: short positions (allow_short)
# ---------------------------------------------------------------------------
def test_simulate_short_known_trade_no_cost():
    opens = [100.0, 99.0, 98.0, 97.0, 96.0]
    closes = list(opens)
    sig = ["SELL", "HOLD", "BUY", "HOLD", "HOLD"]
    returns, trades, in_pos, _ = simulate_frame(opens, closes, sig, 0.0, 0.0, allow_short=True)
    # Short opens at open1=99 (SELL at bar 0) and closes at open3=97 (BUY at bar 2).
    assert returns == pytest.approx([0.0, 99.0 / 98.0 - 1.0, 98.0 / 97.0 - 1.0, 0.0])
    assert len(trades) == 1
    assert trades[0]["direction"] == "short"
    assert trades[0]["ret"] == pytest.approx(99.0 / 97.0 - 1.0)
    assert in_pos == 2


def test_position_intervals_single_position_at_a_time():
    # BUY@0 -> long at 1; SELL@1 closes long at 2; SELL@2 (flat) opens a short
    # at 3; BUY@3 closes it at 4. Never two positions at once.
    sig = ["BUY", "SELL", "SELL", "BUY", "HOLD"]
    assert position_intervals(sig, 5, allow_short=True) == [(1, 2, "long"), (3, 4, "short")]


def test_position_intervals_ignores_opposing_repeats():
    # BUY while long and SELL while short are ignored, so a short can only
    # open AFTER the long closed.
    sig = ["BUY", "BUY", "SELL", "SELL", "HOLD"]
    assert position_intervals(sig, 5, allow_short=True) == [(1, 3, "long"), (4, 5, "short")]


def test_position_intervals_default_ignores_flat_sell():
    # With shorting off a SELL while flat is ignored — long/flat only.
    sig = ["SELL", "BUY", "HOLD"]
    assert position_intervals(sig, 3, allow_short=False) == [(2, 3, "long")]


def test_simulate_short_costs_reduce_pnl():
    opens = [100.0, 99.0, 98.0, 97.0, 96.0]
    sig = ["SELL", "HOLD", "BUY", "HOLD", "HOLD"]
    _, trades, _, _ = simulate_frame(opens, opens, sig, slippage=0.005, commission=0.001, allow_short=True)
    assert trades[0]["direction"] == "short"
    assert trades[0]["ret"] < 99.0 / 97.0 - 1.0


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def test_metrics_total_and_drawdown():
    times = [pd.Timestamp("2024-01-0%d" % (i + 1)) for i in range(2)]
    m = metrics.compute_metrics([0.2, -0.25], times, [], periods_per_year=252.0)
    assert m["total_return_pct"] == pytest.approx(-10.0)
    assert m["max_drawdown_pct"] == pytest.approx(25.0)
    assert m["num_trades"] == 0


def test_metrics_trade_stats():
    trades = [
        {"ret": 0.02}, {"ret": -0.01}, {"ret": 0.03}, {"ret": -0.02},  # 4 closed
    ]
    m = metrics.compute_metrics([0.0], [pd.Timestamp("2024-01-01")], trades, periods_per_year=252.0)
    assert m["num_trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate_pct"] == pytest.approx(50.0)
    assert m["profit_factor"] == pytest.approx(0.05 / 0.03)


def test_gate_result(tmp_path):
    # Deterministic thresholds (the repo .env may override the defaults).
    settings = _settings(
        tmp_path,
        gate_min_sharpe=1.0,
        gate_max_drawdown_percent=25.0,
        gate_min_win_rate_percent=55.0,
        gate_max_weekly_loss_percent=5.0,
    )
    m = {
        "sharpe": 1.5, "max_drawdown_pct": 10.0, "win_rate_pct": 60.0,
        "worst_week_pct": -3.0,
    }
    g = metrics.gate_result(settings, m)
    assert g["pass"] is True
    m2 = dict(m, sharpe=0.5)
    assert metrics.gate_result(settings, m2)["pass"] is False


# ---------------------------------------------------------------------------
# engine: run_backtest end-to-end (temp store, synthetic data)
# ---------------------------------------------------------------------------
def test_run_backtest_buy_and_hold(tmp_path):
    settings = _settings(tmp_path)
    _store_buy_all(tmp_path, settings)
    df = _rising_df()
    res = run_backtest(settings, dataset=df)
    assert res["ok"] is True
    assert res["signals"].get("BUY", 0) == len(df)
    m = res["metrics"]
    assert m["num_trades"] == 1
    assert m["total_return_pct"] > 0
    assert len(res["equity_curve"]) > 10
    assert m["win_rate_pct"] == 100.0  # single round-trip in a rising market
    assert "pass" in res["gate"] and "checks" in res["gate"]


def _same_day_candles(n=8, freq="1h") -> pd.DataFrame:
    """Flat bars all inside ONE day, each low enough to hit a 2% stop.

    The circuit breaker groups by day, so this is the shape that can actually
    trip it: several trades sharing a day, each stopped out."""
    idx = pd.date_range("2024-01-02 09:00", periods=n, freq=freq, tz="America/New_York")
    close = np.full(n, 100.0)
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 3.0, "close": close,
         "volume": [1e6] * n},
        index=idx,
    )


def test_run_backtest_requires_rule_based(tmp_path):
    settings = _settings(tmp_path, model_type="logistic_regression")
    _store_buy_all(tmp_path, settings)
    res = run_backtest(settings, dataset=_rising_df(10))
    assert res["ok"] is False
    assert "rule_based" in (res["error"] or "")


def test_run_backtest_empty_dataset(tmp_path):
    settings = _settings(tmp_path)
    _store_buy_all(tmp_path, settings)
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    res = run_backtest(settings, dataset=empty)
    assert res["ok"] is False


def test_run_backtest_short_profitable_when_allowed(tmp_path):
    settings = _settings(tmp_path, allow_short=True)
    _store_sell_all(tmp_path, settings)
    df = _falling_df()
    res = run_backtest(settings, dataset=df)
    assert res["ok"] is True
    assert res["signals"].get("SELL", 0) == len(df)
    m = res["metrics"]
    assert m["num_trades"] == 1
    assert res["trades"] and res["trades"][0]["side"] == "short"
    assert m["total_return_pct"] > 0
    assert m["win_rate_pct"] == 100.0


def test_run_backtest_shorts_disabled_by_default(tmp_path):
    settings = _settings(tmp_path)  # allow_short defaults to False
    _store_sell_all(tmp_path, settings)
    df = _falling_df()
    res = run_backtest(settings, dataset=df)
    assert res["ok"] is True
    # SELL while flat is ignored -> no position at all in a short-only strategy.
    assert res["metrics"]["num_trades"] == 0
    assert res["metrics"]["total_return_pct"] == pytest.approx(0.0)
    assert res["trades"] == []


# ---------------------------------------------------------------------------
# web service (monkeypatched so no real engine run)
# ---------------------------------------------------------------------------
def test_backtest_service_run_and_payload(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from src.web.services import backtest_service

    backtest_service._RESULTS.clear()
    calls = {}

    def fake_settings():
        return SimpleNamespace(
            instrument="TEST",
            historical_bar_size="1d",
            model_type="rule_based",
            historical_data_dir=str(tmp_path / "historical"),
        )

    def fake_run(settings):
        calls["settings"] = settings
        # Mirror real engine output: numpy scalars must be normalized before the
        # API (FastAPI strict ``dict``) and the persisted JSON file see them.
        return {
            "ok": True,
            "metrics": {"num_trades": np.float64(3)},
            "gate": {"pass": np.bool_(False), "checks": {}},
            "equity_curve": [{"time": "2024-01-02", "equity": np.float64(1.0)}],
            "trades": [],
            "notes": [],
        }

    monkeypatch.setattr(backtest_service, "get_effective_settings", fake_settings)
    monkeypatch.setattr(backtest_service, "active_strategy_name", lambda: "Test")
    monkeypatch.setattr(backtest_service, "run_backtest", fake_run)

    # No recorded run yet -> the active strategy has no result (guide shows),
    # and the panel cannot offer a report either.
    assert backtest_service.payload()["result"] is None
    assert backtest_service.payload()["run_count"] == 0

    started = backtest_service.start_backtest()
    assert started["started"] is True

    for _ in range(100):
        p = backtest_service.payload()
        if not p["status"]["running"]:
            break
        time.sleep(0.02)
    assert calls and p["result"]["ok"] is True
    assert p["result"]["metrics"]["num_trades"] == 3
    # numpy scalars were normalized -> payload is plain JSON types.
    assert type(p["result"]["gate"]["pass"]) is bool
    assert p["result"]["gate"]["pass"] is False
    assert p["result"]["equity_curve"][0]["equity"] == 1.0

    # Successful runs are persisted per strategy under backtest_results/<slug>/.
    latest_file = tmp_path / "backtest_results" / "Test" / "latest.json"
    assert latest_file.exists()
    latest = _json.loads(latest_file.read_text(encoding="utf-8"))
    assert latest["ok"] is True
    assert latest["strategy"] == "Test"
    assert latest["metrics"]["num_trades"] == 3
    assert latest["gate"]["pass"] is False
    assert latest["schema_version"] == backtest_service.SCHEMA_VERSION
    assert latest["run_id"] == p["result"]["run_id"]
    assert latest["generated_at"]  # stamped at persist time

    # ...and the full run is kept alongside it under runs/.
    run_files = list((tmp_path / "backtest_results" / "Test" / "runs").glob("*.json"))
    assert len(run_files) == 1
    assert run_files[0].stem == latest["run_id"]

    # A fresh process (empty in-memory cache) reloads the same result on demand.
    backtest_service._RESULTS.clear()
    assert backtest_service.payload()["result"]["metrics"]["num_trades"] == 3
    # One stored run -> the panel shows the "Open full report" button.
    assert backtest_service.payload()["run_count"] == 1


def test_backtest_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from src.web.app import app
    from src.web.services import backtest_service

    client = TestClient(app)
    payload = {
        "status": {"running": False, "last_error": None, "last_run": None},
        "result": {"ok": True, "metrics": {"num_trades": 0}, "gate": {"pass": True}, "equity_curve": [], "trades": [], "notes": []},
    }
    monkeypatch.setattr(backtest_service, "payload", lambda: payload)
    r = client.get("/api/v1/backtest")
    assert r.status_code == 200
    assert r.json()["result"]["metrics"]["num_trades"] == 0


# ---------------------------------------------------------------------------
# storage layout (src/backtest/store.py)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Alpha – AAPL", "Alpha-AAPL"),  # typographic en-dash
        ("Delta – NVDA - 1h", "Delta-NVDA-1h"),
        ("Epsilon – AMZN – 1d", "Epsilon-AMZN-1d"),
        ("Café/Über\\Long", "Cafe-Uber-Long"),  # accents + separators fold to '-'
        ("  ..  ", "strategy"),  # never empty
    ],
)
def test_slug_is_filesystem_and_url_safe(raw, expected):
    from src.backtest.store import slug

    assert slug(raw) == expected
    assert slug(raw).isascii()
    assert not set(slug(raw)) & set(' <>:"/\\|?*')  # no shell/URL-hostile chars


def test_run_id_is_sortable_and_input_sensitive():
    from src.backtest import store

    t1 = datetime(2026, 9, 10, 11, 29, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 11, 30, tzinfo=timezone.utc)
    inputs = {"allow_short": False, "window": {"rows": 100}}

    first = store.new_run_id(inputs, when=t1)
    assert first.startswith("20260910T112900Z-")
    # Chronological order is lexicographic order, so runs/ sorts naturally.
    assert store.new_run_id(inputs, when=t1) < store.new_run_id(inputs, when=t2)
    # Same inputs -> same hash; anything different -> a different id.
    assert store.new_run_id(inputs, when=t1) == first
    assert store.new_run_id({**inputs, "allow_short": True}, when=t1) != first


def test_ui_view_trims_for_panel_but_run_file_keeps_everything(tmp_path, monkeypatch):
    """The panel view is bounded; the report source on disk is not."""
    import json as _json
    from types import SimpleNamespace

    from src.web.services import backtest_service

    backtest_service._RESULTS.clear()

    def fake_settings():
        return SimpleNamespace(
            instrument="TEST",
            historical_bar_size="1d",
            model_type="rule_based",
            historical_data_dir=str(tmp_path / "historical"),
        )

    def fake_run(settings):
        return {
            "ok": True,
            "inputs": {"allow_short": False, "window": {"rows": 700}},
            "metrics": {"num_trades": 250},
            "gate": {"pass": True, "checks": {}},
            "equity_curve": [{"time": f"t{i}", "equity": 1.0} for i in range(700)],
            "trades": [{"i": i} for i in range(250)],
            "notes": [],
        }

    monkeypatch.setattr(backtest_service, "get_effective_settings", fake_settings)
    monkeypatch.setattr(backtest_service, "active_strategy_name", lambda: "Test")
    monkeypatch.setattr(backtest_service, "run_backtest", fake_run)

    backtest_service.start_backtest()
    for _ in range(100):
        if not backtest_service.payload()["status"]["running"]:
            break
        time.sleep(0.02)

    strategy_dir = tmp_path / "backtest_results" / "Test"
    latest = _json.loads((strategy_dir / "latest.json").read_text(encoding="utf-8"))
    run_file = next((strategy_dir / "runs").glob("*.json"))
    run = _json.loads(run_file.read_text(encoding="utf-8"))

    # The panel view is bounded, and says so.
    assert len(latest["equity_curve"]) <= backtest_service.MAX_CURVE_POINTS
    assert len(latest["trades"]) == backtest_service.MAX_TRADE_ROWS
    assert latest["view"] == {
        "curve_points": len(latest["equity_curve"]),
        "curve_points_total": 700,
        "trade_rows": backtest_service.MAX_TRADE_ROWS,
        "trade_rows_total": 250,
        "truncated": True,
        "full_run": f"runs/{run['run_id']}.json",
    }
    # The final equity is exact even after downsampling.
    assert latest["equity_curve"][-1] == run["equity_curve"][-1]

    # The run file keeps every point and every trade for the report to use.
    assert len(run["equity_curve"]) == 700
    assert len(run["trades"]) == 250
    assert "view" not in run
    assert run["run_id"] == latest["run_id"] == run_file.stem


def test_legacy_flat_result_still_loads(tmp_path, monkeypatch):
    """Results written before the per-strategy layout keep working."""
    import json as _json
    from types import SimpleNamespace

    from src.web.services import backtest_service

    root = tmp_path / "backtest_results"
    root.mkdir()
    (root / "Legacy.json").write_text(
        _json.dumps({"ok": True, "metrics": {"num_trades": 7}}), encoding="utf-8"
    )

    def fake_settings():
        return SimpleNamespace(
            instrument="TEST",
            historical_bar_size="1d",
            model_type="rule_based",
            historical_data_dir=str(tmp_path / "historical"),
        )

    backtest_service._RESULTS.clear()
    monkeypatch.setattr(backtest_service, "get_effective_settings", fake_settings)
    monkeypatch.setattr(backtest_service, "active_strategy_name", lambda: "Legacy")

    assert backtest_service.payload()["result"]["metrics"]["num_trades"] == 7


def test_run_backtest_records_provenance(tmp_path):
    """A run must say what it tested, so a result is reproducible."""
    settings = _settings(
        tmp_path, backtest_slippage_percent=0.1, backtest_commission_per_trade=0.001
    )
    _store_buy_all(tmp_path, settings)
    res = run_backtest(settings, dataset=_rising_df())

    inputs = res["inputs"]
    window = inputs["window"]
    assert window["instrument"] == "TEST"
    assert window["bar_size"] == "1d"
    assert window["rows"] == res["rows"]
    # The window records the data actually replayed (start <= end, both stamped).
    assert window["start"].startswith("2024-01-02")
    assert window["end"] > window["start"]
    assert window["periods_per_year"] > 0
    assert window["backtest_start_date"] is None
    assert window["backtest_end_date"] is None
    assert inputs["model_type"] == "rule_based"
    assert inputs["allow_short"] is False
    # The rule set is pinned (and hashed) so the verdict is attributable.
    assert [r["side"] for r in inputs["rules"]] == ["BUY"]
    assert re.fullmatch(r"[0-9a-f]{12}", inputs["rules_hash"])
    # Costs are recorded as fractions, exactly as the simulation used them.
    assert inputs["costs"]["slippage"] == pytest.approx(0.001)
    assert inputs["costs"]["commission"] == pytest.approx(0.001)
    # Settings are captured, minus anything credential-shaped.
    assert inputs["settings"]["instrument"] == "TEST"
    assert not any(
        re.search(r"key|token|secret|password|credential", k, re.IGNORECASE)
        for k in inputs["settings"]
    )
    # The execution environment is stamped as well, so a stored run can always be
    # attributed to paper or live (their results legitimately differ). Exactly
    # these keys — never a credential.
    assert set(inputs["execution"]) == {"broker", "env", "live", "base_url", "configured"}
    assert inputs["execution"]["broker"] == "alpaca"
    assert inputs["execution"]["env"] == "paper"
    assert inputs["execution"]["live"] is False

    # Identical inputs -> identical hash (so a re-run is recognisable).
    again = run_backtest(settings, dataset=_rising_df())
    assert again["inputs"]["rules_hash"] == inputs["rules_hash"]
