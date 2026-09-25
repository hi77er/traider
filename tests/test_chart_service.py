"""Tests for the chart indicators service (Option B overlays + memoization).

Uses a temp Parquet dataset and a fake-free (pure pandas) pipeline — no network.
"""

import numpy as np
import pandas as pd

from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.model import rules as R
from src.web.services import chart_service


def _settings(tmp_path, **over):
    base = dict(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="1d",
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
        features_min_lookback=50,
        feature_sma_enabled=True,
        feature_ema_enabled=True,
        feature_macd_enabled=True,
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

    # EMA rides the candles like the SMAs, one chip per configured window.
    assert "ema_9" in by_key and "ema_21" in by_key
    assert by_key["ema_21"]["label"] == "EMA 21"
    assert by_key["ema_21"]["scale"] == "price"
    assert by_key["ema_21"]["kind"] == "line"
    assert len(by_key["ema_21"]["lines"][0]["data"]) > 0

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
        feature_ema_enabled=False,
        feature_macd_enabled=False,
        feature_vwap_enabled=False,
        feature_volume_enabled=False,
        feature_volume_abs_enabled=False,
    )
    bundle = chart_service.chart_indicators(st)
    keys = {o["key"] for o in bundle["overlays"]}
    assert "rsi_14" not in keys and "sma_10" not in keys
    assert not any(k.startswith("ema_") for k in keys)
    assert not any(k.startswith("macd") for k in keys)
    assert "vwap_20" not in keys and "vratio_20" not in keys and "volume_abs" not in keys
    assert "bbands_20" in keys  # bollinger still enabled


def test_ema_toggle_recomputes_the_memoized_frame(tmp_path):
    """Flipping FEATURE_EMA_ENABLED changes the config fingerprint, so the
    memoized frame is rebuilt with the ema_* columns instead of being reused
    without them (which would KeyError while building the overlays)."""
    off = _seed(tmp_path, feature_ema_enabled=False)
    keys_off = {o["key"] for o in chart_service.chart_indicators(off)["overlays"]}
    assert not any(k.startswith("ema_") for k in keys_off)

    on = _settings(tmp_path, feature_ema_enabled=True)
    keys_on = {o["key"] for o in chart_service.chart_indicators(on)["overlays"]}
    assert "ema_9" in keys_on and "ema_21" in keys_on


def test_macd_pane_has_histogram_bars_and_two_lines(tmp_path):
    st = _seed(tmp_path, n=90)
    by_key = {o["key"]: o for o in chart_service.chart_indicators(st)["overlays"]}
    macd = by_key["macd_12_26_9"]
    assert macd["scale"] == "osc" and macd["kind"] == "line"
    assert macd["label"] == "MACD 12, 26, 9"

    # Histogram FIRST: series are created in order, so its bars paint behind the
    # two lines rather than over them.
    assert [l["name"] for l in macd["lines"]] == [
        "macd_hist_12_26_9", "macd_12_26_9", "macd_signal_12_26_9"
    ]
    hist, line, signal = macd["lines"]
    assert hist["kind"] == "histogram"
    # The two lines stay plain lines; only the histogram is a bar series.
    assert "kind" not in line and "kind" not in signal

    # MACD values are small decimals, so the `volume` format must be overridden.
    assert hist["priceFormat"] == {"type": "price", "precision": 2, "minMove": 0.01}
    # The two lines must not share a colour — their crossing is the signal.
    assert line["color"] != signal["color"]

    # Warm-up rows are dropped: the line starts 8 bars before signal/histogram.
    assert len(line["data"]) == 90 - 25
    assert len(signal["data"]) == 90 - 33
    assert len(hist["data"]) == 90 - 33

    # Every drawn bar carries its own colour, consistent with its sign.
    drawn = [p for p in hist["data"] if p.get("value") is not None]
    assert drawn
    for point in drawn:
        expected = (
            chart_service._MACD_HIST_UP_COLOR
            if point["value"] >= 0
            else chart_service._MACD_HIST_DOWN_COLOR
        )
        assert point["color"] == expected


def test_histogram_points_colour_by_sign(tmp_path):
    st = _settings(tmp_path)
    s = pd.Series(
        [1.0, -2.0, 0.0, 3.5],
        index=pd.date_range("2024-01-01", periods=4),
    )
    pts = chart_service._histogram_points(s, st, "#up", "#down")
    assert [p["color"] for p in pts] == ["#up", "#down", "#up", "#up"]  # 0 counts as up


def test_histogram_points_skip_leading_warmup_nulls(tmp_path):
    st = _settings(tmp_path)
    s = pd.Series(
        [float("nan"), float("nan"), -1.0],
        index=pd.date_range("2024-01-01", periods=3),
    )
    pts = chart_service._histogram_points(s, st, "#up", "#down")
    assert len(pts) == 1
    assert pts[0]["color"] == "#down"


def test_macd_toggle_recomputes_the_memoized_frame(tmp_path):
    """Flipping FEATURE_MACD_ENABLED must change the config fingerprint, or the
    memoized frame is reused without the macd_* columns and the overlay build
    raises KeyError."""
    off = _seed(tmp_path, feature_macd_enabled=False)
    keys_off = {o["key"] for o in chart_service.chart_indicators(off)["overlays"]}
    assert not any(k.startswith("macd") for k in keys_off)

    on = _settings(tmp_path, feature_macd_enabled=True)
    keys_on = {o["key"] for o in chart_service.chart_indicators(on)["overlays"]}
    assert "macd_12_26_9" in keys_on


def test_macd_period_change_recomputes_the_memoized_frame(tmp_path):
    """The MACD windows are part of the fingerprint: changing them must not
    reuse a frame still holding the old macd_<fast>_<slow>_<signal> columns."""
    st = _seed(
        tmp_path,
        features_macd_fast_period=12,
        features_macd_slow_period=26,
        features_macd_signal_period=9,
    )
    keys = {o["key"] for o in chart_service.chart_indicators(st)["overlays"]}
    assert "macd_12_26_9" in keys

    changed = _settings(
        tmp_path,
        features_macd_fast_period=5,
        features_macd_slow_period=35,
        features_macd_signal_period=5,
    )
    keys2 = {o["key"] for o in chart_service.chart_indicators(changed)["overlays"]}
    assert "macd_5_35_5" in keys2
    assert "macd_12_26_9" not in keys2


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


# ---------------------------------------------------------------------------
# which indicators the strategy's RULES test (the chart's chips)
# ---------------------------------------------------------------------------
def _write_store(tmp_path, *, feature="sma_20", ref=None, enabled=True, active=True):
    """A rules file with one strategy whose BUY rule tests ``feature`` (against ``ref``)."""
    store = R.StrategyStore(
        active="rules" if active else None,
        strategies={"rules": R.RuleSet(
            name="rules", instrument="AAPL",
            rules=[R.Rule(side="BUY", mode="all", enabled=enabled, conditions=[
                R.RuleCondition(feature=feature, op=">", value=32.0) if ref is None
                else R.RuleCondition(feature=feature, op=">", ref=ref),
            ])],
        )},
    )
    path = R.save_store(_settings(tmp_path, strategy_rules_file=str(tmp_path / "store.json")),
                        store)
    return str(path)


def test_only_the_indicators_the_rules_test_are_marked_used(tmp_path):
    st = _seed(tmp_path, strategy_rules_file=_write_store(tmp_path))
    bundle = chart_service.chart_indicators(st)
    used = {o["key"] for o in bundle["overlays"] if o["used"]}

    assert used == {"sma_20"}, "the rules name sma_20 and nothing else"


def test_a_rule_that_compares_two_series_marks_both(tmp_path):
    st = _seed(
        tmp_path,
        features_sma_periods="10,20,50",
        strategy_rules_file=_write_store(tmp_path, feature="close", ref="sma_50"),
    )
    used = {o["key"] for o in chart_service.chart_indicators(st)["overlays"] if o["used"]}

    # `close` is a candle, not an overlay: only the series that HAS an overlay can be marked.
    assert used == {"sma_50"}


def test_macd_is_matched_by_its_histogram_column_and_not_only_its_key(tmp_path):
    """The rules name the FEATURE column (``macd_hist_12_26_9``), the overlay is drawn as
    ``macd_...`` — a chart that matched on the key alone would offer no control for an indicator
    the strategy trades on."""
    st = _seed(tmp_path, strategy_rules_file=_write_store(tmp_path, feature="macd_hist_12_26_9"))
    by_key = {o["key"]: o for o in chart_service.chart_indicators(st)["overlays"]}

    assert by_key["macd_12_26_9"]["used"] is True
    assert sum(1 for o in by_key.values() if o["used"]) == 1


def test_a_disabled_rule_and_a_missing_store_mark_nothing(tmp_path):
    st = _seed(tmp_path, strategy_rules_file=_write_store(tmp_path, enabled=False))
    assert not any(o["used"] for o in chart_service.chart_indicators(st)["overlays"])

    chart_service.clear_cache()
    quiet = _seed(tmp_path, strategy_rules_file=str(tmp_path / "absent.json"))
    assert not any(o["used"] for o in chart_service.chart_indicators(quiet)["overlays"])


def test_a_corrupt_rules_file_never_takes_the_chart_down(tmp_path):
    """The indicators are the chart's content; `used` only decides which of them get a control.
    A half-written store must leave every series computed and every flag simply false."""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    st = _seed(tmp_path, strategy_rules_file=str(broken))
    bundle = chart_service.chart_indicators(st)

    assert bundle["overlays"], "the series are still built"
    assert not any(o["used"] for o in bundle["overlays"])

