"""Tests for the market screener data layer.

No network: ``yfinance.screen`` is monkeypatched and the curl_cffi session
setup is stubbed out.
"""

import pandas as pd
import pytest
from yfinance import EquityQuery

from src.data import screener as sc


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep the session patch and any HTTP out of the unit tests."""
    monkeypatch.setattr(sc, "apply_yfinance_session", lambda: True)


def _quote(symbol, **over):
    row = {
        "symbol": symbol,
        "shortName": f"{symbol} Inc.",
        "regularMarketPrice": 10.0,
        "regularMarketChangePercent": 5.0,
        "regularMarketVolume": 1_000_000,
        "averageDailyVolume3Month": 500_000,
        "marketCap": 1_000_000_000,
        "exchange": "NMS",
        "fullExchangeName": "NasdaqGS",
        "currency": "USD",
    }
    row.update(over)
    return row


def _fake_screen(quotes, captured=None):
    def fake(query=None, **kwargs):
        if captured is not None:
            captured.update(kwargs)
            captured["query"] = query
        return {"quotes": quotes}
    return fake


# ── query construction ──────────────────────────────────────────────────
def test_build_query_single_clause_is_not_wrapped_in_and():
    """`and` needs >= 2 operands — a one-filter screen must return that filter."""
    spec = sc.ScreenSpec(key="x", label="X", description="", us_only=True, min_price=0)
    query = sc.build_query(spec)
    assert query.to_dict() == {"operator": "EQ", "operands": ["region", "us"]}


def test_build_query_combines_us_region_cap_band_and_floors():
    spec = sc.ScreenSpec(
        key="x",
        label="X",
        description="",
        us_only=True,
        market_cap_min=sc.SMALL_CAP_MIN,
        market_cap_max=sc.SMALL_CAP_MAX,
        min_price=1.0,
        min_volume=250_000,
    )
    payload = sc.build_query(spec).to_dict()
    assert payload["operator"] == "AND"
    clauses = [tuple(c["operands"]) for c in payload["operands"]]
    assert ("region", "us") in clauses
    assert ("intradaymarketcap", sc.SMALL_CAP_MIN) in clauses
    assert ("intradaymarketcap", sc.SMALL_CAP_MAX) in clauses
    assert ("intradayprice", 1.0) in clauses
    assert ("dayvolume", 250_000) in clauses


def test_build_query_with_no_filters_is_still_valid():
    spec = sc.ScreenSpec(key="x", label="X", description="", us_only=False, min_price=0)
    assert sc.build_query(spec).to_dict()["operator"] == "GT"


def test_small_cap_panels_use_the_small_cap_band():
    for key in ("small_cap_gainers", "small_cap_volume"):
        spec = sc.get_spec(key)
        assert spec.market_cap_min == sc.SMALL_CAP_MIN
        assert spec.market_cap_max == sc.SMALL_CAP_MAX


def test_panel_order_covers_every_panel():
    assert set(sc.PANEL_ORDER) == set(sc.PANELS)
    # The landing page expects the whole market first.
    assert sc.PANEL_ORDER[0] == "whole_market"


def test_get_spec_unknown_panel_raises():
    with pytest.raises(sc.ScreenerError):
        sc.get_spec("nope")


# ── normalization ───────────────────────────────────────────────────────
def test_canonical_sort_maps_screener_fields_to_columns():
    """Regression: screener sort fields are not response field names."""
    assert sc._canonical_sort("percentchange") == "change_percent"
    assert sc._canonical_sort("dayvolume") == "volume"
    assert sc._canonical_sort("intradaymarketcap") == "market_cap"
    assert sc._canonical_sort("unknown") == "market_cap"


def test_normalize_maps_fields_and_computes_dollar_volume():
    spec = sc.get_spec("whole_market")
    frame = sc.normalize([_quote("AAPL", regularMarketPrice=10.0, regularMarketVolume=2_000_000)], spec)
    assert list(frame.columns) == sc.COLUMNS
    row = frame.iloc[0]
    assert row["symbol"] == "AAPL"
    assert row["name"] == "AAPL Inc."
    assert row["price"] == 10.0
    assert row["change_percent"] == 5.0
    assert row["dollar_volume"] == 20_000_000
    assert row["avg_volume_3m"] == 500_000


def test_normalize_drops_otc_and_keeps_the_us_primary_tape():
    spec = sc.get_spec("whole_market")
    frame = sc.normalize(
        [_quote("GOOD"), _quote("PINKY", exchange="PNK"), _quote("LONDON", exchange="LSE")],
        spec,
    )
    assert frame["symbol"].tolist() == ["GOOD"]


def test_normalize_can_keep_every_exchange():
    spec = sc.ScreenSpec(key="x", label="X", description="", us_only=False)
    frame = sc.normalize([_quote("GOOD"), _quote("PINKY", exchange="PNK")], spec)
    assert sorted(frame["symbol"]) == ["GOOD", "PINKY"]


def test_normalize_sorts_gainers_by_percent_change_desc():
    """Regression: top gainers were silently ordered by market cap."""
    spec = sc.get_spec("top_gainers")
    frame = sc.normalize(
        [
            _quote("SMALL", regularMarketChangePercent=40.0, marketCap=1_000_000),
            _quote("BIG", regularMarketChangePercent=10.0, marketCap=900_000_000),
        ],
        spec,
    )
    assert frame["symbol"].tolist() == ["SMALL", "BIG"]


def test_normalize_sorts_losers_ascending():
    spec = sc.get_spec("top_losers")
    frame = sc.normalize(
        [
            _quote("MILD", regularMarketChangePercent=-2.0),
            _quote("BRUTAL", regularMarketChangePercent=-30.0),
        ],
        spec,
    )
    assert frame["symbol"].tolist() == ["BRUTAL", "MILD"]


def test_normalize_sorts_volume_panels_by_volume():
    spec = sc.get_spec("highest_volume")
    frame = sc.normalize(
        [
            _quote("QUIET", regularMarketVolume=1_000_000, marketCap=900_000_000),
            _quote("BUSY", regularMarketVolume=90_000_000, marketCap=1_000_000),
        ],
        spec,
    )
    assert frame["symbol"].tolist() == ["BUSY", "QUIET"]


def test_normalize_drops_rows_without_a_price():
    spec = sc.get_spec("whole_market")
    frame = sc.normalize([_quote("OK"), _quote("NOPRICE", regularMarketPrice=None)], spec)
    assert frame["symbol"].tolist() == ["OK"]


def test_normalize_empty_input_returns_the_schema():
    frame = sc.normalize([], sc.get_spec("whole_market"))
    assert frame.empty
    assert list(frame.columns) == sc.COLUMNS


def test_normalize_respects_the_limit():
    spec = sc.get_spec("whole_market")
    frame = sc.normalize([_quote(f"S{i}") for i in range(10)], spec, limit=3)
    assert len(frame) == 3


# ── execution ───────────────────────────────────────────────────────────
def test_run_screen_passes_sort_and_caps_size_at_yahoo_limit(monkeypatch):
    captured = {}
    monkeypatch.setattr("yfinance.screen", _fake_screen([_quote("AAPL")], captured))
    sc.run_screen(sc.get_spec("top_gainers"), size=9999, offset=10)
    assert captured["sortField"] == "percentchange"
    assert captured["sortAsc"] is False
    assert captured["size"] == sc.MAX_PAGE_SIZE
    assert captured["offset"] == 10


def test_run_screen_losers_sort_ascending(monkeypatch):
    captured = {}
    monkeypatch.setattr("yfinance.screen", _fake_screen([_quote("X")], captured))
    sc.run_screen(sc.get_spec("top_losers"), size=5)
    assert captured["sortAsc"] is True


def test_run_preset_keeps_the_providers_own_ranking(monkeypatch):
    """Yahoo ranks presets server-side; we must not re-sort them."""
    rows = [
        _quote("FIRST", regularMarketChangePercent=1.0, marketCap=10_000),
        _quote("SECOND", regularMarketChangePercent=99.0, marketCap=9_000_000_000),
    ]
    monkeypatch.setattr("yfinance.screen", _fake_screen(rows))
    spec = sc.get_preset_spec("day_gainers", min_price=0)
    frame = sc.run_screen(spec, size=10)
    assert frame["symbol"].tolist() == ["FIRST", "SECOND"]


def test_run_preset_applies_local_filters_and_limit(monkeypatch):
    rows = [
        _quote("KEEP", marketCap=1_000_000_000),
        _quote("DROP", marketCap=90_000_000_000),
    ]
    monkeypatch.setattr("yfinance.screen", _fake_screen(rows))
    spec = sc.get_preset_spec(
        "day_gainers", market_cap_max=sc.SMALL_CAP_MAX, min_price=0
    )
    frame = sc.run_screen(spec, size=1)
    assert frame["symbol"].tolist() == ["KEEP"]


def test_run_screen_wraps_provider_failures(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("yahoo says no")
    monkeypatch.setattr("yfinance.screen", boom)
    with pytest.raises(sc.ScreenerError, match="yahoo says no"):
        sc.run_screen(sc.get_spec("whole_market"), size=5)


def test_run_preset_wraps_provider_failures(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("preset down")
    monkeypatch.setattr("yfinance.screen", boom)
    with pytest.raises(sc.ScreenerError, match="preset down"):
        sc.run_screen(sc.get_preset_spec("day_gainers"), size=5)


# ── presets ─────────────────────────────────────────────────────────────
def test_preset_label_humanizes_keys():
    assert sc.preset_label("day_gainers") == "Day gainers"
    assert sc.preset_label("aggressive_small_caps") == "Aggressive small caps"


def test_list_presets_returns_key_label_pairs():
    presets = sc.list_presets()
    assert presets and all(set(p) == {"key", "label"} for p in presets)


def test_get_preset_spec_carries_the_filters():
    spec = sc.get_preset_spec(
        "small_cap_gainers", market_cap_min=100, market_cap_max=200, min_volume=7
    )
    assert spec.preset == "small_cap_gainers"
    assert (spec.market_cap_min, spec.market_cap_max) == (100, 200)
    assert spec.min_volume == 7
    # A sector filter cannot be honoured for presets, so it must not be settable.
    assert spec.sector is None


# ── criteria text ───────────────────────────────────────────────────────
def test_criteria_describes_the_applied_filters():
    text = sc.get_spec("small_cap_gainers").criteria()
    assert "market cap" in text
    assert "sorted by % change" in text
    assert "no OTC" in text


def test_criteria_for_presets_makes_no_sorting_claim():
    text = sc.get_preset_spec("day_gainers").criteria()
    assert "preset" in text
    assert "sorted by" not in text
