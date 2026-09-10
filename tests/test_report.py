"""Tests for the backtest report: analytics, run store, and report API.

All offline: reports are built from synthetic run dicts and a temp store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.backtest import report as report_mod
from src.backtest import store as bt_store


def _run(points, trades=None, metrics=None, run_id="20260910T120000Z-aaaabbbb"):
    """A stored-run-shaped dict with an explicit equity curve."""
    return {
        "ok": True,
        "schema_version": 2,
        "run_id": run_id,
        "generated_at": "2026-09-10T12:00:00+00:00",
        "strategy": "Test",
        "symbol": "TEST",
        "bar_size": "1d",
        "model_type": "rule_based",
        "rows": len(points),
        "start": points[0][0],
        "end": points[-1][0],
        "metrics": metrics or {"total_return_pct": 10.0, "sharpe": 1.5, "num_trades": len(trades or [])},
        "gate": {"pass": True, "checks": {}},
        "inputs": {
            "settings": {"market_timezone": "America/New_York"},
            "rules": [{"side": "BUY"}],
            "rules_hash": "deadbeef0000",
            "allow_short": False,
            "costs": {"slippage": 0.0005, "commission": 0.0},
        },
        "equity_curve": [{"time": d, "equity": e} for d, e in points],
        "benchmark_curve": [],
        "trades": trades or [],
        "notes": [],
        "signals": {"BUY": 3, "SELL": 2, "HOLD": 1},
        "rule_stats": {"evaluated": 1, "skipped": 0},
    }


def _trade(ret_pct, bars=5, side="long", reason=None):
    row = {
        "i": 1, "side": side, "entry_time": "2024-01-02", "exit_time": "2024-01-09",
        "entry_price": 100.0, "exit_price": 101.0, "ret_pct": ret_pct, "bars": bars,
    }
    if reason is not None:
        row["exit_reason"] = reason
    return row


# ---------------------------------------------------------------------------
# period returns
# ---------------------------------------------------------------------------
def test_monthly_and_yearly_returns_compound_from_the_curve():
    run = _run([("2024-01-31", 1.10), ("2024-02-29", 1.21), ("2024-03-15", 1.10)])
    rep = report_mod.build_report(run)

    assert [m["key"] for m in rep["monthly"]] == ["2024-01", "2024-02", "2024-03"]
    assert rep["monthly"][0]["ret_pct"] == pytest.approx(10.0)
    assert rep["monthly"][1]["ret_pct"] == pytest.approx(10.0)
    assert rep["monthly"][2]["ret_pct"] == pytest.approx((1.10 / 1.21 - 1) * 100)
    assert rep["monthly"][0]["label"] == "Jan 2024"

    # A year row compounds its months back to the whole-year return.
    assert len(rep["yearly"]) == 1
    assert rep["yearly"][0]["year"] == 2024
    assert rep["yearly"][0]["ret_pct"] == pytest.approx(10.0)


def test_monthly_matrix_pivots_years_and_months():
    run = _run([
        ("2023-12-29", 1.05),
        ("2024-01-31", 1.10),
        ("2024-02-29", 1.00),
        ("2025-01-31", 1.20),
    ])
    matrix = report_mod.build_report(run)["monthly_matrix"]

    assert [row["year"] for row in matrix["rows"]] == [2023, 2024, 2025]
    # Months are keyed by number and only the covered ones are present.
    assert matrix["rows"][0]["months"] == {"12": pytest.approx(5.0)}
    assert set(matrix["rows"][1]["months"]) == {"1", "2"}
    # 2024 = 1.10/1.05 - 1 then 1.00/1.10 - 1.
    assert matrix["rows"][1]["total_pct"] == pytest.approx((1.00 / 1.05 - 1) * 100)
    assert matrix["month_labels"][0] == "Jan"


def test_period_returns_handles_intraday_unix_times():
    """Intraday curves use UTC unix seconds, bucketed in market time."""
    ts = int(datetime(2024, 3, 1, 18, 0, tzinfo=timezone.utc).timestamp())
    ts2 = int(datetime(2024, 4, 1, 18, 0, tzinfo=timezone.utc).timestamp())
    rep = report_mod.build_report(_run([(ts, 1.0), (ts2, 1.25)]))

    assert [m["key"] for m in rep["monthly"]] == ["2024-03", "2024-04"]
    assert rep["monthly"][1]["ret_pct"] == pytest.approx(25.0)


def test_period_returns_ignores_unparseable_times():
    run = _run([("not-a-date", 1.0), ("2024-01-31", 1.1)])
    assert [m["key"] for m in report_mod.build_report(run)["monthly"]] == ["2024-01"]


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------
def test_drawdown_series_and_stats():
    run = _run([
        ("2024-01-05", 1.0),
        ("2024-01-06", 1.20),
        ("2024-01-07", 1.08),   # -10% off the peak
        ("2024-01-08", 1.20),   # recovered
        ("2024-01-09", 1.30),
    ])
    rep = report_mod.build_report(run)

    dd = [round(d["dd_pct"], 4) for d in rep["drawdown"]]
    assert dd == [0.0, 0.0, pytest.approx(-10.0), 0.0, 0.0]
    assert rep["drawdown_stats"]["max_dd_pct"] == pytest.approx(-10.0)
    assert rep["drawdown_stats"]["max_dd_time"] == "2024-01-07"
    assert rep["drawdown_stats"]["recovered"] is True
    assert rep["drawdown_stats"]["current_dd_pct"] == pytest.approx(0.0)


def test_drawdown_stats_reports_longest_underwater_stretch():
    run = _run([
        ("2024-01-05", 1.0),
        ("2024-01-06", 1.10),
        ("2024-01-07", 1.04),   # underwater
        ("2024-01-08", 1.02),   # underwater
        ("2024-01-09", 1.10),   # recovered
        ("2024-01-10", 1.09),   # underwater again
    ])
    stats = report_mod.build_report(run)["drawdown_stats"]
    assert stats["longest_bars"] == 2
    assert stats["longest_end"] == "2024-01-08"
    assert stats["recovered"] is False


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------
def test_trade_distribution_buckets_and_stats():
    trades = [_trade(r) for r in (-12.0, -6.0, -3.0, -0.5, 0.5, 1.5, 3.0, 7.0, 12.0)]
    dist = report_mod.build_report(_run([("2024-01-05", 1.0)], trades=trades))["distribution"]

    assert dist["total"] == 9
    counts = {b["label"]: b["count"] for b in dist["buckets"]}
    assert counts["< -10%"] == 1
    assert counts["-10% … -5%"] == 1
    assert counts["-1% … 0%"] == 1
    assert counts["0% … +1%"] == 1
    assert counts["> +10%"] == 1
    # Every trade lands in exactly one bucket.
    assert sum(b["count"] for b in dist["buckets"]) == 9
    assert sum(b["pct"] for b in dist["buckets"]) == pytest.approx(100.0)
    assert dist["best_pct"] == pytest.approx(12.0)
    assert dist["worst_pct"] == pytest.approx(-12.0)
    assert dist["median_pct"] == pytest.approx(0.5)


def test_trade_extras_counts_streaks_sides_and_hold_times():
    trades = [
        _trade(2.0, bars=4),
        _trade(3.0, bars=6),
        _trade(-1.0, bars=2, side="short"),
        _trade(-2.0, bars=2, side="short"),
        _trade(-0.5, bars=3),
        _trade(5.0, bars=10),
    ]
    stats = report_mod.build_report(_run([("2024-01-05", 1.0)], trades=trades))["trade_stats"]

    assert stats["count"] == 6
    assert stats["wins"] == 3 and stats["losses"] == 3
    assert stats["longs"] == 4 and stats["shorts"] == 2
    assert stats["max_win_streak"] == 2
    assert stats["max_loss_streak"] == 3
    assert stats["avg_win_pct"] == pytest.approx((2.0 + 3.0 + 5.0) / 3)
    assert stats["avg_loss_pct"] == pytest.approx(-(1.0 + 2.0 + 0.5) / 3)
    assert stats["avg_bars"] == pytest.approx((4 + 6 + 2 + 2 + 3 + 10) / 6)
    assert stats["avg_win_bars"] == pytest.approx((4 + 6 + 10) / 3)
    assert stats["total_bars_in_market"] == 27


# ---------------------------------------------------------------------------
# assembly + robustness
# ---------------------------------------------------------------------------
def test_build_report_survives_an_empty_or_legacy_run():
    rep = report_mod.build_report({})
    assert rep["monthly"] == [] and rep["trades"] == [] and rep["equity_curve"] == []
    assert rep["distribution"]["total"] == 0
    assert rep["drawdown_stats"]["max_dd_pct"] == 0.0
    assert rep["benchmark"]["total_return_pct"] is None


def test_exit_breakdown_groups_trades_by_how_the_risk_layer_ended_them():
    trades = (
        [_trade(-2.0, bars=1, reason="stop") for _ in range(3)]
        + [_trade(4.0, bars=3, reason="take") for _ in range(2)]
        + [_trade(1.5, bars=4, reason="signal")]
    )
    out = report_mod.exit_breakdown(trades)

    assert out["total"] == 6
    assert [r["reason"] for r in out["rows"]] == ["stop", "take", "signal"]
    stop = out["rows"][0]
    assert stop["count"] == 3
    assert stop["pct"] == pytest.approx(50.0)
    assert stop["wins"] == 0 and stop["win_rate_pct"] == 0.0
    assert stop["avg_ret_pct"] == pytest.approx(-2.0)
    assert stop["avg_bars"] == 1.0
    take = out["rows"][1]
    assert take["count"] == 2 and take["win_rate_pct"] == 100.0
    assert take["total_ret_pct"] == pytest.approx(8.0)
    # Empty reasons are dropped rather than listed as zero rows.
    assert "forced" not in [r["reason"] for r in out["rows"]]


def test_exit_breakdown_falls_back_for_a_run_without_reasons():
    """A run recorded before the risk layer has no reason column -> "signal"."""
    out = report_mod.exit_breakdown([_trade(1.0), _trade(-1.0)])
    assert [r["reason"] for r in out["rows"]] == ["signal"]
    assert out["rows"][0]["count"] == 2
    assert report_mod.exit_breakdown([]) == {"total": 0, "rows": []}


def test_report_exposes_the_risk_layer_settings_and_outcomes():
    run = _run([("2024-01-05", 1.0), ("2024-01-06", 1.05)], [_trade(-2.0, reason="stop")])
    run["inputs"]["risk"] = {
        "applied": True, "weight": 0.9, "risk_limit_percent": 2.0,
        "stop_loss_percent": 2.0, "take_profit_percent": 4.0,
        "max_exposure_percent": 90.0, "breaker_skips": 2, "breaker_trips": 1,
        "stop_exits": 1, "take_exits": 0,
    }
    run["risk_events"] = {
        "applied": True,
        "vetoed": [{"time": "2024-01-03", "side": "long", "reason": "circuit_breaker"}],
    }

    risk = report_mod.build_report(run)["risk"]
    assert risk["applied"] is True
    assert risk["weight"] == pytest.approx(0.9)
    assert risk["stop_exits"] == 1
    assert risk["breaker_skips"] == 2
    assert risk["vetoed"] == [
        {"time": "2024-01-03", "side": "long", "reason": "circuit_breaker"}
    ]


def test_report_risk_block_survives_a_run_without_risk_data():
    """Older runs have neither inputs.risk nor risk_events — no crash, empty vetoes."""
    report = report_mod.build_report(_run([("2024-01-05", 1.0)], [_trade(1.0)]))
    assert report["risk"] == {"vetoed": []}
    assert report["exits"]["rows"][0]["reason"] == "signal"


def test_benchmark_excess_return_is_derived_when_present():
    run = _run([("2024-01-05", 1.0), ("2024-01-06", 1.5)])
    run["benchmark_curve"] = [{"time": "2024-01-05", "equity": 1.0}, {"time": "2024-01-06", "equity": 1.2}]
    run["metrics"] = {"total_return_pct": 50.0}

    bench = report_mod.build_report(run)["benchmark"]
    assert bench["total_return_pct"] == pytest.approx(20.0)
    assert bench["excess_return_pct"] == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# run store: index, listing, de-duplication
# ---------------------------------------------------------------------------
def _settings(tmp_path):
    return SimpleNamespace(historical_data_dir=str(tmp_path / "historical"))


def test_save_run_writes_full_run_and_index(tmp_path):
    settings = _settings(tmp_path)
    run = _run([("2024-01-05", 1.0), ("2024-01-06", 1.10)])
    bt_store.save_run(settings, "Test", run, {"ok": True, "trimmed": True})

    runs = bt_store.list_runs(settings, "Test")
    assert len(runs) == 1
    entry = runs[0]
    assert entry["run_id"] == run["run_id"]
    assert entry["gate_pass"] is True
    assert entry["sharpe"] == pytest.approx(1.5)
    assert entry["rules_hash"] == "deadbeef0000"
    assert entry["num_rules"] == 1

    # The full run is on disk untouched; latest.json holds the panel view.
    assert bt_store.load_run(settings, "Test", run["run_id"])["equity_curve"] == run["equity_curve"]
    latest = bt_store.load_latest(settings, "Test")
    assert latest["trimmed"] is True


def test_rerunning_identical_inputs_replaces_the_previous_run(tmp_path):
    settings = _settings(tmp_path)
    first = _run([("2024-01-05", 1.0)], run_id="20260910T120000Z-aaaabbbb")
    second = _run([("2024-01-05", 1.0)], run_id="20260911T090000Z-aaaabbbb")  # same inputs hash

    bt_store.save_run(settings, "Test", first)
    bt_store.save_run(settings, "Test", second)

    files = sorted(p.name for p in bt_store.runs_dir(settings, "Test").glob("*.json"))
    assert files == ["20260911T090000Z-aaaabbbb.json"]
    assert [r["run_id"] for r in bt_store.list_runs(settings, "Test")] == ["20260911T090000Z-aaaabbbb"]
    # The index must not keep a record for the superseded run either.
    assert len(bt_store._load_index(settings, "Test")) == 1


def test_index_does_not_grow_when_runs_are_superseded(tmp_path):
    """Repeated re-runs of one config leave exactly one file AND one index record."""
    settings = _settings(tmp_path)
    for stamp in ("20260910T120000Z", "20260910T130000Z", "20260910T140000Z"):
        bt_store.save_run(settings, "Test", _run([("2024-01-05", 1.0)], run_id=f"{stamp}-aaaabbbb"))

    assert len(list(bt_store.runs_dir(settings, "Test").glob("*.json"))) == 1
    assert len(bt_store._load_index(settings, "Test")) == 1
    assert len(bt_store.list_runs(settings, "Test")) == 1

    # A genuinely different config adds a record rather than replacing one.
    bt_store.save_run(settings, "Test", _run([("2024-01-05", 1.0)], run_id="20260910T150000Z-ccccdddd"))
    assert len(bt_store.list_runs(settings, "Test")) == 2


def test_list_runs_is_newest_first_and_survives_a_missing_index(tmp_path):
    settings = _settings(tmp_path)
    older = _run([("2024-01-05", 1.0)], run_id="20260910T120000Z-aaaa0000")
    newer = _run([("2024-01-05", 1.0)], run_id="20260911T120000Z-bbbb1111")
    bt_store.save_run(settings, "Test", older)
    bt_store.save_run(settings, "Test", newer)

    assert [r["run_id"] for r in bt_store.list_runs(settings, "Test")] == [
        "20260911T120000Z-bbbb1111",
        "20260910T120000Z-aaaa0000",
    ]

    # A hand-copied run file with no index entry still shows up.
    stray = bt_store.run_path(settings, "Test", "20260912T120000Z-cccc2222")
    stray.write_text(json.dumps(_run([("2024-01-05", 1.0)], run_id="20260912T120000Z-cccc2222")), encoding="utf-8")
    listed = bt_store.list_runs(settings, "Test")
    assert listed[0]["run_id"] == "20260912T120000Z-cccc2222"


def test_list_runs_drops_entries_whose_file_is_gone(tmp_path):
    settings = _settings(tmp_path)
    run = _run([("2024-01-05", 1.0)])
    bt_store.save_run(settings, "Test", run)
    bt_store.run_path(settings, "Test", run["run_id"]).unlink()
    assert bt_store.list_runs(settings, "Test") == []


def test_load_run_rejects_unknown_ids(tmp_path):
    settings = _settings(tmp_path)
    assert bt_store.load_run(settings, "Test", "nope") is None
    assert bt_store.load_run(settings, "Test", "") is None


# ---------------------------------------------------------------------------
# report API
# ---------------------------------------------------------------------------
def _client(tmp_path, monkeypatch, runs):
    from fastapi.testclient import TestClient

    from src.web.app import app
    from src.web.services import report_service

    settings = _settings(tmp_path)
    for run in runs:
        bt_store.save_run(settings, "Test", run)
    monkeypatch.setattr(report_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(report_service, "active_strategy_name", lambda: "Test")
    return TestClient(app)


def test_report_runs_endpoint_lists_the_menu(tmp_path, monkeypatch):
    older = _run([("2024-01-05", 1.0)], run_id="20260910T120000Z-aaaa0000")
    newer = _run([("2024-01-05", 1.0)], run_id="20260911T120000Z-bbbb1111")
    client = _client(tmp_path, monkeypatch, [older, newer])

    body = client.get("/api/v1/report/runs?strategy=Test").json()
    assert body["strategy"] == "Test"
    assert body["error"] is None
    assert [r["run_id"] for r in body["runs"]] == [
        "20260911T120000Z-bbbb1111",
        "20260910T120000Z-aaaa0000",
    ]


def test_report_run_defaults_to_the_newest_and_honours_an_explicit_id(tmp_path, monkeypatch):
    older = _run([("2024-01-05", 1.0), ("2024-01-06", 1.2)], run_id="20260910T120000Z-aaaa0000")
    newer = _run([("2024-01-05", 1.0), ("2024-01-06", 1.5)], run_id="20260911T120000Z-bbbb1111")
    client = _client(tmp_path, monkeypatch, [older, newer])

    default = client.get("/api/v1/report/run?strategy=Test").json()
    assert default["run_id"] == "20260911T120000Z-bbbb1111"  # newest
    assert default["report"]["monthly"][0]["ret_pct"] == pytest.approx(50.0)

    chosen = client.get("/api/v1/report/run?strategy=Test&run_id=20260910T120000Z-aaaa0000").json()
    assert chosen["run_id"] == "20260910T120000Z-aaaa0000"
    assert chosen["report"]["monthly"][0]["ret_pct"] == pytest.approx(20.0)
    # The menu always travels with the report so the page can render the selector.
    assert len(chosen["runs"]) == 2


def test_report_run_errors_are_reported_not_raised(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, [])

    empty = client.get("/api/v1/report/run?strategy=Test").json()
    assert empty["report"] is None
    assert "No backtest runs" in empty["error"]

    older = _run([("2024-01-05", 1.0)], run_id="20260910T120000Z-aaaa0000")
    newer = _run([("2024-01-05", 1.0), ("2024-01-06", 1.2)], run_id="20260911T120000Z-bbbb1111")
    client = _client(tmp_path, monkeypatch, [older, newer])

    # A stale run id (e.g. a bookmarked URL after a re-run superseded it) falls
    # back to the newest run and says so, rather than showing nothing.
    stale = client.get("/api/v1/report/run?strategy=Test&run_id=ghost").json()
    assert stale["run_id"] == "20260911T120000Z-bbbb1111"
    assert stale["report"] is not None
    assert "ghost" in stale["error"]


def test_report_page_is_served(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, [])
    r = client.get("/report")
    assert r.status_code == 200
    assert "Backtest report" in r.text


def test_report_page_shows_provenance_collapsed_near_the_top(tmp_path, monkeypatch):
    """The "What was tested" panel is collapsible, collapsed, and above the charts."""
    client = _client(tmp_path, monkeypatch, [])
    html = client.get("/report").text

    assert 'id="rp-what-toggle"' in html
    assert 'id="rp-what-body" hidden' in html  # collapsed by default
    # It sits above the result panels (it used to be the last card).
    assert html.index('id="rp-what-card"') < html.index('id="rp-equity"')
    assert html.index('id="rp-what-card"') < html.index('id="rp-monthly"')
    assert html.index('id="rp-what-card"') < html.index('id="rp-trades"')
    # ...but below the run/verdict summary, which keeps the report's identity first.
    assert html.index('id="rp-kpis"') < html.index('id="rp-what-card"')


def test_report_page_shows_the_risk_layer_collapsed(tmp_path, monkeypatch):
    """The risk layer panel collapses like "What was tested" and starts collapsed."""
    client = _client(tmp_path, monkeypatch, [])
    html = client.get("/report").text

    assert 'id="rp-risk-toggle"' in html
    assert 'id="rp-risk-body" hidden' in html  # collapsed by default
    assert 'id="rp-risk-card"' in html
    # Its content lives inside the collapse body, so nothing shows until expanded.
    body_start = html.index('id="rp-risk-body"')
    assert html.index('id="rp-risk-note"') > body_start
    assert html.index('id="rp-exits"') > body_start
    assert html.index('id="rp-vetoed"') > body_start


# ---------------------------------------------------------------------------
# deleting a stored run
# ---------------------------------------------------------------------------
def test_delete_run_removes_file_index_and_report_output(tmp_path):
    settings = _settings(tmp_path)
    run = _run([("2024-01-05", 1.0)])
    bt_store.save_run(settings, "Test", run)
    artefact = bt_store.reports_dir(settings, "Test", run["run_id"])
    artefact.mkdir(parents=True)
    (artefact / "report.json").write_text("{}", encoding="utf-8")

    result = bt_store.delete_run(settings, "Test", run["run_id"])

    assert result["deleted"] is True
    assert not bt_store.run_path(settings, "Test", run["run_id"]).exists()
    assert not artefact.exists()  # the generator's output goes too
    assert bt_store._load_index(settings, "Test") == []
    assert bt_store.list_runs(settings, "Test") == []


def test_delete_run_is_quiet_about_unknown_ids(tmp_path):
    settings = _settings(tmp_path)
    assert bt_store.delete_run(settings, "Test", "ghost")["deleted"] is False
    assert bt_store.delete_run(settings, "Test", "")["deleted"] is False


def test_delete_report_endpoint_removes_the_run_and_repoints_the_panel(tmp_path, monkeypatch):
    older = _run([("2024-01-05", 1.0)], run_id="20260910T120000Z-aaaa0000")
    newer = _run([("2024-01-05", 1.0)], run_id="20260911T120000Z-bbbb1111")
    client = _client(tmp_path, monkeypatch, [older, newer])
    settings = _settings(tmp_path)

    body = client.delete(
        "/api/v1/report/run?strategy=Test&run_id=20260911T120000Z-bbbb1111"
    ).json()

    assert body["ok"] is True
    assert body["deleted"] == "20260911T120000Z-bbbb1111"
    assert body["runs"] and [r["run_id"] for r in body["runs"]] == ["20260910T120000Z-aaaa0000"]
    # The panel must not keep showing the run that was just deleted.
    assert bt_store.load_latest(settings, "Test")["run_id"] == older["run_id"]
    assert (
        client.get("/api/v1/report/run?strategy=Test").json()["run_id"] == older["run_id"]
    )


def test_delete_report_endpoint_clears_the_panel_when_the_last_run_goes(tmp_path, monkeypatch):
    run = _run([("2024-01-05", 1.0)])
    client = _client(tmp_path, monkeypatch, [run])
    settings = _settings(tmp_path)

    body = client.delete(f"/api/v1/report/run?strategy=Test&run_id={run['run_id']}").json()

    assert body["ok"] is True
    assert body["runs"] == []
    assert body["message"]  # explains how to get a report back
    assert not bt_store.latest_path(settings, "Test").exists()
    assert client.get("/api/v1/report/run?strategy=Test").json()["report"] is None


def test_delete_report_endpoint_reports_a_missing_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, [])
    body = client.delete("/api/v1/report/run?strategy=Test&run_id=ghost").json()
    assert body["ok"] is False
    assert body["deleted"] is None
    assert "ghost" in body["message"]
