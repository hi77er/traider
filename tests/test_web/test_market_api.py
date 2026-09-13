"""Tests for the market landing-page API endpoints.

The screener data layer is monkeypatched, so no network is hit.
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.data import screener as sc
from src.web.app import app
from src.web.services import market_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_cache():
    """The overview is cached in module state — keep tests independent."""
    market_service.refresh()
    yield
    market_service.refresh()


def _frame(symbols, **over):
    rows = []
    for index, symbol in enumerate(symbols):
        row = {
            "symbol": symbol,
            "name": f"{symbol} Inc.",
            "price": 10.0 + index,
            "change_percent": 1.0 * index,
            "volume": 1_000 * (index + 1),
            "avg_volume_3m": 500,
            "market_cap": 1_000_000_000,
            "dollar_volume": 1_000_000.0,
            "exchange": "NMS",
            "exchange_name": "NasdaqGS",
            "sector": "Technology",
            "industry": "Software",
            "currency": "USD",
        }
        row.update(over)
        rows.append(row)
    return pd.DataFrame(rows)[sc.COLUMNS]


def _patch_run(monkeypatch, frame=None, calls=None):
    def fake_run(spec, size=25, offset=0):
        if calls is not None:
            calls.append(spec.key)
        if frame is not None:
            return frame
        return _frame([f"{spec.key.upper()[:4]}1"])
    monkeypatch.setattr(sc, "run_screen", fake_run)


# ── overview ────────────────────────────────────────────────────────────
def test_overview_returns_every_panel_in_order(monkeypatch):
    _patch_run(monkeypatch)
    r = client.get("/api/v1/market/overview", params={"size": 5})
    assert r.status_code == 200
    body = r.json()
    assert [p["key"] for p in body["panels"]] == sc.PANEL_ORDER
    assert body["errors"] == []
    assert body["cached"] is False
    assert body["generated_at"]
    for panel in body["panels"]:
        assert panel["count"] == len(panel["rows"]) == 1
        assert panel["criteria"]
        assert panel["error"] is None
        assert panel["rows"][0]["symbol"]


def test_overview_isolates_a_failing_panel(monkeypatch):
    def fake_run(spec, size=25, offset=0):
        if spec.key == "top_losers":
            raise sc.ScreenerError("yahoo said no")
        return _frame(["AAPL"])

    monkeypatch.setattr(sc, "run_screen", fake_run)
    body = client.get("/api/v1/market/overview").json()

    failed = [p for p in body["panels"] if p["key"] == "top_losers"][0]
    assert failed["error"] == "yahoo said no"
    assert failed["rows"] == []
    # The other panels still render.
    ok = [p for p in body["panels"] if p["key"] != "top_losers"]
    assert ok and all(p["count"] == 1 for p in ok)
    assert body["errors"] == ["Top Losers: yahoo said no"]


def test_overview_second_call_is_served_from_cache(monkeypatch):
    calls = []
    _patch_run(monkeypatch, calls=calls)
    first = client.get("/api/v1/market/overview").json()
    after_first = len(calls)
    second = client.get("/api/v1/market/overview").json()

    assert len(calls) == after_first  # no extra provider calls
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]


def test_overview_force_bypasses_the_cache(monkeypatch):
    calls = []
    _patch_run(monkeypatch, calls=calls)
    client.get("/api/v1/market/overview")
    after_first = len(calls)
    client.get("/api/v1/market/overview", params={"force": True})
    assert len(calls) > after_first


def test_overview_size_is_validated(monkeypatch):
    _patch_run(monkeypatch)
    assert client.get("/api/v1/market/overview", params={"size": 0}).status_code == 422
    assert client.get("/api/v1/market/overview", params={"size": 9999}).status_code == 422


def test_overview_makes_nan_json_safe(monkeypatch):
    frame = _frame(["AAPL"], market_cap=float("nan"), avg_volume_3m=float("inf"))
    _patch_run(monkeypatch, frame=frame)
    r = client.get("/api/v1/market/overview")
    assert r.status_code == 200
    assert b"NaN" not in r.content and b"Infinity" not in r.content
    row = r.json()["panels"][0]["rows"][0]
    assert row["market_cap"] is None
    assert row["avg_volume_3m"] is None


# ── single panel / paging ───────────────────────────────────────────────
def test_panel_endpoint_passes_size_and_offset(monkeypatch):
    seen = {}

    def fake_run(spec, size=25, offset=0):
        seen["key"] = spec.key
        seen["size"] = size
        seen["offset"] = offset
        return _frame(["AAPL"])

    monkeypatch.setattr(sc, "run_screen", fake_run)
    body = client.get(
        "/api/v1/market/panel/whole_market", params={"size": 10, "offset": 30}
    ).json()
    assert (seen["key"], seen["size"], seen["offset"]) == ("whole_market", 10, 30)
    assert body["offset"] == 30
    assert body["count"] == 1


def test_panel_endpoint_unknown_key_is_404():
    r = client.get("/api/v1/market/panel/not_a_panel")
    assert r.status_code == 404
    assert "not_a_panel" in r.json()["detail"]


# ── presets ─────────────────────────────────────────────────────────────
def test_presets_endpoint_lists_options(monkeypatch):
    monkeypatch.setattr(
        sc, "list_presets",
        lambda: [{"key": "day_gainers", "label": "Day gainers"},
                 {"key": "day_losers", "label": "Day losers"}],
    )
    body = client.get("/api/v1/market/presets").json()
    assert [p["key"] for p in body["presets"]] == ["day_gainers", "day_losers"]
    assert body["default"] == sc.DEFAULT_PRESET
    assert body["max_rows"] == sc.MAX_PRESET_ROWS


# ── screener ────────────────────────────────────────────────────────────
def test_screen_endpoint_builds_the_spec_from_query_params(monkeypatch):
    captured = {}

    def fake_run(spec, size=25, offset=0):
        captured["spec"] = spec
        captured["size"] = size
        return _frame(["KEEP"])

    monkeypatch.setattr(sc, "run_screen", fake_run)
    monkeypatch.setattr(sc, "list_presets", lambda: [{"key": "small_cap_gainers", "label": "Small cap gainers"}])

    body = client.get(
        "/api/v1/market/screen",
        params={
            "preset": "small_cap_gainers",
            "size": 10,
            "market_cap_min": 300_000_000,
            "market_cap_max": 2_000_000_000,
            "min_price": 1.5,
            "min_volume": 250_000,
        },
    ).json()

    spec = captured["spec"]
    assert spec.preset == "small_cap_gainers"
    assert spec.market_cap_min == 300_000_000
    assert spec.market_cap_max == 2_000_000_000
    assert spec.min_price == 1.5
    assert spec.min_volume == 250_000
    assert body["preset"] == "small_cap_gainers"
    assert body["count"] == 1
    # The advertised criteria must mention the filters that were actually applied.
    assert "market cap" in body["criteria"]
    assert "sorted by" not in body["criteria"]


def test_screen_endpoint_clamps_size_to_the_preset_window(monkeypatch):
    captured = {}
    monkeypatch.setattr(sc, "list_presets", lambda: [{"key": "day_gainers", "label": "Day gainers"}])

    def fake_run(spec, size=25, offset=0):
        captured["size"] = size
        return _frame(["A"])

    monkeypatch.setattr(sc, "run_screen", fake_run)
    client.get("/api/v1/market/screen", params={"preset": "day_gainers", "size": 250})
    assert captured["size"] == sc.MAX_PRESET_ROWS


def test_screen_endpoint_rejects_an_unknown_preset(monkeypatch):
    monkeypatch.setattr(sc, "list_presets", lambda: [{"key": "day_gainers", "label": "Day gainers"}])
    r = client.get("/api/v1/market/screen", params={"preset": "nonsense"})
    assert r.status_code == 200
    body = r.json()
    assert "nonsense" in body["error"]
    assert body["rows"] == []
    assert body["presets"] == ["day_gainers"]


def test_screen_endpoint_defaults_to_the_default_preset(monkeypatch):
    captured = {}

    def fake_run(spec, size=25, offset=0):
        captured["preset"] = spec.preset
        return _frame(["A"])

    monkeypatch.setattr(sc, "run_screen", fake_run)
    monkeypatch.setattr(sc, "list_presets", lambda: [{"key": sc.DEFAULT_PRESET, "label": "Day gainers"}])
    client.get("/api/v1/market/screen")
    assert captured["preset"] == sc.DEFAULT_PRESET


def test_screen_endpoint_failure_is_reported(monkeypatch):
    def boom(spec, size=25, offset=0):
        raise sc.ScreenerError("rate limited")

    monkeypatch.setattr(sc, "run_screen", boom)
    monkeypatch.setattr(sc, "list_presets", lambda: [{"key": "day_gainers", "label": "Day gainers"}])
    body = client.get("/api/v1/market/screen", params={"preset": "day_gainers"}).json()
    assert body["error"] == "rate limited"
    assert body["rows"] == []


# ── refresh ─────────────────────────────────────────────────────────────
def test_refresh_clears_the_cache(monkeypatch):
    calls = []
    _patch_run(monkeypatch, calls=calls)
    client.get("/api/v1/market/overview")
    assert client.get("/api/v1/market/overview").json()["cached"] is True

    assert client.post("/api/v1/market/refresh").json()["ok"] is True
    assert client.get("/api/v1/market/overview").json()["cached"] is False


# ── page route ──────────────────────────────────────────────────────────
def test_market_page_is_served():
    r = client.get("/market")
    assert r.status_code == 200
    assert "Top Screener" in r.text
    assert "/static/market.js" in r.text


def test_screener_card_ships_collapsed():
    """The screener body must be hidden in the HTML, so it is closed on first paint."""
    html = client.get("/market").text
    assert '<section class="card collapsible" id="mkt-screener">' in html
    assert '<div class="collapse-body" id="scr-body" hidden>' in html
    assert "toggleMarketCard('screener')" in html


def test_market_js_collapses_both_long_cards_by_default():
    js = client.get("/static/market.js").text
    assert 'COLLAPSIBLE_CARDS = ["screener", "whole_market"]' in js
    assert "collapsed: { screener: true, whole_market: true }" in js
    # Paging a collapsed table must expand it rather than fetch invisibly.
    assert "state.collapsed[key] = false;" in js
