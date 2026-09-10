"""Tests for the Features Engineering module (src/features).

Pure pandas math on synthetic frames — no network, no .env dependence (the
feature settings are supplied explicitly to Settings).
"""

import numpy as np
import pandas as pd
import pytest

from src.config.settings import Settings
from src.features import indicators
from src.features.engineering import FeatureEngineer, compute_features, latest_features
from src.features.schema import active_feature_columns


def _settings(**over):
    base = dict(
        features_sma_periods="10,20",
        features_rsi_period=14,
        features_atr_period=14,
        features_bollinger_period=20,
        features_bollinger_std=2.0,
        features_momentum_periods="10,20",
        features_volatility_period=20,
        features_vwap_period=20,
        features_volume_period=20,
        features_min_lookback=50,
        feature_sma_enabled=True,
        feature_rsi_enabled=True,
        feature_atr_enabled=True,
        feature_bollinger_enabled=True,
        feature_momentum_enabled=True,
        feature_volatility_enabled=True,
        feature_vwap_enabled=True,
        feature_volume_enabled=True,
        feature_volume_abs_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def _rising_frame(n=120):
    """Strictly rising closes -> high/low band of +/-2, business-day index."""
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


def _flat_frame(n=120):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.full(n, 150.0)
    return pd.DataFrame(
        {"open": close, "high": close + 2.0, "low": close - 2.0, "close": close,
         "volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------
def test_sma_matches_manual():
    s = pd.Series([10.0, 12.0, 15.0, 14.0])
    out = indicators.sma(s, 2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == pytest.approx(11.0)
    assert out.iloc[2] == pytest.approx(13.5)
    assert out.iloc[3] == pytest.approx(14.5)


def test_momentum_matches_manual():
    s = pd.Series([100.0, 110.0, 121.0])
    out = indicators.momentum(s, 2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[2] == pytest.approx(0.21)  # 121/100 - 1


def test_rsi_pure_up_move_is_100():
    df = _rising_frame()
    out = indicators.rsi(df["close"], 14)
    assert out.iloc[:13].isna().all()  # warmup
    tail = out.iloc[13:].dropna()
    assert (tail > 99.0).all()


def test_rsi_flat_series_is_neutral_50():
    df = _flat_frame()
    out = indicators.rsi(df["close"], 14).dropna()
    assert out.iloc[-1] == pytest.approx(50.0)


def test_atr_positive_and_finite():
    df = _rising_frame()
    out = indicators.atr(df["high"], df["low"], df["close"], 14).dropna()
    assert (out > 0).all()
    assert out.iloc[-1] == pytest.approx(4.0, rel=0.05)  # high-low band is 4


def test_bollinger_pctb_finite_after_warmup():
    df = _rising_frame()
    out = indicators.bollinger_pctb(df["close"], 20, 2.0)
    assert out.iloc[:19].isna().all()
    assert out.iloc[19:].notna().all()


def test_rolling_vol_nonnegative():
    df = _rising_frame()
    out = indicators.rolling_vol(df["close"], 20).dropna()
    assert (out >= 0).all()


def test_vwap_matches_manual():
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame(
        {
            "high": [10.0, 12.0, 14.0],
            "low": [8.0, 10.0, 12.0],
            "close": [9.0, 11.0, 13.0],
            "volume": [100.0, 300.0, 100.0],
        },
        index=idx,
    )
    # typical price = (H+L+C)/3 = 9, 11, 13
    # vwap(3) = (9*100 + 11*300 + 13*100) / (100+300+100) = 11.0
    out = indicators.vwap(df["high"], df["low"], df["close"], df["volume"], 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(11.0)


def test_volume_ratio_matches_manual():
    s = pd.Series([100.0, 200.0, 300.0, 50.0])
    out = indicators.volume_ratio(s, 2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == pytest.approx(200.0 / 150.0)  # vs mean(100,200)
    assert out.iloc[2] == pytest.approx(1.2)  # 300 / mean(200,300)
    assert out.iloc[3] == pytest.approx(50.0 / 175.0)  # vs mean(300,50)


def test_vwap_constant_volume_equals_sma():
    """With uniform volume, rolling VWAP collapses to the SMA of typical price.
    On the rising fixture typical price == close, so vwap_20 == sma_20."""
    df = _rising_frame()
    vwap = indicators.vwap(df["high"], df["low"], df["close"], df["volume"], 20)
    sma = indicators.sma(df["close"], 20)
    tail = ~sma.isna()
    assert (vwap[tail] - sma[tail]).abs().max() < 1e-9


# ---------------------------------------------------------------------------
# engineering
# ---------------------------------------------------------------------------
def test_feature_columns_follow_toggles():
    st = _settings(feature_rsi_enabled=False, feature_volatility_enabled=False)
    cols = active_feature_columns(st)
    assert "rsi_14" not in cols and "vol_20" not in cols
    assert "sma_10" in cols and "sma_20" in cols
    assert "atr_14" in cols and "bb_pctb_20" in cols
    assert "mom_10" in cols and "mom_20" in cols
    assert cols == FeatureEngineer(st).feature_columns


def test_compute_frame_columns_and_index():
    st = _settings()
    df = _rising_frame()
    feat = FeatureEngineer(st).compute_frame(df)
    assert list(feat.columns) == active_feature_columns(st)
    assert (feat.index == df.index).all()


def test_features_finite_after_warmup():
    st = _settings()
    df = _rising_frame()
    feat = FeatureEngineer(st).compute_frame(df)
    # Indicator columns are NaN through warmup; raw absolute volume is not an
    # indicator, so it is present from the very first bar.
    indicator_cols = [c for c in feat.columns if c != "volume_abs"]
    assert feat[indicator_cols].iloc[:9].isna().any(axis=1).all()
    assert feat["volume_abs"].iloc[:9].notna().all()
    assert feat.iloc[80:].notna().all().all()  # long after warmup: all finite


def test_compute_latest_equals_frame_last_row():
    st = _settings()
    df = _rising_frame()
    eng = FeatureEngineer(st)
    frame = eng.compute_frame(df)
    latest = eng.compute_latest(df)
    for col in frame.columns:
        expected = float(frame[col].iloc[-1])
        if pd.isna(expected):
            assert latest[col] is None
        else:
            assert latest[col] == pytest.approx(expected)


def test_no_lookahead_prefix_identity():
    """Feature value at bar t must be identical whether computed on the full
    series or only on data up to and including t."""
    st = _settings()
    df = _rising_frame()
    eng = FeatureEngineer(st)
    full = eng.compute_frame(df)
    for t in (30, 80, 100):
        prefix = eng.compute_frame(df.iloc[: t + 1])
        for col in full.columns:
            a, b = full[col].iloc[t], prefix[col].iloc[-1]
            if pd.isna(a) and pd.isna(b):
                continue
            assert a == pytest.approx(b, abs=1e-9)


def test_disabled_all_features_empty_frame():
    st = _settings(
        feature_sma_enabled=False, feature_rsi_enabled=False, feature_atr_enabled=False,
        feature_bollinger_enabled=False, feature_momentum_enabled=False,
        feature_volatility_enabled=False, feature_vwap_enabled=False,
        feature_volume_enabled=False, feature_volume_abs_enabled=False,
    )
    df = _rising_frame()
    feat = FeatureEngineer(st).compute_frame(df)
    assert list(feat.columns) == []
    assert len(feat) == len(df)


def test_vwap_and_volume_features_in_frame():
    st = _settings()
    df = _rising_frame()
    feat = FeatureEngineer(st).compute_frame(df)
    assert list(feat.columns) == active_feature_columns(st)
    assert "vwap_20" in feat.columns and "vratio_20" in feat.columns
    # constant volume on the rising fixture -> relative volume == 1 after warmup
    assert feat["vratio_20"].iloc[80] == pytest.approx(1.0)
    # and vwap_20 == sma_20 (typical price == close with uniform volume)
    assert feat["vwap_20"].iloc[80] == pytest.approx(feat["sma_20"].iloc[80])


def test_vwap_and_volume_follow_toggles():
    st = _settings(feature_vwap_enabled=False, feature_volume_enabled=False,
                   feature_volume_abs_enabled=False)
    cols = active_feature_columns(st)
    assert "vwap_20" not in cols and "vratio_20" not in cols
    assert "volume_abs" not in cols
    st2 = _settings(feature_vwap_enabled=True, feature_volume_enabled=True,
                    feature_volume_abs_enabled=True)
    cols2 = active_feature_columns(st2)
    assert "vwap_20" in cols2 and "vratio_20" in cols2
    assert "volume_abs" in cols2


def test_absolute_volume_feature_equals_raw_volume():
    st = _settings()
    df = _rising_frame()
    feat = FeatureEngineer(st).compute_frame(df)
    assert "volume_abs" in feat.columns
    assert (feat["volume_abs"] == df["volume"]).all()
    assert feat["volume_abs"].iloc[0] == pytest.approx(1_000_000.0)


def test_module_level_shortcuts_match_class():
    st = _settings()
    df = _rising_frame()
    assert compute_features(st, df).equals(FeatureEngineer(st).compute_frame(df))
    assert latest_features(st, df) == FeatureEngineer(st).compute_latest(df)


def test_not_ready_for_short_history():
    st = _settings()
    eng = FeatureEngineer(st)
    assert eng.is_ready(_rising_frame(200)) is True
    assert eng.is_ready(_rising_frame(10)) is False
    assert eng.compute_latest(_rising_frame(5)) != {}
