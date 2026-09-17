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

from src.config import state_files
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.data.dataset import save_dataset
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

    # 14:05 -> the 13:30 bar closes at 14:30, plus the provider-lag allowance.
    assert slept == [pytest.approx(25 * 60 + orchestrator.PROVIDER_LAG_SECONDS, abs=1)]


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
    """
    settings = armed()
    write_state(settings, {"on": False, "since": None})
    elsewhere = _settings(tmp_path, market_timezone="UTC")
    resolved = [settings, elsewhere]
    moments = [_at("2024-01-04 23:00"), _at("2024-01-04 23:00")]

    records = orchestrator.run(
        settings, ticks=2, sleep=lambda _s: None, clock=lambda: moments.pop(0),
        resolve=lambda: resolved.pop(0), sync_call=lambda: {}, clock_call=Calls().clock,
    )

    assert [r["day"] for r in records] == ["2024-01-04", "2024-01-05"]
