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
* **reconciliation.** The broker is the truth about what is held. If it disagrees with
  the local state the driver refuses to submit until they agree, because trading on a
  position you are wrong about is how a bot doubles up or sells something it does not
  own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from src.config import state_files
from src.strategy.broker import Broker, BrokerPosition, Fill
from src.strategy.engine import (
    CLOSE,
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
        self.name = name or str(getattr(settings, "instrument", "strategy"))
        self.state_path = Path(state_path) if state_path else state_files.state_path(
            settings, f"strategy_state_{_safe(self.name)}.json"
        )
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
    def reconcile(self) -> Optional[str]:
        """``None`` when local state and the broker agree, else why they do not.

        Returns a message rather than raising: a mismatch is a condition to report and
        stop on, not a crash — the dashboard shows it and the operator fixes it.
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
            return (
                f"local state holds a {local.direction if hasattr(local, 'direction') else 'position'} "
                f"in {self.name} but the broker is flat — refusing to trade until they agree"
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
        bar_key = str(signal_bar)
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
        return {"action": "decided", "bar": bar_key, "signal": act, "intents": done}

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
            if real is not None:
                self._reprice_entry(real)
            return {
                "intent": intent.action,
                "reason": intent.reason,
                "price": real if real is not None else intent.expected_price,
                "expected": intent.expected_price,
                "status": fill.status,
            }
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


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))[:60]
