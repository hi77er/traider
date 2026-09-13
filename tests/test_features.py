"""Tests for the Features Engineering module (src/features).

Pure pandas math on synthetic frames — no network, no .env dependence (the
feature settings are supplied explicitly to Settings).
"""

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.features import indicators
from src.features.engineering import FeatureEngineer, compute_features, latest_features
from src.features.schema import active_feature_columns


def _settings(**over):
    base = dict(
        features_sma_periods="10,20",
        features_ema_periods="9,21",
        features_macd_fast_period=12,
        features_macd_slow_period=26,
        features_macd_signal_period=9,
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
        feature_ema_enabled=True,
        feature_macd_enabled=True,
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


def test_ema_matches_manual():
    """EMA = recursive E[t] = a*close[t] + (1-a)*E[t-1], a = 2/(period+1)."""
    close = pd.Series([float(x) for x in range(1, 11)])
    out = indicators.ema(close, 3)
    alpha = 2.0 / (3 + 1)
    expected = [1.0]
    for v in close.iloc[1:]:
        expected.append(alpha * v + (1.0 - alpha) * expected[-1])
    assert out.iloc[:2].isna().all()  # min_periods=period blanks the warmup
    for i in range(2, len(close)):
        assert out.iloc[i] == pytest.approx(expected[i])


def test_ema_reacts_faster_than_sma():
    """After a step up, the EMA follows the new level sooner than the SMA."""
    close = pd.Series([100.0] * 30 + [120.0] * 30)
    e = indicators.ema(close, 10)
    s = indicators.sma(close, 10)
    assert e.iloc[31] > s.iloc[31]


def test_ema_feature_follows_toggle():
    st = _settings()
    cols = active_feature_columns(st)
    assert "ema_9" in cols and "ema_21" in cols
    feat = FeatureEngineer(st).compute_frame(_rising_frame())
    assert "ema_9" in feat.columns and "ema_21" in feat.columns

    off = _settings(feature_ema_enabled=False)
    assert [c for c in active_feature_columns(off) if c.startswith("ema_")] == []
    assert [c for c in FeatureEngineer(off).compute_frame(_rising_frame()).columns
            if c.startswith("ema_")] == []


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
def test_macd_matches_manual_ema_difference():
    """MACD line = EMA(fast) - EMA(slow); signal = EMA(signal) of that line."""
    close = pd.Series(np.linspace(100.0, 130.0, 60))
    out = indicators.macd(close, 12, 26, 9)

    expected_line = indicators.ema(close, 12) - indicators.ema(close, 26)
    assert np.allclose(out["macd"].dropna(), expected_line.dropna())

    expected_signal = expected_line.ewm(span=9, adjust=False, min_periods=9).mean()
    assert np.allclose(out["signal"].dropna(), expected_signal.dropna())


def test_macd_histogram_is_line_minus_signal():
    close = pd.Series(np.linspace(100.0, 130.0, 60) + np.sin(np.arange(60)))
    out = indicators.macd(close)
    assert np.allclose(out["hist"].dropna(), (out["macd"] - out["signal"]).dropna())


def test_macd_warmup_is_slow_plus_signal_minus_one():
    """The MACD line needs the slow EMA; the signal/histogram need that plus
    the signal EMA — the longest warm-up of the family."""
    out = indicators.macd(pd.Series(np.linspace(100.0, 130.0, 60)), 12, 26, 9)
    assert out["macd"].isna().sum() == 25
    assert out["signal"].isna().sum() == 33
    assert out["hist"].isna().sum() == 33
    assert pd.notna(out["hist"].iloc[33])


def test_macd_has_no_lookahead():
    """Values must not change when later bars are added, or the live decision
    would not match what the backtest saw on the same bar."""
    close = pd.Series(np.linspace(100.0, 140.0, 90) + np.sin(np.arange(90) / 3.0))
    full = indicators.macd(close, 12, 26, 9)
    truncated = indicators.macd(close.iloc[:70], 12, 26, 9)
    assert np.allclose(full["macd"].iloc[:70].dropna(), truncated["macd"].dropna())
    assert np.allclose(full["signal"].iloc[:70].dropna(), truncated["signal"].dropna())
    assert np.allclose(full["hist"].iloc[:70].dropna(), truncated["hist"].dropna())


def test_macd_is_zero_on_a_flat_series():
    """A constant price has no fast/slow spread and no signal gap."""
    out = indicators.macd(_flat_frame()["close"], 12, 26, 9)
    assert np.allclose(out["macd"].dropna(), 0.0)
    assert np.allclose(out["signal"].dropna(), 0.0)
    assert np.allclose(out["hist"].dropna(), 0.0)


def test_macd_feature_follows_toggle():
    st = _settings()
    cols = active_feature_columns(st)
    assert "macd_12_26_9" in cols
    assert "macd_signal_12_26_9" in cols
    assert "macd_hist_12_26_9" in cols

    feat = FeatureEngineer(st).compute_frame(_rising_frame())
    assert "macd_12_26_9" in feat.columns and "macd_hist_12_26_9" in feat.columns

    off = _settings(feature_macd_enabled=False)
    assert [c for c in active_feature_columns(off) if c.startswith("macd")] == []
    assert [c for c in FeatureEngineer(off).compute_frame(_rising_frame()).columns
            if c.startswith("macd")] == []


def test_macd_columns_embed_custom_periods():
    st = _settings(
        features_macd_fast_period=5,
        features_macd_slow_period=35,
        features_macd_signal_period=5,
    )
    assert "macd_5_35_5" in active_feature_columns(st)
    feat = FeatureEngineer(st).compute_frame(_rising_frame())
    assert "macd_5_35_5" in feat.columns and "macd_hist_5_35_5" in feat.columns
    assert "macd_12_26_9" not in feat.columns


def test_settings_reject_a_fast_period_that_is_not_shorter():
    """fast >= slow would silently invert the histogram (and every rule on it)."""
    with pytest.raises(ValidationError):
        _settings(features_macd_fast_period=26, features_macd_slow_period=12)
    with pytest.raises(ValidationError):
        _settings(features_macd_fast_period=12, features_macd_slow_period=12)


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
        feature_sma_enabled=False, feature_ema_enabled=False,
        feature_macd_enabled=False,
        feature_rsi_enabled=False, feature_atr_enabled=False,
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
