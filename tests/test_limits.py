"""The day's loss limits: when the loop stops OPENING things, and why.

Two halves, both about ``src/strategy/limits.py``:

* the POLICY, which is a pure function of facts and is tested as one — no broker, no
  files, no clock; and
* the DRIVER's part, which is turning an entry into a refusal. That conversion is the
  whole mechanism, so the tests here are about what does NOT happen: the broker is never
  asked, and the trade log records the refusal rather than the entry.

The one thing that must never break is the exception built into it: a halted day refuses
NEW risk and still lets an open position leave. A loss limit that also blocked the exits
would not be a limit, it would be a way of holding a loser until it gets worse.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.config.settings import Settings
from src.strategy.broker import SimulatedBroker
from src.strategy.config import StrategyConfig
from src.strategy.engine import StrategyEngine
from src.strategy.limits import day_halt_reason, day_loss_percent, losing_streak
from src.strategy.live import LiveDriver

# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


def _config(**kw) -> StrategyConfig:
    return StrategyConfig(**kw)


def _loss(equity_ret: float, **extra) -> dict:
    return {"skipped": False, "equity_ret": equity_ret, "ret": equity_ret, **extra}


# --- the percent limit -----------------------------------------------------
def test_no_limits_configured_means_no_halt() -> None:
    """Empty is NOT APPLIED — there is no master switch and no default limit."""
    assert day_halt_reason(_config()) is None


def test_the_percent_limit_halts_only_once_it_is_reached() -> None:
    """Down 1.99% with a 2% limit is not a halt. Down exactly 2% is."""
    config = _config(max_loss_percent=2.0)

    assert day_halt_reason(config, equity=98.01, last_equity=100.0) is None, "just short of it"
    halt = day_halt_reason(config, equity=98.0, last_equity=100.0)
    assert halt, "exactly at the limit is at the limit"
    assert "MAX_LOSS_PERCENT=2" in halt, f"the message must name the setting: {halt}"
    assert "2.00%" in halt, "and the day's real move, so it can be checked: " + halt


def test_a_day_that_is_up_is_never_a_loss() -> None:
    assert day_halt_reason(_config(max_loss_percent=2.0), equity=101.0, last_equity=100.0) is None


def test_a_zero_limit_halts_on_any_loss_and_not_on_a_flat_day() -> None:
    """``0`` is configured, not empty: it means "no loss is tolerated".

    And it must not halt a day that ended exactly flat — a limit of zero percent is a limit
    on LOSSES, and "down 0.00%" is not one. Without the ``pct < 0`` guard this refuses every
    entry on a quiet day.
    """
    config = _config(max_loss_percent=0.0)

    assert day_halt_reason(config, equity=100.0, last_equity=100.0) is None, "flat is not a loss"
    assert day_halt_reason(config, equity=99.99, last_equity=100.0), "any loss at all"


def test_an_unmeasurable_account_refuses_when_the_limit_is_set() -> None:
    """FAIL CLOSED: a limit that cannot be evaluated is not one that has been satisfied.

    The account reason is carried through rather than replaced, because "why could it not be
    read" is the only thing that tells the operator whether this clears in a second or in an
    hour.
    """
    config = _config(max_loss_percent=2.0)

    no_equity = day_halt_reason(
        config, equity=None, last_equity=None, account_reason="the live account could not be read (401)"
    )
    assert no_equity and "401" in no_equity, no_equity
    assert "MAX_LOSS_PERCENT is set" in no_equity, "it must say WHY the refusal is happening"

    no_denominator = day_halt_reason(config, equity=0.0, last_equity=0.0)
    assert no_denominator, "a percentage of nothing is not 0%, so it cannot be compared to a limit"


def test_without_the_percent_limit_the_account_is_never_needed() -> None:
    """The loop reads the account only when this setting is set — so an unreadable account
    must not halt a strategy that never asked for the limit."""
    config = _config(max_consecutive_losses=3)
    assert day_halt_reason(config, trades=[], equity=None, last_equity=None) is None


# --- the streak ------------------------------------------------------------
def test_the_streak_counts_only_the_run_at_the_end() -> None:
    assert losing_streak([_loss(-0.01), _loss(-0.01)]) == 2
    assert losing_streak([_loss(-0.01), _loss(0.02), _loss(-0.01)]) == 1, "the win resets it"
    assert losing_streak([_loss(0.01), _loss(-0.01)]) == 1
    assert losing_streak([]) == 0


def test_a_skipped_entry_neither_counts_nor_resets_the_streak() -> None:
    """The refusal this module issues must not feed back into the tally that issued it.

    Counting a skip would halt over trades that never happened; resetting on one would clear
    a halt that is still earned — the streak would look broken by the bot's own refusal.
    """
    skipped = {"skipped": True, "equity_ret": None}
    assert losing_streak([_loss(-0.01), skipped, _loss(-0.01)]) == 2, "still two losses"
    assert losing_streak([_loss(-0.01), skipped]) == 1, "the skip did not reset it"


def test_a_trade_with_no_measurable_return_is_passed_over() -> None:
    """Unknown is not a win, so it does not reset the run either."""
    assert losing_streak([_loss(-0.01), {"skipped": False, "equity_ret": None}]) == 1


def test_the_streak_follows_the_money_and_not_the_price() -> None:
    """``equity_ret`` is the leg's return times the weight it was sized at.

    A price move is not a loss: a trade that moved 5% against a position that was never
    taken sizeable enough to lose money has not lost money. The reverse matters more — a
    trade whose price rose but whose MONEY fell still counts as a loss, which is what a
    limit is for.
    """
    assert losing_streak([{"skipped": False, "ret": -0.05, "equity_ret": 0.0}]) == 0
    assert losing_streak([{"skipped": False, "ret": 0.05, "equity_ret": -0.01}]) == 1


def test_the_streak_limit_halts_at_its_limit() -> None:
    config = _config(max_consecutive_losses=2)

    assert day_halt_reason(config, trades=[_loss(-0.01)]) is None, "one loss, one allowed"
    halt = day_halt_reason(config, trades=[_loss(-0.01), _loss(-0.02)])
    assert halt
    assert "MAX_CONSECUTIVE_LOSSES=2" in halt, halt
    assert "2 losing trades in a row" in halt, halt


def test_a_streak_limit_of_zero_refuses_every_entry() -> None:
    """Configured, and it means what it says: not one losing trade is tolerated.

    The tally starts at zero, so zero consecutive losses is already true — and the message
    says so plainly instead of leaving an operator to guess why nothing is being opened.
    """
    halt = day_halt_reason(_config(max_consecutive_losses=0), trades=[])
    assert halt and "MAX_CONSECUTIVE_LOSSES=0" in halt, halt


def test_both_limits_report_together() -> None:
    """Two breaches, one sentence: the panel shows one reason field, so it must hold both."""
    halt = day_halt_reason(
        _config(max_loss_percent=2.0, max_consecutive_losses=2),
        trades=[_loss(-0.01), _loss(-0.01)],
        equity=97.0,
        last_equity=100.0,
    )
    assert halt
    assert "MAX_LOSS_PERCENT" in halt and "MAX_CONSECUTIVE_LOSSES" in halt, halt
    assert halt.count("no new entries until the next exchange day") == 1, "one closing clause"


# --- the arithmetic --------------------------------------------------------
def test_the_day_percent_is_measured_against_the_days_opening_equity() -> None:
    assert day_loss_percent(98.0, 100.0) == pytest.approx(-2.0)
    assert day_loss_percent(101.0, 100.0) == pytest.approx(1.0)
    assert day_loss_percent(None, 100.0) is None
    assert day_loss_percent(100.0, None) is None
    assert day_loss_percent(100.0, 0.0) is None, "no denominator, so no percentage"


# ---------------------------------------------------------------------------
# the driver: an entry becomes a refusal, an exit does not
# ---------------------------------------------------------------------------
HALT = "the day's loss limit is in force — 3 losing trades in a row"


def _settings(tmp_path, **kw) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="AAPL",
        historical_bar_size="1h",
        features_min_lookback=3,
        stop_loss_percent=2.0,
        take_profit_percent=4.0,
        alpaca_paper_api_key="PK-PAPER",
        alpaca_paper_api_secret="S-PAPER",
    )
    values.update(kw)
    return Settings(**values)


def _frame(n: int = 60, shift: int = 0) -> pd.DataFrame:
    """``n`` half-hourly bars from 09:30, optionally shifted by ``shift`` hours.

    Sixty bars because that is what the configured features need before they will produce a
    signal at all — a shorter window is REFUSED rather than decided on, so a small fixture
    would test that refusal instead of the thing each test is about.

    ``shift`` is how a test hands the same driver a later bar: the signal bar is the newest
    one in the window, so a frame that starts later is a different decision.
    """
    start = pd.Timestamp("2024-01-05 09:30") + pd.Timedelta(hours=shift)
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=30 * i) for i in range(n)])
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


class Scripted:
    """One signal per call, in order — so a test can open and then close."""

    rules = [object()]

    def __init__(self, *signals):
        self.signals = list(signals)
        self.calls = 0

    def evaluate_frame(self, frame):
        self.calls += 1
        signal = self.signals[min(self.calls, len(self.signals)) - 1]
        return frame.assign(signal=signal)


class CountingBroker(SimulatedBroker):
    """A simulated broker that remembers whether it was asked to do anything."""

    def __init__(self):
        super().__init__()
        self.submitted = []

    def submit(self, intent, client_order_id=None):
        self.submitted.append(intent.action)
        return super().submit(intent, client_order_id=client_order_id)


def _driver(tmp_path, signals, *, broker=None, halt=None, name="Alpha") -> LiveDriver:
    """A driver for ``tmp_path``, reading its state from disk exactly as the loop does.

    The state is NOT passed in: the loop builds a driver per tick and the driver recovers
    what it did last time from its own file, so a test that handed over a fresh state would
    be testing a driver the loop never has — one that has forgotten its position.
    """
    settings = _settings(tmp_path)
    return LiveDriver(
        settings=settings,
        engine=StrategyEngine(StrategyConfig.from_settings(settings)),
        generator=Scripted(*signals),
        broker=broker if broker is not None else CountingBroker(),
        halt=halt,
        name=name,
    )


def test_a_halted_day_refuses_the_entry_before_the_broker_is_asked(tmp_path) -> None:
    """The refusal is a SKIP, not a dropped intent.

    A SKIP goes through the ledger, so the day's log shows an entry that was refused and
    why. Dropping it would leave a bar that silently produced nothing — indistinguishable
    from a bug, and impossible to explain to anyone reading the log.
    """
    broker = CountingBroker()
    driver = _driver(tmp_path, ["BUY"], broker=broker, halt=HALT)

    report = driver.on_bar_closed(_frame())

    assert broker.submitted == [], "nothing was sent to the broker"
    assert report["action"] == "decided", "the bar was still decided — it was not skipped"
    assert report["reason"] == HALT, "and the record says why nothing was opened"
    assert report["intents"][0]["skipped"] is True, report["intents"]
    assert report["intents"][0]["reason"] == HALT
    # The refusal IS carried in ``trades``, because the loop writes every leg to the trade
    # log — and that is the only reason ``losing_streak`` has to ignore skipped rows. What
    # must not happen is a leg that counts: ``ledger.trades`` filters them out.
    assert [leg["skipped"] for leg in report["trades"]] == [True], report["trades"]
    skips = [leg for leg in driver.ledger.legs if leg.get("skipped")]
    assert len(skips) == 1 and skips[0]["reason"] == HALT
    assert driver.ledger.trades == [], "and it is not counted as a trade either"
    assert driver.state.position is None, "no position was created for an order never sent"


def test_a_halted_day_still_lets_an_open_position_leave(tmp_path) -> None:
    """The exception, and the reason the whole design works.

    The position is opened on a normal bar, then the day hits its limit. The exit must go
    through: a limit that also refused the exit would convert "stop opening new trades" into
    "hold this loser until it gets worse", which is the opposite of what it is for.
    """
    broker = CountingBroker()
    opened = _driver(tmp_path, ["BUY"], broker=broker)
    assert opened.on_bar_closed(_frame())["action"] == "decided"
    assert broker.submitted == ["open"], "the first bar opened a position"

    halted = _driver(tmp_path, ["SELL"], broker=broker, halt=HALT)
    report = halted.on_bar_closed(_frame(shift=2))

    assert broker.submitted == ["open", "close"], "the exit was submitted, halt or no halt"
    assert report["reason"] == HALT, "the day is still halted, and the record says so"
    assert len(report.get("trades") or []) == 1, "and the closed trade was booked"
    assert halted.state.position is None


def test_the_halt_is_reported_even_when_the_signal_asked_for_nothing(tmp_path) -> None:
    """A HOLD on a halted day is still a halted day.

    "This strategy is not taking entries today" is the thing an operator staring at a quiet
    panel needs to know, and a HOLD produces no intent at all — so the reason field on the
    tick is the ONLY place it can be said.
    """
    report = _driver(tmp_path, ["HOLD"], halt=HALT).on_bar_closed(_frame())
    assert report["intents"] == [], "a HOLD decides nothing to do, halt or no halt"
    assert report["reason"] == HALT, "and the day's state is still reported"


def test_a_driver_with_no_halt_behaves_exactly_as_before(tmp_path) -> None:
    """The default is ``None``, which is what a replay and a backtest get."""
    broker = CountingBroker()
    report = _driver(tmp_path, ["BUY"], broker=broker).on_bar_closed(_frame())

    assert broker.submitted == ["open"]
    assert "reason" not in report, "an ordinary decided bar carries no reason"
    assert report["intents"][0]["intent"] == "open", "and nothing was refused"
    assert "trades" not in report, "an entry on its own closes nothing"


def test_the_halt_can_be_replaced_between_ticks(tmp_path) -> None:
    """The loop assigns it per tick, so the driver must not hold on to the verdict.

    A halt lifts when the exchange day turns over, and the loop builds a driver per tick — a
    driver that remembered the first verdict it was given would keep a cleared day halted
    until the process restarted.
    """
    broker = CountingBroker()
    driver = _driver(tmp_path, ["BUY", "BUY"], broker=broker, halt=HALT)
    assert driver.on_bar_closed(_frame())["reason"] == HALT
    assert broker.submitted == [], "the first bar was refused"

    driver.halt = None
    report = driver.on_bar_closed(_frame(shift=2))

    assert "reason" not in report, "the next bar is not halted, and does not say it is"
    assert broker.submitted == ["open"], "and the entry went through"
