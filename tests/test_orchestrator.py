"""The loop: one test per thing the tick's ORDER is responsible for.

The tick is a pipeline, and most of these tests are about order rather than about outcomes:
the switch before the clock, the clock before the sync, the sync before the window, the
window before the decision. Getting the sequence wrong is invisible in production because
every step still "works" — it just acts on the wrong input, or acts when it should have
stepped aside.

Everything is injected: the provider sync, the exchange clock and the driver. No test
reaches a provider, an exchange or a broker (``tests/conftest.py`` closes the transport
outright).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config import freshness
from src.config import state_files
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.data import dataset
from src.data.dataset import save_dataset
from src.execution.accounts import EnvAccount
from src.scheduler import lease as lease_mod
from src.scheduler import orchestrator
from src.strategy.broker import SimulatedBroker
from src.strategy.config import StrategyConfig
from src.strategy.engine import StrategyEngine
from src.strategy.live import LiveDriver

ET = ZoneInfo("America/New_York")
STRATEGY = "Alpha"

# A Friday session with an hourly grid: 09:30 through 15:30.
SESSION = [f"2024-01-05 {hh}:30:00" for hh in ("09", "10", "11", "12", "13", "14", "15")]


def _sessions(count: int = 14, last: str = "2024-01-05") -> list:
    """``count`` weekday sessions of hourly bars, ending on ``last``.

    More than one, because the features need a warmup before they will produce anything:
    a window shorter than ``required_bars`` is refused outright rather than decided on, and
    a fixture that ignored that would be testing the refusal on every test.
    """
    days, day = [], date.fromisoformat(last)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    stamps: list = []
    for one in sorted(days):
        stamps += [f"{one} {hh}:30:00" for hh in ("09", "10", "11", "12", "13", "14", "15")]
    return stamps


def _settings(tmp_path, **kw) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        # The loop WRITES here (``latest.json``, a tick log, the day index), so it has to be a
        # temp tree like the datasets: without this a suite run leaves a tick in the live tree of
        # whatever strategy the machine has active, and the Session monitor then shows a session
        # that never happened — an "action: off" tick nobody ran.
        live_dir=str(tmp_path / "data" / "live_results"),
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
        trading_start_hour="09:30",
        trading_end_hour="16:00",
        features_min_lookback=3,
        # Configured, because a tick that could not place an order refuses before it asks
        # anything else — which is the point of that check, and would mask every test below
        # it. ``test_orders_that_would_be_refused...`` takes them away on purpose.
        alpaca_paper_api_key="PK-PAPER",
        alpaca_paper_api_secret="S-PAPER",
    )
    values.update(kw)
    return Settings(**values)


def _frame(stamps):
    idx = pd.DatetimeIndex([pd.Timestamp(s) for s in stamps])
    n = len(idx)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ET)


class AlwaysBuy:
    """A generator that always says BUY, so the tests are about sequencing, not signals."""

    skipped_rules: list = []
    rules: list = [object()]

    def evaluate_frame(self, frame):
        out = frame.copy()
        out["signal"] = "BUY"
        return out


def _driver(settings, broker=None) -> LiveDriver:
    return LiveDriver(
        settings=settings,
        engine=StrategyEngine(StrategyConfig.from_settings(settings)),
        generator=AlwaysBuy(),
        broker=broker if broker is not None else SimulatedBroker(),
        name=STRATEGY,
    )


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """Trading ON for STRATEGY, state files in tmp_path, and a dataset on disk.

    ``active_strategy_name`` is patched because it reads the real strategy store; the loop
    only needs the two answers "which strategy is active" and "which one was stamped".
    """
    monkeypatch.setattr(state_files, "state_path", lambda s, name: tmp_path / name)
    monkeypatch.setattr(orchestrator, "active_strategy_name", lambda: STRATEGY)

    def build(**kw):
        settings = _settings(tmp_path, **kw)
        save_dataset(settings, _frame(_sessions()), "AAPL", "1h")
        write_state(settings, {"on": True, "since": "2024-01-05T13:00:00+00:00", "strategy": STRATEGY, "env": "paper"})
        return settings

    return build


class Calls:
    """Records what the tick asked for, so the ORDER can be asserted."""

    def __init__(self, *, clock=None, sync_error=None):
        self.order = []
        self.sync_error = sync_error
        self._clock = clock if clock is not None else {"is_open": True, "next_close": "2024-01-05T21:00:00Z"}

    def sync(self):
        self.order.append("sync")
        if self.sync_error:
            raise RuntimeError(self.sync_error)
        return {"synced": True}

    def clock(self):
        self.order.append("clock")
        return self._clock


# ---------------------------------------------------------------------------
# the switch, first
# ---------------------------------------------------------------------------
def test_nothing_happens_at_all_while_trading_is_off(tmp_path, monkeypatch, armed):
    monkeypatch.setattr(state_files, "state_path", lambda s, name: tmp_path / name)
    settings = _settings(tmp_path)
    save_dataset(settings, _frame(SESSION), "AAPL", "1h")
    calls = Calls()

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert record["action"] == "off"
    assert calls.order == [], "the switch is checked before anything is asked for"


def test_the_switch_is_re_read_every_tick(tmp_path, armed):
    """That is what makes OFF take effect at the next boundary with nothing else needed."""
    settings = armed()
    calls = Calls()
    first = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)
    assert first["action"] != "off"

    write_state(settings, {"on": False, "since": None})
    second = orchestrator.tick(settings, now=_at("2024-01-05 15:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert second["action"] == "off", "a state written a moment ago is what the next tick obeys"


# ---------------------------------------------------------------------------
# the stamp, before anything is asked of the broker
# ---------------------------------------------------------------------------
def test_a_tick_refuses_when_the_active_strategy_is_not_the_stamped_one(tmp_path, armed, monkeypatch):
    """A stale tab or a hand-edited store must not become orders for the wrong strategy."""
    settings = armed()
    monkeypatch.setattr(orchestrator, "active_strategy_name", lambda: "SomethingElse")
    calls = Calls()

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert record["action"] == "refused"
    assert "armed for 'Alpha'" in record["reason"] and "SomethingElse" in record["reason"]
    assert calls.order == [], "refused before the clock or the provider was touched"


# ---------------------------------------------------------------------------
# the clock, and never a fallback
# ---------------------------------------------------------------------------
def test_a_clock_that_cannot_be_read_fails_the_tick(tmp_path, armed):
    """Falling back to a weekday-and-hours check is how a holiday trades a stale bar."""
    settings = armed()

    def broken():
        raise RuntimeError("no route to the exchange")

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=broken)

    assert record["action"] == "refused"
    assert "clock could not be read" in record["reason"]


def test_a_closed_exchange_is_a_closed_tick_with_no_sync(tmp_path, armed):
    settings = armed()
    calls = Calls(clock={"is_open": False})

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert record["action"] == "closed"
    assert calls.order == ["clock"], "no data is fetched for a market that is shut"


def test_orders_that_would_be_refused_stop_the_tick_before_the_clock(tmp_path, monkeypatch, armed):
    """No credentials means the switch could not have been armed — refuse without asking."""
    settings = armed(alpaca_paper_api_key=None)
    calls = Calls()

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert record["action"] == "refused"
    assert calls.order == []


# ---------------------------------------------------------------------------
# the sync, then the window
# ---------------------------------------------------------------------------
def test_the_sync_runs_after_the_clock_and_before_the_decision(tmp_path, armed):
    settings = armed()
    calls = Calls()

    orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert calls.order == ["clock", "sync"]
    assert orchestrator.store.load_latest(settings, STRATEGY) is not None


def test_the_sync_still_runs_on_a_tick_that_decides_nothing(tmp_path, armed):
    """Coming back to a current dataset after a week off beats a gap that never heals."""
    settings = armed()
    calls = Calls()
    orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    # Same bar again: a no-op, but the dataset was still brought up to date.
    orchestrator.tick(settings, now=_at("2024-01-05 14:10"), sync_call=calls.sync, clock_call=calls.clock)

    assert calls.order == ["clock", "sync", "clock", "sync"]


def test_a_provider_failure_refuses_the_tick_rather_than_deciding_on_stale_data(tmp_path, armed):
    settings = armed()
    calls = Calls(sync_error="provider is down")

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=calls.sync, clock_call=calls.clock)

    assert record["action"] == "refused"
    assert "could not be synced" in record["reason"]


def test_the_decision_ends_at_the_newest_bar_that_has_CLOSED(tmp_path, armed):
    """At 14:05 the 13:30 bar is still forming, so the window must end at 12:30."""
    settings = armed()
    seen = {}

    class Recorder(AlwaysBuy):
        def evaluate_frame(self, frame):
            seen["last"] = str(frame.index[-1])
            return super().evaluate_frame(frame)

    driver = _driver(settings)
    driver.generator = Recorder()
    orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=driver)

    assert seen["last"] == "2024-01-05 12:30:00"


def test_a_tick_refuses_when_the_dataset_is_behind_the_bar_that_should_have_closed(tmp_path, armed):
    """The failure that costs nothing, chosen over the one that costs money."""
    armed()                                     # arms the switch; its dataset is not the one we read
    # A data directory of its own, holding ONLY a short dataset. ``save_dataset`` MERGES
    # into what is already on disk, so writing the short frame over the fixture's would
    # leave every bar it meant to remove exactly where it was.
    settings = _settings(
        tmp_path,
        data_dir=str(tmp_path / "behind"),
        historical_data_dir=str(tmp_path / "behind" / "historical"),
    )
    truncated = _sessions()[:-4]                                # nothing after 11:30
    save_dataset(settings, _frame(truncated), "AAPL", "1h")

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings))

    assert record["action"] == "refused"
    assert "stale bar" in record["reason"]


# ---------------------------------------------------------------------------
# idempotency and the heartbeat
# ---------------------------------------------------------------------------
def test_the_same_bar_is_only_decided_once(tmp_path, armed):
    settings = armed()
    first = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings))
    second = orchestrator.tick(settings, now=_at("2024-01-05 14:10"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings))

    assert first["action"] == "decided"
    assert second["action"] == "noop" and "already decided" in second["reason"]


def test_a_new_bar_is_decided_on(tmp_path, armed):
    settings = armed()
    # ONE broker across both ticks. The account is the same account between ticks — a fresh
    # SimulatedBroker each time would report flat against a held position, and the driver
    # would rightly refuse that as drift.
    broker = SimulatedBroker()
    first = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings, broker))
    later = orchestrator.tick(settings, now=_at("2024-01-05 15:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings, broker))

    assert first["bar"] != later["bar"]
    assert later["action"] == "decided"


@pytest.mark.parametrize("action_hint", ["off", "closed", "noop", "refused"])
def test_every_tick_moves_the_heartbeat(tmp_path, armed, action_hint):
    """A quiet day and a dead loop must not look the same from the dashboard."""
    settings = armed()
    path = orchestrator.store.latest_path(settings, STRATEGY)
    assert not path.exists()

    if action_hint == "off":
        write_state(settings, {"on": False, "since": None})
        record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"))
    elif action_hint == "closed":
        record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), clock_call=Calls(clock={"is_open": False}).clock)
    elif action_hint == "refused":
        record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), clock_call=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    else:
        orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings))
        record = orchestrator.tick(settings, now=_at("2024-01-05 14:06"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings))

    assert record["action"] == action_hint
    assert path.exists(), "the heartbeat is written whatever the tick decided"
    assert orchestrator.store.load_latest(settings, STRATEGY)["action"] == action_hint


# ---------------------------------------------------------------------------
# nothing may kill the loop
# ---------------------------------------------------------------------------
def test_an_unexpected_failure_becomes_a_refusal_not_a_crash(tmp_path, armed):
    settings = armed()

    class Exploding(AlwaysBuy):
        def evaluate_frame(self, frame):
            raise ValueError("something nobody predicted")

    driver = _driver(settings)
    driver.generator = Exploding()

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=driver)

    assert record["action"] == "refused"
    assert "decision failed" in record["reason"]


def test_a_driver_for_another_strategy_is_refused(tmp_path, armed):
    """The state file and the account would both be the wrong one."""
    settings = armed()
    other = LiveDriver(
        settings=settings,
        engine=StrategyEngine(StrategyConfig.from_settings(settings)),
        generator=AlwaysBuy(),
        broker=SimulatedBroker(),
        name="SomebodyElse",
    )

    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=other)

    assert record["action"] == "refused"
    assert "SomebodyElse" in record["reason"]


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
def test_a_decision_is_logged_and_counted_in_the_day_index(tmp_path, armed):
    settings = armed()
    record = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings))

    assert record["action"] == "decided"
    logged = orchestrator.store.read_ticks(settings, STRATEGY, when=_at("2024-01-05 14:05"))
    assert [r["action"] for r in logged] == ["decided"]
    index = orchestrator.store.load_index(settings, STRATEGY)
    assert index[0]["day"] == "2024-01-05"
    assert index[0]["decided"] == 1 and index[0]["events"] == 1


def test_a_quiet_tick_is_not_written_to_the_log(tmp_path, armed):
    """The log is what someone reads to find out what HAPPENED, not that nothing did."""
    settings = armed()
    orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls(clock={"is_open": False}).clock)

    assert orchestrator.store.read_ticks(settings, STRATEGY, when=_at("2024-01-05 14:05")) == []
    assert orchestrator.store.load_latest(settings, STRATEGY)["action"] == "closed"


# ---------------------------------------------------------------------------
# the order and trade logs
# ---------------------------------------------------------------------------
class _SellAfterBuy(AlwaysBuy):
    """Buy on the first call, sell on the second — one round trip, deterministically."""

    def __init__(self):
        self.calls = 0

    def evaluate_frame(self, frame):
        self.calls += 1
        return frame.assign(signal="BUY" if self.calls == 1 else "SELL")


def test_a_decided_tick_writes_the_order_it_submitted(tmp_path, armed):
    """orders.jsonl is the loop's account of what it TRIED to do, with the join key.

    The broker's own list answers what became of an order; this answers which bar, which
    signal and which intent produced it. ``client_order_id`` is what joins the two, and it
    is the only thing that makes an order in Alpaca's dashboard traceable back to a
    strategy and a bar.
    """
    settings = armed()
    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=_driver(settings),
    )

    rows = orchestrator.store.read_orders(settings, STRATEGY)
    assert record["action"] == "decided"
    assert len(rows) == 1, rows
    assert rows[0]["intent"] == "open"
    assert rows[0]["status"] == "filled"
    assert rows[0]["bar"] == record["bar"]
    assert rows[0]["client_order_id"].startswith("traider-")
    assert rows[0]["day"] == "2024-01-05"
    assert orchestrator.store.load_index(settings, STRATEGY)[0]["orders"] == 1


def test_a_refused_tick_writes_no_orders_but_its_trades_are_kept(tmp_path, armed):
    """A refusal can still have BOOKED something.

    An exit the broker made is adopted before the reconcile that refuses, so the trade
    happened whatever the tick's verdict — and dropping it here is how a real exit vanishes
    from the log while the position is still gone from the account.
    """
    settings = armed()
    leg = {"entry_idx": 1, "exit_idx": 4, "direction": "long", "entry_price": 100.0,
           "exit_price": 104.0, "ret": 0.04, "equity_ret": 0.04, "weight": 1.0,
           "bars": 3, "reason": "stop", "skipped": False}

    class RefusingDriver:
        name = STRATEGY
        state = type("S", (), {"position": None})()

        def on_bar_closed(self, window):
            return {"action": "refused", "reason": "the broker holds 4 shares we do not",
                    "bar": "2024-01-05T17:30:00+00:00", "trades": [leg]}

    orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=RefusingDriver(),
    )

    trades = orchestrator.store.read_trades(settings, STRATEGY)
    assert [t["reason"] for t in trades] == ["stop"]
    assert trades[0]["exit_price"] == 104.0 and trades[0]["ret"] == 0.04
    assert orchestrator.store.read_orders(settings, STRATEGY) == [], "nothing was submitted"
    assert orchestrator.store.load_index(settings, STRATEGY)[0]["trades"] == 1


def test_a_position_the_broker_never_opened_is_dropped_and_said_out_loud(tmp_path, armed):
    """The tick that finds one goes ON, and the record says what was let go.

    A position only the local state holds, with nothing in the broker's history that could ever
    settle it, is what used to stop every tick from here on. When the driver can prove it was never
    opened it drops it — and a position that disappears with no trade against it is not a quiet
    correction, so it goes on the tick's notes where a reader of the day will meet it.
    """
    settings = armed()
    detail = "a recorded position in X was dropped — the broker is flat and its entry was REFUSED"

    class DroppingDriver:
        name = STRATEGY
        state = type("S", (), {"position": None})()

        def on_bar_closed(self, window):
            return {
                "action": "decided", "bar": "2024-01-05T17:30:00+00:00", "signal": "HOLD",
                "intents": [],
                "dropped": {"bar": "2024-01-05 13:30:00", "order_id": "traider-X-abc",
                            "detail": detail},
            }

    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=DroppingDriver(),
    )

    assert record["action"] == "decided", "the drop is not a refusal — that is its whole point"
    assert record["notes"] == [detail]
    logged = orchestrator.store.read_ticks(settings, STRATEGY, "2024-01-05")[0]
    assert logged["notes"] == record["notes"], "and it is written to the day's log"


def test_a_position_the_driver_adopted_is_said_out_loud_on_the_tick(tmp_path, armed):
    """The other correction that is not a refusal: a position the driver TOOK ON.

    Trading may be armed while something is already open in the account it trades — the loop
    adopts it (see ``LiveDriver.adopt_broker_position``) and can then only close it. That is a
    position the strategy owns without having opened it, so the tick says which one it is: the
    operator reads the notes column to find out what the bot is now managing.
    """
    settings = armed()
    detail = (
        "the paper account already held 5 X (long) when trading was armed, so the strategy "
        "adopted it at 159.0000; it will be CLOSED"
    )

    class AdoptingDriver:
        name = STRATEGY
        state = type("S", (), {"position": None})()

        def on_bar_closed(self, window):
            return {
                "action": "decided", "bar": "2024-01-05T17:30:00+00:00", "signal": "BUY",
                "intents": [],
                "adopted_position": {"short": False, "quantity": 5.0, "price": 159.0,
                                     "stop": 155.8, "take": 165.4, "detail": detail},
            }

    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=AdoptingDriver(),
    )

    assert record["action"] == "decided", "an adoption is a decision, not a refusal"
    assert record["notes"] == [detail]
    logged = orchestrator.store.read_ticks(settings, STRATEGY, "2024-01-05")[0]
    assert logged["notes"] == record["notes"], "and it is written to the day's log"


def test_a_closed_position_writes_a_trade_row(tmp_path, armed):
    """The live half of a trade list: until this existed, a live exit price was lost with
    the tick that computed it — which is why only the backtest could show one."""
    settings = armed()
    broker = SimulatedBroker()
    generator = _SellAfterBuy()

    first = _driver(settings, broker)
    first.generator = generator
    opened = orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {},
                               clock_call=Calls().clock, driver=first)
    second = _driver(settings, broker)
    second.generator = generator
    closed = orchestrator.tick(settings, now=_at("2024-01-05 15:05"), sync_call=lambda: {},
                               clock_call=Calls().clock, driver=second)

    assert opened["action"] == "decided" and closed["action"] == "decided"
    assert closed["signal"] == "SELL"
    trades = orchestrator.store.read_trades(settings, STRATEGY)
    assert len(trades) == 1, trades
    assert trades[0]["direction"] == "long"
    assert trades[0]["entry_price"] == opened["intents"][0]["price"]
    assert trades[0]["reason"] in ("signal", "forced")
    assert orchestrator.store.load_latest(settings, STRATEGY)["trades"], "also on the heartbeat"
    assert len(orchestrator.store.read_orders(settings, STRATEGY)) == 2, "the open and the close"


def test_a_closed_position_records_the_money_it_made(tmp_path, armed):
    """A return becomes a profit only with a SIZE, and the size comes from the fill: the
    strategy's own leg has no quantity at all.

    End to end, through the driver and the simulated broker — which is also the path a dry
    run takes, so a simulated round trip must carry a real size rather than zero.
    """
    settings = armed()
    broker = SimulatedBroker()
    generator = _SellAfterBuy()

    first = _driver(settings, broker)
    first.generator = generator
    orchestrator.tick(settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {},
                      clock_call=Calls().clock, driver=first)
    second = _driver(settings, broker)
    second.generator = generator
    orchestrator.tick(settings, now=_at("2024-01-05 15:05"), sync_call=lambda: {},
                      clock_call=Calls().clock, driver=second)

    (trade,) = orchestrator.store.read_trades(settings, STRATEGY)
    assert trade["qty"] == 1.0, "the shares the simulated close flattened"
    entry, exit_price = trade["entry_price"], trade["exit_price"]
    assert trade["pnl"] == pytest.approx(round((exit_price - entry) * 1.0, 2))

    # And the size is on the ORDER row too, which is where a reader asks what an exit sold.
    orders = orchestrator.store.read_orders(settings, STRATEGY)
    assert [o["filled_qty"] for o in orders] == [1.0, 1.0], orders


# ---------------------------------------------------------------------------
# the bar the decision is made on
# ---------------------------------------------------------------------------
def _break_signal_bar(settings, at, column, value):
    """Break the bar the next tick will decide on, and save the dataset back.

    The bar is found through ``last_closed_bar`` rather than taken to be the frame's last row:
    the loop decides on the newest bar that has CLOSED at ``at``, and near a session boundary
    that is not the last row in the file. (Found the hard way — a corrupted last row was
    silently outside the window, so the check under test never saw it.)
    """
    until = dataset.bar_stamp(settings, dataset.last_closed_bar(settings, at))
    frame = _frame(_sessions())
    stamps = [dataset.bar_stamp(settings, ts) for ts in frame.index]
    frame.iloc[stamps.index(until), frame.columns.get_loc(column)] = value
    save_dataset(settings, frame, "AAPL", "1h")
    return until


def test_an_impossible_bar_refuses_the_tick(tmp_path, armed):
    """A decision made on a bar that cannot exist is a decision about a market that never did.

    Refusing costs a bar; sizing a position from a high that is under its own low costs money,
    and the number would look perfectly ordinary in every record afterwards.
    """
    settings = armed()
    at = _at("2024-01-05 14:05")
    _break_signal_bar(settings, at, "high", 1.0)

    record = orchestrator.tick(
        settings, now=at, sync_call=lambda: {}, clock_call=Calls().clock,
        driver=_driver(settings),
    )

    assert record["action"] == "refused", record
    assert "high (1) is below its low" in record["reason"], record["reason"]
    assert record["intents"] == [] and record["order_ids"] == [], "nothing was decided"
    assert orchestrator.store.read_orders(settings, STRATEGY) == []


def test_an_odd_bar_is_decided_on_and_the_note_rides_along(tmp_path, armed):
    """Unusual is not impossible: the bar is used, and what was odd about it is recorded.

    Both halves matter. Refusing every zero-volume bar stops the bot on a quiet afternoon;
    deciding on one silently hides a bar somebody should look at.
    """
    settings = armed()
    at = _at("2024-01-05 14:05")
    _break_signal_bar(settings, at, "volume", 0)

    record = orchestrator.tick(
        settings, now=at, sync_call=lambda: {}, clock_call=Calls().clock,
        driver=_driver(settings),
    )

    assert record["action"] == "decided", "the bar was usable, so it was used"
    assert record["notes"] == ["the bar has a volume of 0"], record["notes"]
    assert record["intents"][0]["intent"] == "open", "and the entry went through"
    # The note is in the day's log too, not only on the heartbeat the panel reads.
    logged = orchestrator.store.read_ticks(settings, STRATEGY, "2024-01-05")[0]
    assert logged["notes"] == record["notes"]


def test_a_healthy_bar_carries_an_empty_notes_list(tmp_path, armed):
    """The key is always there, so nothing has to check for its absence.

    ``notes`` appears on every record, empty when there is nothing to say — a field that only
    shows up when something is wrong is one a reader has to remember to look for.
    """
    settings = armed()
    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=_driver(settings),
    )

    assert record["notes"] == []


# ---------------------------------------------------------------------------
# the day's loss limits
# ---------------------------------------------------------------------------
def _closed_trade(settings, when, *, equity_ret: float = -0.01) -> None:
    """One closed round trip in the strategy's trade log, dated by ``when``."""
    orchestrator.store.append_trade(
        settings,
        STRATEGY,
        orchestrator.store.trade_record(
            settings=settings,
            strategy=STRATEGY,
            env="paper",
            at=when,
            bar="2024-01-05T17:30:00+00:00",
            leg={
                "entry_idx": 1, "exit_idx": 3, "direction": "long",
                "entry_price": 100.0, "exit_price": 99.0,
                "ret": equity_ret, "equity_ret": equity_ret, "weight": 1.0,
                "bars": 2, "reason": "signal", "skipped": False,
            },
        ),
    )


def _account(equity, last_equity, env: str = "paper"):
    return EnvAccount(env=env, fields=(("equity", equity), ("last_equity", last_equity)))


def test_the_limits_read_nothing_when_they_are_not_configured(tmp_path, armed, monkeypatch):
    """Empty means NOT APPLIED, and it must cost nothing.

    A strategy that does not use these settings must not start making a broker call per
    tick, and must not have a new way to behave differently — the tick below is the same
    tick it was before the limits existed.
    """
    settings = armed()

    def never(settings, env, force=False):
        raise AssertionError("the account was read for a strategy with no loss limits")

    monkeypatch.setattr(orchestrator.accounts, "snapshot", never)
    driver = _driver(settings)
    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt is None
    assert record["action"] == "decided" and record["reason"] == ""
    assert record["intents"][0]["intent"] == "open", "the entry went through"


def test_a_healthy_day_is_not_halted(tmp_path, armed, monkeypatch):
    """Both settings configured, nothing breached: the bot trades as usual."""
    settings = armed(max_loss_percent=2.0, max_consecutive_losses=3)
    monkeypatch.setattr(
        orchestrator.accounts, "snapshot", lambda s, env, force=False: _account(101.0, 100.0)
    )
    driver = _driver(settings)
    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt is None
    assert record["reason"] == "" and record["intents"][0]["intent"] == "open"


def test_a_losing_streak_halts_the_day_and_the_refusal_is_logged(tmp_path, armed):
    """The refusal has to be visible everywhere the day is read.

    The tick record carries it (the Trading panel shows the last tick's reason), the intent is
    a SKIP (the engine booked the veto rather than creating a position), and the trade log
    holds the skipped leg — which is also why the tally has to ignore skipped rows.
    """
    settings = armed(max_consecutive_losses=1)
    _closed_trade(settings, _at("2024-01-05 13:05"))
    driver = _driver(settings)

    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt and "MAX_CONSECUTIVE_LOSSES=1" in driver.halt, driver.halt
    assert record["action"] == "decided", "the bar was decided; the entry was refused"
    assert record["reason"] == driver.halt
    assert record["intents"][0]["skipped"] is True
    assert orchestrator.store.read_orders(settings, STRATEGY) == [], "nothing was sent"
    rows = orchestrator.store.read_trades(settings, STRATEGY, when="2024-01-05")
    assert [row["skipped"] for row in rows] == [False, True], "the day's loss, then the refusal"
    assert rows[1]["reason"] == driver.halt
    assert orchestrator.store.read_ticks(settings, STRATEGY, "2024-01-05")[0]["reason"] == driver.halt


def test_yesterdays_losses_do_not_halt_today(tmp_path, armed):
    """The tally is the EXCHANGE day's, which is what lets a halt clear itself.

    Measured across days, a streak could only be broken by a win — and a halted bot takes no
    trades, so there would be no win to break it with.
    """
    settings = armed(max_consecutive_losses=1)
    _closed_trade(settings, _at("2024-01-04 14:05"))
    driver = _driver(settings)

    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt is None, "yesterday's loss is yesterday's"
    assert record["intents"][0]["intent"] == "open", "and today's entry went through"
    assert len(orchestrator.store.read_orders(settings, STRATEGY)) == 1


def test_the_percent_limit_halts_on_the_days_drawdown(tmp_path, armed, monkeypatch):
    """Measured on the ACCOUNT, so a drawdown that is still open counts too."""
    settings = armed(max_loss_percent=2.0)
    monkeypatch.setattr(
        orchestrator.accounts, "snapshot", lambda s, env, force=False: _account(97.0, 100.0)
    )
    driver = _driver(settings)

    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt and "MAX_LOSS_PERCENT=2" in driver.halt, driver.halt
    assert "3.00%" in driver.halt, "the real move, so it can be checked: " + driver.halt
    assert record["reason"] == driver.halt
    assert orchestrator.store.read_orders(settings, STRATEGY) == []


def test_an_unreadable_account_halts_when_the_percent_limit_is_set(tmp_path, armed, monkeypatch):
    """FAIL CLOSED, and with the broker's own words.

    The limit is configured, so the day's loss is a number that matters — and an account we
    cannot read is not evidence that it is zero. The reason is carried through rather than
    replaced, because it is the only thing that says whether this clears in a second or in
    an hour.
    """
    settings = armed(max_loss_percent=2.0)
    monkeypatch.setattr(
        orchestrator.accounts,
        "snapshot",
        lambda s, env, force=False: EnvAccount(
            env=env, known=False, reason="the paper account could not be read (401 Unauthorized)"
        ),
    )
    driver = _driver(settings)
    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt and "MAX_LOSS_PERCENT is set" in driver.halt, driver.halt
    assert "401 Unauthorized" in driver.halt, "the reason the account could not be read"
    assert record["intents"][0]["skipped"] is True
    assert orchestrator.store.read_orders(settings, STRATEGY) == [], "nothing was sent"


# ---------------------------------------------------------------------------
# the code this process is running
# ---------------------------------------------------------------------------
def test_a_loop_running_older_code_than_its_own_source_refuses_new_entries(
    tmp_path, armed, monkeypatch
):
    """Python loads a module once, so an edit under a running loop is one it never read.

    Nothing inside the process can see that for itself: every test imports the code fresh, so
    the suite passes while this process runs a strategy nobody is looking at. The watch list is
    the substitute — and since the baseline is the mtimes the process IMPORTED, faking a stale
    loop means backdating that baseline rather than touching a file.
    """
    settings = armed()
    monkeypatch.setattr(
        orchestrator, "LOADED_SOURCE", dict.fromkeys(orchestrator.LOOP_SOURCE, 0.0)
    )
    driver = _driver(settings)

    record = orchestrator.tick(
        settings, now=_at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock,
        driver=driver,
    )

    assert driver.halt and "older code" in driver.halt, driver.halt
    assert "src/strategy/engine.py" in driver.halt, "it names what moved, so it can be checked"
    assert record["action"] == "decided", "the bar was decided; the ENTRY was what refused"
    assert record["intents"][0]["skipped"] is True
    assert record["reason"] == driver.halt
    assert orchestrator.store.read_orders(settings, STRATEGY) == [], "nothing was sent"


def test_the_loop_watches_the_code_that_decides_what_to_trade():
    """A watch list that quietly stopped naming the engine would protect nothing while still
    reporting that it was watching.

    The existence half matters as much as the membership half: a watched path that does not
    exist counts as CHANGED (``src.config.freshness`` treats a vanished file as movement), so a
    rename would refuse every entry with a message about a file nobody can find.
    """
    for rel in (
        "src/scheduler/orchestrator.py",
        "src/strategy/config.py",
        "src/strategy/engine.py",
        "src/strategy/limits.py",
        "src/strategy/live.py",
    ):
        assert rel in orchestrator.LOOP_SOURCE, rel
        assert (freshness.ROOT / rel).is_file(), f"{rel} is watched but does not exist"


# ---------------------------------------------------------------------------
# the run loop
# ---------------------------------------------------------------------------
def test_run_ticks_the_requested_number_of_times_and_returns_them(tmp_path, armed):
    settings = armed()
    slept = []

    records = orchestrator.run(
        settings, ticks=1, sleep=slept.append,
        clock=lambda: _at("2024-01-05 14:05"), sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings),
    )

    assert len(records) == 1 and records[0]["action"] == "decided"
    assert slept == [], "a bounded run does not sleep after its last tick"


def test_run_sleeps_to_the_next_boundary_after_a_tick(tmp_path, armed):
    settings = armed()
    slept = []
    moments = [_at("2024-01-05 14:05"), _at("2024-01-05 14:05")]

    orchestrator.run(
        settings, ticks=2, sleep=slept.append, clock=lambda: moments.pop(0),
        sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings),
    )

    # 14:05 -> the 13:30 bar closes at 14:30, plus the provider-lag allowance. The wait is
    # SLICED so that turning trading off stops the loop mid-sleep rather than at the boundary
    # (``_wait_for_the_boundary``), so what is asserted is the TOTAL wait — and that no single
    # slice outlasts the check interval, which is the property that makes the switch responsive.
    total = 25 * 60 + orchestrator.PROVIDER_LAG_SECONDS
    assert sum(slept) == pytest.approx(total, abs=1)
    assert len(slept) > 1, "one long sleep would not notice the switch until the boundary"
    assert max(slept) <= orchestrator.STOP_CHECK_SECONDS


def test_a_run_that_starts_late_decides_only_on_the_newest_closed_bar(tmp_path, armed):
    """No catch-up: the loop decides on the bar that just closed, not on the day it missed.

    The alternative — replaying the bars of a session it slept through — would place orders
    at prices that no longer exist, one per missed bar.
    """
    settings = armed()
    decided = []

    class Recorder(AlwaysBuy):
        def evaluate_frame(self, frame):
            decided.append(str(frame.index[-1]))
            return super().evaluate_frame(frame)

    driver = _driver(settings)
    driver.generator = Recorder()
    records = orchestrator.run(
        settings, ticks=1, sleep=lambda _s: None, clock=lambda: _at("2024-01-05 15:50"),
        sync_call=lambda: {}, clock_call=Calls().clock, driver=driver,
    )

    assert records[0]["action"] == "decided"
    # 15:50: the hourly grid is :30-past, so the bar that has closed is 14:30 — the 15:30
    # bar is still forming and closes at 16:00.
    assert decided == ["2024-01-05 14:30:00"], "the newest closed bar, and only that one"
    assert len(orchestrator.store.read_ticks(settings, STRATEGY, when=_at("2024-01-05 15:50"))) == 1


# ---------------------------------------------------------------------------
# what the run tells the host about itself
# ---------------------------------------------------------------------------
def test_the_loop_declares_the_wake_it_is_sleeping_towards(tmp_path, armed):
    """The claim is refreshed BEFORE the sleep, with the boundary the loop has committed to.

    That is what makes a lease readable rather than decorative: without it a reader sees a
    timestamp from the last tick and cannot tell an hour-long sleep from a dead process,
    and would have to refresh on a timer — which fights the very scheduling this design is
    built around.
    """
    settings = armed()
    claim = lease_mod.acquire(settings, strategy=STRATEGY)
    moments = [_at("2024-01-05 14:05"), _at("2024-01-05 14:05")]

    orchestrator.run(
        settings, ticks=2, sleep=lambda _s: None, clock=lambda: moments.pop(0), lease=claim,
        sync_call=lambda: {}, clock_call=Calls().clock, driver=_driver(settings),
    )

    record = lease_mod.read(settings)
    # 14:05 ET -> the 13:30 bar closes at 14:30 ET, which is 19:30Z.
    assert record["next_wake"] == "2024-01-05T19:30:00+00:00"
    assert record["pid"], "the claim still says who holds it"


def test_each_tick_uses_the_settings_resolved_for_that_tick(tmp_path, armed):
    """Why ``resolve`` exists: a change made in the dashboard lands at the NEXT boundary.

    The account layer answers from files with an mtime-keyed cache, so handing the loop a
    way to ask again is what makes a risk setting, an environment or a bar size take effect
    with no restart, no signal and no IPC. The record's own ``day`` is what proves which of
    the two settings the tick was actually handed: 23:00 in New York is already tomorrow
    in UTC, so the two produce different dates from the same moment.

    The switch stays ARMED for both ticks: a run that is off stops after one tick, which
    would make this a test about the switch rather than about resolution.
    """
    settings = armed()
    elsewhere = _settings(tmp_path, market_timezone="UTC")
    resolved = [settings, elsewhere]
    moments = [_at("2024-01-04 23:00"), _at("2024-01-04 23:00")]

    records = orchestrator.run(
        settings, ticks=2, sleep=lambda _s: None, clock=lambda: moments.pop(0),
        resolve=lambda: resolved.pop(0), sync_call=lambda: {}, clock_call=Calls().clock,
    )

    assert [r["day"] for r in records] == ["2024-01-04", "2024-01-05"]
