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
from src.strategy.broker import BrokerPosition, ClosingFill, Fill, SimulatedBroker
from src.strategy.config import StrategyConfig
from src.strategy.engine import STOP, TAKE, Bar, StrategyEngine, bar_from_row
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
        slippage=(float(settings.backtest_slippage_percent) / 100.0 if costs else 0.0),
        commission=(float(settings.backtest_commission_per_trade) if costs else 0.0),
    )
    return StrategyEngine(cfg)




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
    offset = start - 1
    # Renumber the slice from zero: the ledger's return series is indexed by the bar's
    # POSITION in the series it was given, so a slice must be numbered as its own series
    # and shifted back afterwards for the comparison.
    sliced = [
        Bar(index=i, time=b.time, open=b.open, close=b.close, high=b.high, low=b.low)
        for i, b in enumerate(bars[offset:])
    ]
    batch = _engine(settings).run(signals[offset:], sliced)

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


def test_the_configured_stop_and_target_apply_identically_in_both_paths(tmp_path):
    """The risk settings are the only switch, so both drivers must read them the same.

    A stop that fired in the replay and not live (or the other way round) would make every
    backtest verdict a statement about a different system than the one that trades. The
    loss limits are no longer part of this: halting on losses is deferred to the execution
    loop, so there is nothing for the two paths to disagree about there yet.
    """
    df = _zigzag()
    settings = _settings(tmp_path, stop_loss_percent=2.0, take_profit_percent=3.0)
    gen = _generator(settings, _always_buy())
    audit, live_legs, _ = _replay_both(df, settings, gen, tmp_path, 10)

    def shape(legs):
        return [(leg["reason"], leg["bars"], leg.get("skipped", False)) for leg in legs]

    assert any(leg["reason"] in ("stop", "take") for leg in audit), (
        "the fixture must hit a level for this to prove anything"
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
    calls = {"n": 0, "vetoes": 0}
    real_step = StrategyEngine.step

    def counting_step(self, state, bar, act, **kw):
        calls["n"] += 1
        if kw.get("veto"):
            calls["vetoes"] += 1
        return real_step(self, state, bar, act, **kw)

    monkeypatch.setattr(StrategyEngine, "step", counting_step)

    batch = _engine(settings)
    signals = [str(s) for s in gen.evaluate_frame(df)["signal"].tolist()]
    batch.run(signals, _bars(df))
    after_batch = calls["n"]
    assert after_batch > 0, "the backtest did not use the shared machine"

    _run_live(df, settings, gen, tmp_path, 7)
    assert calls["n"] > after_batch, "the live driver did not use the shared machine"
    # ``veto`` is the one argument a live run can pass that a backtest cannot, so it is the
    # one way the two could drift apart in a way this test would otherwise welcome. The
    # day's loss limits are the LOOP's (``LiveDriver.halt``, set per tick); neither a
    # backtest nor a replay that never sets a halt may reach that path.
    assert calls["vetoes"] == 0, "a veto reached the shared machine from a run with no halt"


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


def test_a_position_only_the_broker_holds_is_adopted_not_traded_over(tmp_path):
    """The broker is the truth about what is held — and when it holds something the local
    state does not know about, the driver TAKES IT ON.

    This used to refuse ("local state is flat"). Refusing was the wrong half of the right
    instinct: it made "trading on" a switch that could never trade, and it left the position
    unmanaged, which is exactly what it claimed to prevent. Adopted, the position is the
    strategy's — and since ``step`` opens nothing while one is held, everything the bot does
    from here closes it.
    """

    class HoldsSomething(SimulatedBroker):
        def position(self):
            return BrokerPosition(quantity=10, entry_price=100.0, short=False)

        def resting_levels(self):
            # Far outside the fixture's prices, so the bar under test cannot close the position
            # on a level: what is pinned here is the adoption, not a stop-out.
            return {"stop": 50.0, "take": 200.0}

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    broker = HoldsSomething()
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=broker, state_path=tmp_path / "state.json")
    result = driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])

    assert result["action"] == "decided", "an adoption is a decision, not a refusal"
    assert result["adopted_position"]["quantity"] == 10
    pos = driver.state.position
    assert pos is not None and pos.short is False, "now held locally, at the broker's word"
    assert pos.entry_price == 100.0, "at the price the broker says it was bought for"
    assert (pos.stop, pos.take) == (50.0, 200.0), "protected by the broker's own exits"
    # The broker was not asked for anything: an always-BUY signal on a bar where a position is
    # held is not an entry, which is the whole of "nothing is added to an adopted position".
    assert [intent["action"] for intent in result["intents"]] == [], result["intents"]


def test_a_broker_that_fills_elsewhere_moves_the_stop_with_it(tmp_path):
    """The one asymmetry a backtest cannot promise: the real fill price. A stop rests
    where the position was actually bought."""

    class Worse(SimulatedBroker):
        """Fills 1% below the expectation on entries — a plausible bad fill."""

        def submit(self, intent, client_order_id=None):
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


def test_the_resting_exits_are_moved_to_the_levels_the_real_fill_implies(tmp_path):
    """Deriving the exit locally is only half the fix.

    The bracket is SENT with the exits measured from the expected price — waiting for the
    fill before sending anything would leave the entry naked. So after a fill that landed
    elsewhere the broker's resting legs have to be moved onto the levels the real price
    implies, or a long filled above expectation carries more risk than was sized for while
    the backtest reports it did not.
    """

    class OffPrice(SimulatedBroker):
        """Fills above expectation and remembers what it was asked to amend."""

        def __init__(self):
            super().__init__()
            self.asked = None

        def submit(self, intent, client_order_id=None):
            fill = super().submit(intent)
            if fill.filled and intent.action == "open":
                return Fill(status="filled", price=float(fill.price) * 1.01, quantity=1.0)
            return fill

        def reprice_exits(self, stop, take):
            self.asked = (stop, take)
            return {"amended": [{"leg": "stop"}, {"leg": "limit"}]}

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    broker = OffPrice()
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=broker, state_path=tmp_path / "state.json")
    result = driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])

    pos = driver.state.position
    assert pos is not None
    expected = _engine(settings).levels(pos.entry_price, pos.short)
    assert broker.asked == expected, "the broker must be told to move onto the real levels"
    assert result["intents"][0]["exits"]["amended"], "and the tick reports that it did"


def test_a_simulated_broker_has_nothing_to_adopt_and_nothing_to_move():
    """Both live-only methods are deliberate no-ops here, which is what keeps the parity
    test on the same code path as a real run. The resting levels are the third: a simulated
    position is protected by the engine's own levels, so there are no orders to read."""
    broker = SimulatedBroker()
    assert broker.closing_fill(False) is None
    assert broker.reprice_exits(100.0, 200.0) is None
    assert broker.resting_levels() == {"stop": None, "take": None}


def test_an_exit_the_broker_made_is_adopted_rather_than_refused(tmp_path):
    """A resting bracket stop firing between two ticks used to wedge the bot for ever.

    The driver is asleep, the market is not: by the next tick the broker is flat and the
    local state still holds a position. Refusing is right for "we disagree"; it is wrong
    for "my exit already happened and I can prove it at what price".
    """

    class BracketFired(SimulatedBroker):
        """Flat at the broker, with the closing fill on record."""

        def __init__(self, *, price=97.25, reason=STOP, proof=True):
            super().__init__()
            self._price, self._reason, self._proof = price, reason, proof

        def position(self):
            return BrokerPosition()          # the stop took us out

        def closing_fill(self, short):
            if not self._proof:
                return None
            return ClosingFill(price=self._price, reason=self._reason, order_id="leg-stop",
                               quantity=10.0)

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    broker = BracketFired()
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=broker, state_path=tmp_path / "state.json")
    driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])
    assert driver.state.position is not None, "a position to be closed out of band"
    trades = len(driver.ledger.legs)

    result = driver.on_bar_closed(df.iloc[:9], next_bar=_bars(df)[9])

    assert result["action"] == "decided", "an adoption is not a refusal"
    assert result["adopted"]["price"] == 97.25
    assert result["adopted"]["reason"] == STOP
    assert result["closed_qty"] == 10.0, (
        "the size the broker's exit filled goes on the tick, or the round trip it closed has "
        "no money on it"
    )
    assert len(driver.ledger.legs) == trades + 1, "the adopted exit is one closed trade"
    booked = driver.ledger.legs[-1]
    assert booked["exit_price"] == 97.25, "at the broker's price, not a local guess"
    assert booked["reason"] == STOP, "and with the reason read off the broker's order"


def test_an_exit_that_cannot_be_priced_is_still_refused(tmp_path):
    """The other half of the distinction: no proof means no booking.

    Either the position was closed by something the history does not explain, or it was
    closed and Alpaca did not say at what price. Both are conditions to stop on, because
    the alternative is inventing the price of a trade.
    """

    class Unexplained(SimulatedBroker):
        def position(self):
            return BrokerPosition()

        def closing_fill(self, short):
            return None

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=Unexplained(), state_path=tmp_path / "state.json")
    driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])
    trades = len(driver.ledger.legs)

    result = driver.on_bar_closed(df.iloc[:9], next_bar=_bars(df)[9])

    assert result["action"] == "refused"
    assert "does not show what closed it" in result["reason"]
    assert len(driver.ledger.legs) == trades, "nothing was booked on a guess"


def test_a_direction_the_two_sides_disagree_on_is_refused(tmp_path):
    """Both hold something, but not the same something: a second opinion about reality,
    which no price can settle."""

    class Opposite(SimulatedBroker):
        """Reports a SHORT while the local state holds a LONG."""

        def position(self):
            return BrokerPosition(quantity=-5.0, short=True)

    df = _zigzag(40)
    settings = _settings(tmp_path)
    gen = _generator(settings, _always_buy())
    driver = LiveDriver(settings=settings, engine=_engine(settings), generator=gen,
                        broker=SimulatedBroker(), state_path=tmp_path / "state.json")
    first = driver.on_bar_closed(df.iloc[:8], next_bar=_bars(df)[8])
    assert first["action"] == "decided"
    assert driver.state.position is not None and driver.state.position.short is False

    # The broker's story changes underneath us — the case that must never be traded on.
    driver.broker = Opposite()
    second = driver.on_bar_closed(df.iloc[:9], next_bar=_bars(df)[9])
    assert second["action"] == "refused"
    assert "the broker says short" in second["reason"]


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
