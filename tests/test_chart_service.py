"""Tests for the chart indicators service (Option B overlays + memoization).

Uses a temp Parquet dataset and a fake-free (pure pandas) pipeline — no network.
"""

import numpy as np
import pandas as pd

from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.web.services import chart_service


def _settings(tmp_path, **over):
    base = dict(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="1d",
        features_sma_periods="10,20",
        features_rsi_period=14,
        features_atr_period=14,
        features_bollinger_period=20,
        features_bollinger_std=2.0,
        features_momentum_periods="10,20",
        features_volatility_period=20,
        features_min_lookback=50,
        feature_sma_enabled=True,
        feature_rsi_enabled=True,
        feature_atr_enabled=True,
        feature_bollinger_enabled=True,
        feature_momentum_enabled=True,
        feature_volatility_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def _frame(n=90):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.arange(n, dtype=float) + 100.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _seed(tmp_path, n=90, **over):
    st = _settings(tmp_path, **over)
    save_dataset(st, _frame(n), "AAPL", "1d")
    chart_service.clear_cache()
    return st


# ---------------------------------------------------------------------------
def test_overlays_grouped_and_complete(tmp_path):
    st = _seed(tmp_path)
    bundle = chart_service.chart_indicators(st)
    assert bundle["symbol"] == "AAPL"
    assert bundle["last_date"] is not None

    by_key = {o["key"]: o for o in bundle["overlays"]}
    assert "sma_10" in by_key and "sma_20" in by_key
    assert by_key["sma_20"]["scale"] == "price"
    assert by_key["sma_20"]["kind"] == "line"

    assert "bbands_20" in by_key
    bbands = by_key["bbands_20"]
    assert bbands["scale"] == "price" and bbands["kind"] == "bands"
    assert [l["name"] for l in bbands["lines"]] == ["upper", "mid", "lower"]

    assert "rsi_14" in by_key and by_key["rsi_14"]["scale"] == "osc"
    assert by_key["rsi_14"].get("range") == {"min": 0.0, "max": 100.0}
    assert "atr_14" in by_key and by_key["atr_14"]["scale"] == "osc"
    assert "mom_10" in by_key and "mom_20" in by_key
    assert "vol_20" in by_key and by_key["vol_20"]["scale"] == "osc"
    assert "vwap_20" in by_key and by_key["vwap_20"]["scale"] == "price"
    assert by_key["vwap_20"]["kind"] == "line"
    assert "vratio_20" in by_key and by_key["vratio_20"]["scale"] == "osc"
    assert by_key["vratio_20"]["kind"] == "line"
    assert "volume_abs" in by_key and by_key["volume_abs"]["scale"] == "osc"
    assert by_key["volume_abs"]["kind"] == "histogram"


def test_points_skip_leading_warmup_nulls(tmp_path):
    st = _seed(tmp_path, n=60)
    bundle = chart_service.chart_indicators(st)
    sma20 = next(o for o in bundle["overlays"] if o["key"] == "sma_20")
    pts = sma20["lines"][0]["data"]
    # Leading warm-up rows (SMA-20 needs 19 bars) are dropped so the series
    # starts at its first REAL value — no leading nulls that lightweight-charts
    # would render as the zero baseline.
    assert len(pts) == 60 - 19
    assert pts[0]["time"] < pts[-1]["time"]
    assert pts[0]["value"] is not None
    # last point aligned to last dataset date
    assert pts[-1]["time"] == bundle["last_date"]


def test_disabled_feature_excluded(tmp_path):
    st = _seed(
        tmp_path,
        feature_rsi_enabled=False,
        feature_sma_enabled=False,
        feature_vwap_enabled=False,
        feature_volume_enabled=False,
        feature_volume_abs_enabled=False,
    )
    bundle = chart_service.chart_indicators(st)
    keys = {o["key"] for o in bundle["overlays"]}
    assert "rsi_14" not in keys and "sma_10" not in keys
    assert "vwap_20" not in keys and "vratio_20" not in keys and "volume_abs" not in keys
    assert "bbands_20" in keys  # bollinger still enabled


def test_osc_stats_present(tmp_path):
    st = _seed(tmp_path)
    bundle = chart_service.chart_indicators(st)
    rsi = next(o for o in bundle["overlays"] if o["key"] == "rsi_14")
    assert "min" in rsi and "max" in rsi
    assert 0 <= rsi["min"] <= rsi["max"] <= 100


# ---------------------------------------------------------------------------
# memoization
# ---------------------------------------------------------------------------
def test_frame_is_memoized_and_invalidated_on_change(tmp_path, monkeypatch):
    st = _seed(tmp_path, n=90)
    calls = {"n": 0}
    orig = chart_service._build_indicator_frame

    def counting(settings, df):
        calls["n"] += 1
        return orig(settings, df)

    monkeypatch.setattr(chart_service, "_build_indicator_frame", counting)

    chart_service.chart_indicators(st)
    chart_service.chart_indicators(st)  # cached -> no recompute
    assert calls["n"] == 1

    # config change (new period) -> different fingerprint -> recompute
    st2 = _settings(tmp_path, features_sma_periods="10,20,50")
    chart_service.chart_indicators(st2)
    assert calls["n"] == 2

    # dataset change (new bar appended) -> recompute (new data fingerprint)
    extra = _frame(95).iloc[-1:]
    save_dataset(st, extra, "AAPL", "1d")
    bundle = chart_service.chart_indicators(st)
    assert bundle["last_date"] == str(extra.index[-1].date())
    assert calls["n"] == 3  # recomputed after the dataset grew


def test_clear_cache_resets(tmp_path):
    st = _seed(tmp_path)
    chart_service.chart_indicators(st)
    chart_service.clear_cache()
    assert chart_service._CACHE == {}
