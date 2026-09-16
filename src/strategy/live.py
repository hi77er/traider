"""Running the strategy live: the same machine the backtest drives.

This module contains NO trading logic. It reacts to a newly closed bar, hands it to
:class:`src.strategy.engine.StrategyEngine`, submits whatever comes back to a broker,
records the fill, and remembers where it got to. Everything about *what* to do is in the
engine, so a backtest and a live run cannot disagree about it — and the parity test
(``tests/test_strategy_parity.py``) replays history through this driver and asserts it
produces the trades the backtest reported.

Three things this driver owns, because they are genuinely live-only:

* **the data window.** A decision needs the bars its indicators look back over
  (``src.data.live.required_bars``). Handed too few, every feature is NaN and the signal
  is HOLD forever — indistinguishable from a quiet market. So the driver REFUSES to run
  instead of trading nothing.
* **idempotency.** ``state.last_decided_bar`` is the bar whose signal has been acted on.
  A repeated tick for the same bar does nothing, and a restart cannot re-fire it.
* **reconciliation.** The broker is the truth about what is held. An exit the broker made
  on its own is BOOKED first (a resting bracket closing between two ticks is the ordinary
  way a live position ends, and refusing on it would wedge the bot for ever); anything left
  after that is genuine drift, and the driver refuses to submit until the two agree,
  because trading on a position you are wrong about is how a bot doubles up or sells
  something it does not own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from src.config import artifacts, state_files
from src.data import dataset
from src.strategy.broker import Broker, BrokerPosition, ClosingFill, Fill
from src.strategy.engine import (
    CLOSE,
    FORCED,
    OPEN,
    SKIP,
    Bar,
    Intent,
    Ledger,
    StrategyEngine,
    bar_from_row,
)
from src.strategy.state import StrategyState

logger = logging.getLogger(__name__)

__all__ = ["LiveDriver", "NotEnoughHistory"]

STATE_FILE = "strategy_state.json"


class NotEnoughHistory(RuntimeError):
    """Raised when a decision is asked for on too little history.

    Loud on purpose: the alternative — deciding anyway — evaluates NaN features and
    returns HOLD for ever, which looks like a working bot with nothing to do.
    """


class LiveDriver:
    """One strategy, one broker, one state file."""

    def __init__(
        self,
        *,
        settings,
        engine: StrategyEngine,
        generator,
        state: Optional[StrategyState] = None,
        broker: Optional[Broker] = None,
        state_path: Optional[Path] = None,
        dry_run: bool = False,
        name: Optional[str] = None,
    ):
        self.settings = settings
        self.engine = engine
        self.generator = generator          # the shared rule engine (features + signals)
        self.state = state or StrategyState()
        self.broker = broker
        self.dry_run = bool(dry_run)
        # WHAT this driver is. The strategy comes first, and not for cosmetics: it is the
        # identity the state file is keyed by and the name the store puts on the tree, so
        # falling back to the instrument (which is what this used to do) gave two
        # strategies on the same symbol ONE position between them.
        #
        # A caller may pass it explicitly and the loop DOES: it must be able to run the
        # strategy STAMPED in trading.json even when the active one has changed underneath,
        # and looking that up here would answer with the wrong one.
        self.name = str(name or _active_strategy() or getattr(settings, "instrument", "strategy"))
        self.env = str(getattr(settings, "execution_env", "paper") or "paper").strip().lower()
        self.state_path = Path(state_path) if state_path else artifacts.live_state_path(
            settings, self.name, self.env
        )
        if state is None:
            # Recover the memory from the file, because a NEW instance has to be the SAME
            # driver as the one that decided the last bar — the loop builds one per tick, so
            # anything kept only in memory made every tick a first tick: the same bar
            # decided again on each pass, and a position the driver had forgotten it owned.
            # An explicitly passed ``state`` is left alone: it is how a test or a replay
            # hands a run a starting point, and overwriting it from disk would ignore it.
            self.load_state()
        self.ledger = Ledger(n=0, opens=[], closes=[])
        self.ledger.finalise(self.state)
        self.log: list = []

    # -- history -----------------------------------------------------------
    @property
    def required_bars(self) -> int:
        from src.data import live as live_data

        return live_data.required_bars(self.settings)

    def check_history(self, candles: pd.DataFrame) -> None:
        """Refuse to decide on a window too short for the configured features."""
        have = 0 if candles is None else int(len(candles))
        need = self.required_bars
        if have < need:
            raise NotEnoughHistory(
                f"{self.name}: {have} bar(s) available, {need} needed for the configured "
                f"features (FEATURES_MIN_LOOKBACK={getattr(self.settings, 'features_min_lookback', '?')}) "
                "— refusing to decide on NaN features"
            )

    # -- state -------------------------------------------------------------
    def load_state(self) -> StrategyState:
        if self.state_path.exists():
            self.state = StrategyState.from_dict(
                state_files.read_json(self.state_path, default={})
            )
        return self.state

    def save_state(self) -> None:
        state_files.write_json(self.state_path, self.state.as_dict())

    # -- reconciliation ----------------------------------------------------
    def adopt_broker_exit(self, bar: Bar) -> Optional[Dict[str, Any]]:
        """Book an exit the BROKER made, so the local state stops disagreeing.

        The case this exists for: a resting bracket's stop or take-profit fires between two
        ticks. The strategy is asleep, the market is not — by the next tick the broker is
        flat and the local state still holds a position. Refusing is right for "we disagree
        about reality" and wrong for "my exit already happened and I can prove it at what
        price", so the provable case is booked here instead of blocking for ever.

        Returns what it adopted, or ``None`` when there was nothing to adopt. ``None`` is
        the honest answer when the broker's history cannot say what closed the position:
        the driver then refuses, because an exit with no price is not something to book.
        """
        if self.broker is None:
            return None
        pos = self.state.position
        if pos is None:
            return None
        if not self.broker.position().is_flat:
            # Something IS held. That is drift, not an exit, and reconcile() decides.
            return None

        closing = self.broker.closing_fill(pos.short)
        if closing is None:
            return None

        intent = Intent(
            action=CLOSE,
            reason=closing.reason or FORCED,
            short=pos.short,
            expected_price=closing.price,
            level=closing.price,
        )
        # Through settle(), not book(): the adopted exit must land in the ledger, the
        # trade log and the stats by exactly the same path a stop the driver saw it make
        # would have used, or the two would disagree about the same trade.
        self.engine.settle(self.ledger, self.state, bar, intent, exit_price=closing.price)
        self.save_state()
        report = {
            "action": "adopted",
            "reason": intent.reason,
            "price": closing.price,
            "bar": str(bar.time),
            "order_id": closing.order_id,
            "detail": closing.detail or "the position was closed at the broker",
        }
        logger.warning(
            "%s: adopted an exit that happened at the broker — %s at %.4f (order %s)",
            self.name, intent.reason, closing.price, closing.order_id,
        )
        return report

    def reconcile(self) -> Optional[str]:
        """``None`` when local state and the broker agree, else why they do not.

        Returns a message rather than raising: a mismatch is a condition to report and
        stop on, not a crash — the dashboard shows it and the operator fixes it.

        An exit the broker made on its own is not a mismatch by the time this runs —
        :meth:`adopt_broker_exit` books it first. So what reaches here is genuine drift:
        a position only one side knows about, or a direction they disagree on. Refusing is
        right for all of it.
        """
        if self.broker is None:
            return None
        actual: BrokerPosition = self.broker.position()
        local = self.state.position
        if local is None and not actual.is_flat:
            return (
                f"broker holds {actual.quantity:g} {self.name} but local state is flat — "
                "refusing to trade until they agree"
            )
        if local is not None and actual.is_flat:
            # The adoption step already ran, so the broker closed this position without
            # leaving a record we could price. Book nothing and stop.
            return (
                f"local state holds a position in {self.name} but the broker is flat, and "
                "its order history does not show what closed it — refusing to trade until "
                "they agree"
            )
        if local is not None and local.short != bool(actual.short):
            return (
                f"local state says {'short' if local.short else 'long'} in {self.name} but "
                f"the broker says {'short' if actual.short else 'long'} — refusing to trade "
                "until they agree"
            )
        return None

    # -- the tick ----------------------------------------------------------
    def on_bar_closed(self, candles: pd.DataFrame, next_bar: Optional[Bar] = None) -> Dict[str, Any]:
        """Act on the most recently CLOSED bar. Returns a small report.

        ``candles`` is the trailing window ending at the bar that has just closed — the
        signal bar. ``next_bar`` is the bar that is now starting, whose open is where a
        fill would happen; a simulated live run (the parity test, a dry run over
        history) supplies its real OHLC so the ranges match a backtest exactly, while
        true live passes nothing and the stop/take belong to the broker's resting
        orders instead of to an intrabar assumption.
        """
        self.check_history(candles)
        candles = candles.sort_index()
        signal_bar = candles.index[-1]
        # CANONICAL, not ``str(signal_bar)``. The idempotency key has to survive the way the
        # bar was written down: a daily index stringifies as "2026-09-15 00:00:00+00:00" and
        # an hourly one as "2026-09-15 15:30:00", so a key stored from one and compared
        # against the other never matches — and a non-matching key re-fires a decision on a
        # bar that was already acted on, which costs money rather than time.
        bar_key = dataset.bar_key(self.settings, signal_bar)
        if self.state.last_decided_bar == bar_key:
            return {"action": "noop", "reason": "this bar was already decided", "bar": bar_key}

        frame = self.generator.evaluate_frame(candles)
        act = str(frame["signal"].iloc[-1])

        # The bar being decided is the one that STARTS here. A replay hands over the real
        # next bar, index included, so a live run over history numbers bars exactly as the
        # backtest does; true live has no bar to hand over, so the driver counts its own.
        fill_bar = next_bar or bar_from_row(
            index=self.state.bar_index + 1,
            time=signal_bar,
            row=candles.iloc[-1],
        )
        self.engine.observe_close(float(candles["close"].iloc[-1]))

        # A resting exit can have closed the position while this driver slept between
        # bars. Adopt it BEFORE reconciling: the mismatch it creates is not drift, and
        # leaving it unbooked would refuse every future tick for ever.
        adopted = self.adopt_broker_exit(fill_bar)

        mismatch = self.reconcile()
        if mismatch:
            logger.warning(mismatch)
            return {"action": "refused", "reason": mismatch, "bar": bar_key}

        intents = self.engine.step(self.state, fill_bar, act)
        done = []
        for intent in intents:
            done.append(self._act(fill_bar, intent))

        self.state.bar_index = fill_bar.index
        self.state.last_decided_bar = bar_key
        self.save_state()
        self.log.extend(done)
        report = {"action": "decided", "bar": bar_key, "signal": act, "intents": done}
        if adopted is not None:
            report["adopted"] = adopted
        return report

    def _act(self, bar: Bar, intent: Intent) -> Dict[str, Any]:
        """Submit one intent and book what came back."""
        if intent.action == SKIP:
            self.engine.settle(self.ledger, self.state, bar, intent)
            return {"intent": intent.action, "reason": intent.reason, "skipped": True}

        fill = self._submit(intent)
        # What the broker really paid wins over the expected price. For the simulated
        # broker they are the same by construction; for a real one they can differ, and
        # that difference is the honest cost of trading rather than a modelling choice.
        real = fill.price if fill.filled else None
        if intent.action == OPEN:
            report = {
                "intent": intent.action,
                "reason": intent.reason,
                "price": real if real is not None else intent.expected_price,
                "expected": intent.expected_price,
                "status": fill.status,
            }
            if real is not None:
                self._reprice_entry(real)
                exits = self._reprice_exits()
                if exits is not None:
                    report["exits"] = exits
            return report
        if intent.action == CLOSE:
            self.engine.settle(self.ledger, self.state, bar, intent, exit_price=real)
            return {
                "intent": intent.action,
                "reason": intent.reason,
                "price": real if real is not None else intent.expected_price,
                "expected": intent.expected_price,
                "status": fill.status,
            }
        return {"intent": intent.action, "reason": intent.reason}

    def _submit(self, intent: Intent) -> Fill:
        if self.broker is None:
            # No broker wired (a dry run that only wants decisions): the expectation is
            # the fill, which is exactly what the simulated broker would say.
            return Fill(price=intent.expected_price, detail="no broker wired")
        if self.dry_run and intent.action == CLOSE and intent.reason == "signal":
            logger.info("[dry run] would close %s", self.name)
        return self.broker.submit(intent)

    def _reprice_entry(self, price: float) -> None:
        """Adopt the real fill price, and re-derive the levels from it.

        A stop is a function of what was PAID, not of what was hoped for, so resting a
        stop computed from the expected price would rest it in the wrong place.
        ``raw_entry_price`` is left alone: it is the untouched price at that bar's open,
        and the gap between it and ``entry_price`` is the cost of trading.
        """
        pos = self.state.position
        if pos is None:
            return
        pos.entry_price = float(price)
        pos.stop, pos.take = self.engine.levels(pos.entry_price, pos.short)

    def _reprice_exits(self) -> Optional[Dict[str, Any]]:
        """Move the broker's resting exits to the levels the REAL fill implies.

        The bracket goes out with the exits derived from the price the strategy EXPECTED,
        because waiting for the fill before sending anything would leave the entry naked
        for however long the round trip takes. Once it fills elsewhere, the levels have to
        follow the money that was actually spent: a long filled above expectation would
        otherwise rest its stop further away than the position was sized for, so the real
        risk per trade would exceed ``RISK_LIMIT_PERCENT`` while the backtest said it did
        not.

        The levels come from :meth:`StrategyEngine.levels` — the same call the bracket was
        built from — so this is one derivation applied twice, not a second opinion. A
        broker with no resting orders (the simulated one) does nothing; see
        ``src/strategy/broker.py``.
        """
        if self.broker is None:
            return None
        pos = self.state.position
        if pos is None:
            return None
        return self.broker.reprice_exits(pos.stop, pos.take)


def _safe(name: str) -> str:
    """Retired — use ``src.config.artifacts.slug``, which is what the store uses.

    Kept as an alias for one release so nothing that imported it breaks silently. The two
    sanitisers disagreed on anything with a space, a dash or a non-ASCII letter
    (``"My Strategy"`` became ``My_Strategy`` here and ``My-Strategy`` there), which would
    have put one strategy's state file in a different directory from its results.
    """
    return artifacts.slug(name)


def _active_strategy() -> Optional[str]:
    """The active strategy's name, or ``None`` when it cannot be determined.

    Imported and called defensively: a driver must still be constructible for a parity test
    or a dry run with no strategy store on disk at all, and the fallback is the instrument.
    """
    try:
        from src.config.effective import active_strategy_name

        return active_strategy_name()
    except Exception:  # noqa: BLE001 - identity is not worth failing a run over
        return None
