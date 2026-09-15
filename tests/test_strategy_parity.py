"""The guarantee: a backtest and a live run make the SAME decisions.

This is the test that makes the backtest worth reading. It replays one fixture twice —
once through the batch engine (what the backtest does) and once through
``LiveDriver.on_bar_closed`` fed a bar at a time (what a live run does) — and asserts the
two produce the same trades, at the same prices, for the same reasons.

It also pins the three things that make "the same logic" more than a promise:

* **one implementation.** Both paths call ``StrategyEngine.step``; a decision that
  reappears in a driver is caught here rather than in production.
* **the same window of data.** A live tick handed one bar (what
  ``data.live.get_latest_candle`` used to return) evaluates NaN features and reports HOLD
  for ever — a silent failure that looks like a quiet market. The driver refuses instead.
* **no lookahead in sizing.** The volatility-target mode reads bars that have CLOSED, so
  a fill cannot be sized from the close of the bar it fills in.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from src.config.settings import Settings
from src.data import live as live_data
from src.model import rules as rules_mod
from src.model.simple_model import RuleBasedSignalGenerator
from src.strategy.broker import BrokerPosition, Fill, SimulatedBroker
from src.strategy.config import StrategyConfig
from src.strategy.engine import Bar, StrategyEngine, bar_from_row
from src.strategy.live import LiveDriver, NotEnoughHistory
from src.strategy.state import StrategyState

NY = "America/New_York"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _settings(tmp_path, **kw) -> Settings:
    base = dict(
        _env_file=None,
        instrument="TEST",
        market_timezone=NY,
        trading_start_hour="00:00",         # the synthetic bars are inside this window
        trading_end_hour="23:59",
        historical_bar_size="1h",
        features_min_lookback=5,
        feature_sma_enabled=False,
        feature_ema_enabled=False,
        feature_macd_enabled=False,
        feature_rsi_enabled=False,
        feature_atr_enabled=False,
        feature_bollinger_enabled=False,
        feature_momentum_enabled=False,
        feature_volatility_enabled=False,
        feature_vwap_enabled=False,
        feature_volume_enabled=False,
        feature_volume_abs_enabled=False,
        strategy_rules_file=str(tmp_path / "store.json"),
        stop_loss_percent=3.0,
        take_profit_percent=5.0,
        risk_limit_percent=2.0,
        max_exposure_percent=100.0,
        apply_risk_layer=True,
        # The breaker is left armed but effectively unreachable here: it counts losses
        # PER DAY, and this fixture is one day, so the batch would trip it partway through
        # while a live run starting later begins with no loss history to trip on. Its own
        # parity is pinned separately (``test_the_breaker_halts_...``).
        max_consecutive_losses=99,
        max_loss_percent=99.0,
        backtest_slippage_percent=0.05,
        backtest_commission_per_trade=0.001,
        allow_short=False,
    )
    base.update(kw)
    return Settings(**base)


def _zigzag(n=90, amp=0.08, period=11) -> pd.DataFrame:
    """A market that swings enough to hit stops and takes, and to reverse."""
    px = [100.0 * (1 + amp * math.sin(2 * math.pi * i / period)) for i in range(n)]
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2026-09-14 09:00") + pd.Timedelta(hours=i) for i in range(n)]
    )
    return pd.DataFrame(
        {
            "open": px,
            "high": [p * 1.02 for p in px],
            "low": [p * 0.98 for p in px],
            "close": px,
            "volume": [1e6] * n,
        },
        index=idx,
    )


def _always_buy() -> rules_mod.RuleSet:
    return rules_mod.RuleSet(
        name="always",
        rules=[
            rules_mod.Rule(
                side="BUY",
                conditions=[rules_mod.RuleCondition(feature="close", op=">", value=0.0)],
            )
        ],
    )


def _alternating() -> rules_mod.RuleSet:
    """Buy the dips, sell the peaks — on the zigzag fixture both sides fire often.

    (Deliberately NOT a cross of close against open: the fixture's OHLC has close equal to
    open, so such a cross never fires and the test would prove nothing.)
    """
    return rules_mod.RuleSet(
        name="dips",
        rules=[
            rules_mod.Rule(
                side="BUY",
                mode="all",
                conditions=[rules_mod.RuleCondition(feature="close", op="<", value=97.0)],
            ),
            rules_mod.Rule(
                side="SELL",
                mode="all",
                conditions=[rules_mod.RuleCondition(feature="close", op=">", value=103.0)],
            ),
        ],
    )


def _generator(settings, ruleset) -> RuleBasedSignalGenerator:
    return RuleBasedSignalGenerator(settings=settings, rules=list(ruleset.rules))


def _bars(df: pd.DataFrame):
    return [
        bar_from_row(i, ts, row)
        for i, (ts, row) in enumerate(df.iterrows())
    ]


def _engine(settings, *, costs=True) -> StrategyEngine:
    cfg = StrategyConfig.from_settings(
        settings,
        enabled=bool(getattr(settings, "apply_risk_layer", True)),
        slippage=(float(settings.backtest_slippage_percent) / 100.0 if costs else 0.0),
        commission=(float(settings.backtest_commission_per_trade) if costs else 0.0),
    )
    return StrategyEngine(cfg)


def _day_keys(df: pd.DataFrame):
    return [str(ts)[:10] for ts in df.index]


# ---------------------------------------------------------------------------
# the parity guarantee
# ---------------------------------------------------------------------------
def _run_live(df, settings, gen, tmp_path, start: int, name="TEST"):
    """Replay history through the live driver: one newly closed bar at a time.

    Starts where a live run COULD start — enough trailing bars to clear the warmup the
    features need — because before that the driver refuses, which is its job: the failure
    it prevents is deciding on NaN features and reporting HOLD for ever.
    """
    driver = LiveDriver(
        settings=settings,
        engine=_engine(settings),
        generator=gen,
        broker=SimulatedBroker(),
        state_path=tmp_path / "state.json",
        name=name,
    )
    bars = _bars(df)
    for k in range(start, len(bars)):
        driver.on_bar_closed(df.iloc[:k], next_bar=bars[k])
    return driver


def _replay_both(df, settings, gen, tmp_path, start: int):
    """Run the same bars through both drivers, numbered the same way.

    The batch is handed the series that begins at ``start - 1`` — the last bar the live run
    knew about before its first decision — so both paths start FLAT, from the same history
    and with the same (empty) breaker memory, and their bars are numbered identically once
    the batch's slice is shifted back. Anything else compares a live run that started
    mid-history against a backtest that was already trading, which is a difference in when
    they began rather than in what they do.
    """
    bars = _bars(df)
    signals = [str(s) for s in gen.evaluate_frame(df)["signal"].tolist()]
    days = [str(ts)[:10] for ts in df.index]
    offset = start - 1
    # Renumber the slice from zero: the ledger's return series is indexed by the bar's
    # POSITION in the series it was given, so a slice must be numbered as its own series
    # and shifted back afterwards for the comparison.
    sliced = [
        Bar(index=i, time=b.time, open=b.open, close=b.close, high=b.high, low=b.low)
        for i, b in enumerate(bars[offset:])
    ]
    batch = _engine(settings).run(signals[offset:], sliced, days=days[offset:])

    shifted = []
    for leg in batch.legs:
        leg = dict(leg)
        leg["entry_idx"] += offset
        leg["exit_idx"] += offset
        shifted.append(leg)

    driver = _run_live(df, settings, gen, tmp_path, start)
    return shifted, list(driver.ledger.legs), driver


@pytest.mark.parametrize("ruleset_name", ["always", "alternating"])
def test_a_live_run_makes_the_same_trades_as_the_backtest(tmp_path, ruleset_name):
    """The whole point: replay history bar by bar through the live driver and get the
    backtest's trades, at the backtest's prices, for the same reasons."""
    df = _zigzag()
    settings = _settings(tmp_path)
    ruleset = _always_buy() if ruleset_name == "always" else _alternating()
    gen = _generator(settings, ruleset)
    driver_probe = LiveDriver(
        settings=settings, engine=_engine(settings), generator=gen,
        state_path=tmp_path / "state.json",
    )
    audit, live_legs, driver = _replay_both(df, settings, gen, tmp_path, driver_probe.required_bars)

    assert len(live_legs) > 5, "the fixture must produce trades for this to prove anything"
    # The backtest force-closes a position still open at the end of the data; a live run
    # has no "end of data", so that leg (and only that one) may be missing.
    if audit and audit[-1]["reason"] == "forced":
        assert driver.state.position is not None, "the still-open position should be held"
        audit = audit[:-1]
    assert len(audit) == len(live_legs), "the two paths traded a different number of times"
    for batch_leg, live_leg in zip(audit, live_legs):
        assert live_leg == batch_leg, (
            f"live and backtest disagreed on the trade at bar {live_leg['entry_idx']}"
        )


def test_the_breaker_halts_the_same_way_in_both_paths(tmp_path):
    """The circuit breaker is risk logic, so it must bite identically.

    Both paths replay from the same bar (see ``_replay_both``), because a breaker's memory
    is its loss history: a live run starting later cannot be expected to have lost what the
    backtest lost before it existed. Without the day key a live run booked its trades
    undated and never tripped — which is how this test came to exist.
    """
    df = _zigzag()
    settings = _settings(tmp_path, max_consecutive_losses=3, max_loss_percent=5.0)
    gen = _generator(settings, _always_buy())
    audit, live_legs, _ = _replay_both(df, settings, gen, tmp_path, 10)

    def shape(legs):
        return [(leg["reason"], leg["bars"], leg.get("skipped", False)) for leg in legs]

    assert any(leg["reason"] == "circuit_breaker" for leg in audit), (
        "the fixture must trip the breaker for this to prove anything"
    )
    assert any(leg["reason"] == "circuit_breaker" for leg in live_legs), (
        "the live run never halted, so it ignored the breaker"
    )
    if audit and audit[-1]["reason"] == "forced":
        audit = audit[:-1]
    assert shape(audit) == shape(live_legs)


def test_both_paths_enter_and_exit_at_the_same_prices(tmp_path):
    df = _zigzag()
    settings = _settings(tmp_path)
    gen = _generator(settings, _alternating())
    audit, live_legs, _ = _replay_both(df, settings, gen, tmp_path, 7)
    assert audit and live_legs, "no overlapping trades to compare"
    for a, b in zip(audit, live_legs):
        assert a["entry_price"] == pytest.approx(b["entry_price"])
        assert a["exit_price"] == pytest.approx(b["exit_price"])
        assert a["reason"] == b["reason"]
        assert a["weight"] == pytest.approx(b["weight"])
        assert a["direction"] == b["direction"]
        assert a["bars"] == b["bars"]


def test_exactly_one_implementation_decides(tmp_path, monkeypatch):
    """Both drivers must go through ``StrategyEngine.step``.

    If a driver ever grows its own opinion about stops, sizing or the breaker, this
    fails — which is the only way "the backtest measured what will run" stays true.
    """
    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    calls = {"n": 0}
    real_step = StrategyEngine.step

    def counting_step(self, state, bar, act):
        calls["n"] += 1
        return real_step(self, state, bar, act)

    monkeypatch.setattr(StrategyEngine, "step", counting_step)

    batch = _engine(settings)
    signals = [str(s) for s in gen.evaluate_frame(df)["signal"].tolist()]
    batch.run(signals, _bars(df), days=_day_keys(df))
    after_batch = calls["n"]
    assert after_batch > 0, "the backtest did not use the shared machine"

    _run_live(df, settings, gen, tmp_path, 7)
    assert calls["n"] > after_batch, "the live driver did not use the shared machine"


def test_the_adapter_holds_no_decisions_of_its_own():
    """``risk_sim`` is a translation layer. Any trading rule reappearing in it is a
    second implementation, and the parity tests above would eventually stop noticing.

    Reading ``opens``/``highs``/``lows`` to BUILD the bars is translation and is fine;
    deciding what to do with them is not.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "backtest" / "risk_sim.py"
    ).read_text(encoding="utf-8")
    for banned in ("target_weight", "realized_volatility_percent", "stop_distance",
                   "_level_hit", "record_trade", "tripped("):
        assert banned not in source, f"{banned} is a trading decision; it belongs in src/strategy"
    assert "StrategyEngine" in source, "the adapter must drive the shared machine"


# ---------------------------------------------------------------------------
# the data window
# ---------------------------------------------------------------------------
def test_the_required_window_comes_from_the_feature_config(tmp_path):
    """Not a separate number that can disagree with it."""
    settings = _settings(tmp_path, features_min_lookback=50, feature_sma_enabled=True,
                         features_sma_periods="10,20,50")
    assert live_data.required_bars(settings) >= 52
    longer = _settings(tmp_path, features_min_lookback=5, feature_sma_enabled=True,
                       features_sma_periods="200")
    assert live_data.required_bars(longer) >= 202, "a long indicator raises the need"


def test_a_short_window_is_refused_rather_than_decided_on(tmp_path):
    """One bar (what ``get_latest_candle`` returns) has NaN features: deciding on it
    reports HOLD for ever, which is indistinguishable from a quiet market."""
    df = _zigzag(60)
    settings = _settings(tmp_path, features_min_lookback=50)
    gen = _generator(settings, _always_buy())
    driver = LiveDriver(
        settings=settings, engine=_engine(settings), generator=gen,
        broker=SimulatedBroker(), state_path=tmp_path / "state.json",
    )
    with pytest.raises(NotEnoughHistory) as err:
        driver.on_bar_closed(df.iloc[-1:])       # one bar
    assert "NaN features" in str(err.value)
    with pytest.raises(NotEnoughHistory):
        driver.on_bar_closed(df.iloc[-10:])      # still short of 51
    driver.on_bar_closed(df.iloc[-60:])          # enough history -> a real decision


def test_the_fetch_window_is_derived_from_the_bar_count(tmp_path):
    """A bar count becomes dates, differently per bar size."""
    hourly = _settings(tmp_path, historical_bar_size="1h")
    daily = _settings(tmp_path, historical_bar_size="1d")
    assert live_data.days_for_bars(hourly, 51) < live_data.days_for_bars(daily, 51)
    for bar in ("1m", "15m", "1h", "1d"):
        s = _settings(tmp_path, historical_bar_size=bar)
        need = live_data.required_bars(s)
        assert live_data.days_for_bars(s, need) >= 1, bar


# ---------------------------------------------------------------------------
# live-only responsibilities
# ---------------------------------------------------------------------------
def test_the_same_bar_is_never_decided_twice(tmp_path):
    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    driver = LiveDriver(
        settings=settings, engine=_engine(settings), generator=gen,
        broker=SimulatedBroker(), state_path=tmp_path / "state.json",
    )
    bars = _bars(df)
    first = driver.on_bar_closed(df.iloc[:8], next_bar=bars[8])
    assert first["action"] == "decided"
    already = driver.on_bar_closed(df.iloc[:8], next_bar=bars[8])
    assert already["action"] == "noop"
    assert len(driver.ledger.legs) == len([leg for leg in driver.ledger.legs])


def test_state_survives_a_restart_without_refiring(tmp_path):
    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    path = tmp_path / "state.json"
    bars = _bars(df)
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=SimulatedBroker(), state_path=path)
    driver.on_bar_closed(df.iloc[:8], next_bar=bars[8])
    assert driver.state.position is not None
    assert path.exists()

    restarted = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                           broker=SimulatedBroker(), state_path=path)
    restarted.load_state()
    assert restarted.state.position is not None, "the position must survive a restart"
    assert restarted.state.last_decided_bar == driver.state.last_decided_bar
    assert restarted.on_bar_closed(df.iloc[:8], next_bar=bars[8])["action"] == "noop"


def test_the_driver_refuses_to_trade_while_the_broker_disagrees(tmp_path):
    """The broker is the truth about what is held. Acting on stale state is how a bot
    doubles up or sells something it does not own."""

    class LyingBroker(SimulatedBroker):
        def position(self):
            return BrokerPosition(quantity=10, entry_price=100.0, short=False)

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=LyingBroker(), state_path=tmp_path / "state.json")
    result = driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])
    assert result["action"] == "refused"
    assert "local state is flat" in result["reason"]
    assert driver.state.position is None, "nothing was opened"


def test_a_broker_that_fills_elsewhere_moves_the_stop_with_it(tmp_path):
    """The one asymmetry a backtest cannot promise: the real fill price. A stop rests
    where the position was actually bought."""

    class Worse(SimulatedBroker):
        """Fills 1% below the expectation on entries — a plausible bad fill."""

        def submit(self, intent):
            fill = super().submit(intent)
            if fill.filled and intent.action == "open":
                return Fill(status="filled", price=float(fill.price) * 0.99, quantity=1.0)
            return fill

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=Worse(), state_path=tmp_path / "state.json")
    driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])
    pos = driver.state.position
    assert pos is not None
    expected = _engine(settings).levels(pos.entry_price, pos.short)
    assert (pos.stop, pos.take) == expected, "levels must follow the real fill"
    assert pos.entry_price < _bars(df)[5].open, "the worse price was adopted"


# ---------------------------------------------------------------------------
# sizing may not look ahead
# ---------------------------------------------------------------------------
def test_sizing_cannot_see_the_close_of_the_bar_it_fills_in(tmp_path):
    """The entry bar's own close is not knowable at its open, so the volatility-target
    mode may not use it — a fill sized from it could never be reproduced live."""
    settings = _settings(tmp_path, sizing_mode="volatility_target", risk_limit_percent=2.0)
    engine = _engine(settings)
    bars = [Bar(index=i, time=f"2026-09-14 {9+i:02d}:00", open=100.0 + i, close=100.0 + i,
                high=101.0 + i, low=99.0 + i) for i in range(4)]
    state = StrategyState()
    engine.observe_close(100.0)
    engine.observe_close(101.0)
    first = engine.step(state, bars[1], "BUY")[0]
    weight_a = first.weight

    # A wildly different close on the entry bar must not change the size.
    wild = list(bars)
    wild[1] = Bar(index=1, time=bars[1].time, open=bars[1].open, close=10_000.0,
                  high=bars[1].high, low=bars[1].low)
    state2 = StrategyState()
    engine2 = _engine(settings)
    engine2.observe_close(100.0)
    engine2.observe_close(101.0)
    weight_b = engine2.step(state2, wild[1], "BUY")[0].weight
    assert weight_a == pytest.approx(weight_b)
