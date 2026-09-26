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

Two things this driver deliberately does NOT keep, both of them positions the broker never
opened:

* **an entry the broker REFUSED.** The engine records a position while it builds the intent,
  before anything is sent, so a refusal used to leave one behind — and the tick after it
  stopped on "the broker is flat and we are not", with nothing in the broker's history ever
  able to settle it. A refusal is a refusal of the POSITION: the local record is dropped (the
  order row the loop writes is the whole record of the attempt, and the strategy is free to
  try again on the next bar), and ``drop_never_opened_position`` clears one left behind by an
  older run.
* **an exit for a position that does not exist.** A stop can fire inside the bar its entry
  filled, so one bar can produce an entry and an exit — and when the entry was refused, the
  exit is about nothing. It is not sent, and not logged as an order the broker ever saw.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from src.config import artifacts, state_files
from src.data import dataset
from src.strategy.broker import REJECTED, Broker, BrokerPosition, ClosingFill, Fill
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
from src.strategy.state import Position, StrategyState

logger = logging.getLogger(__name__)

__all__ = ["LiveDriver", "NotEnoughHistory", "order_id_for"]

STATE_FILE = "strategy_state.json"

#: Alpaca caps a client order id at 48 characters. Ours is 31, with room to spare.
ORDER_ID_PREFIX = "traider"
ORDER_ID_NAME_CHARS = 12
ORDER_ID_DIGEST_CHARS = 10


def order_id_for(name: str, env: str, bar_key: str, index: int) -> str:
    """The broker's name for one order: the SAME string every time this bar is decided.

    This is an idempotency key, and the reason it is derived rather than generated is the
    crash in the middle of a tick. The driver saves its state LAST, deliberately, so a tick
    that dies after submitting leaves the bar undecided and retryable — but "retryable" is
    only safe if the retry is the SAME order. Alpaca deduplicates on the client order id, so
    a fixed id turns a possible second position into a refused duplicate, and a fresh uuid
    would do the opposite. It also makes an order in the broker's own dashboard traceable
    back to a strategy and a bar without the local log, which is the one thing a deleted
    log cannot otherwise tell you.

    ``hashlib`` rather than ``hash()``: the latter is randomised per process, so it would
    produce a different id after a restart — exactly when the id has to be stable.
    """
    digest = hashlib.sha256(
        f"{name}|{env}|{bar_key}|{int(index)}".encode("utf-8")
    ).hexdigest()[:ORDER_ID_DIGEST_CHARS]
    label = artifacts.slug(name)[:ORDER_ID_NAME_CHARS]
    return f"{ORDER_ID_PREFIX}-{label}-{digest}"


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
        halt: Optional[str] = None,
    ):
        self.settings = settings
        self.engine = engine
        self.generator = generator          # the shared rule engine (features + signals)
        self.state = state or StrategyState()
        self.broker = broker
        self.dry_run = bool(dry_run)
        # Why no NEW entry may be opened today, or None. The LOOP owns the day's loss limits
        # (``src.strategy.limits``) and hands the verdict down, because measuring them needs
        # the trade log and the account's equity, and ``src/strategy`` may reach neither.
        # A halt refuses what the machine wants to OPEN; it never blocks an exit.
        self.halt = halt or None
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
            # The size the broker's exit filled. No order row exists for it — the loop did not
            # place it — so this report is the only place the loop can learn the shares that
            # turn the round trip's return into money (see ``orchestrator._filled_size``).
            "filled_qty": closing.quantity,
            "detail": closing.detail or "the position was closed at the broker",
        }
        logger.warning(
            "%s: adopted an exit that happened at the broker — %s at %.4f (order %s)",
            self.name, intent.reason, closing.price, closing.order_id,
        )
        return report

    def adopt_broker_position(self, bar: Bar) -> Optional[Dict[str, Any]]:
        """Take ownership of a position the broker holds and the local state does not know about.

        The case: trading was armed while something was already open — the operator's own
        position, or one this strategy opened in a run whose state file is gone. Arming is
        ALLOWED on top of one now, so refusing here would make "trading on" a switch that never
        trades: every tick would stop on the same drift, and the position would sit unmanaged
        either way.

        Adoption is what makes the position the STRATEGY's, and it is the whole of the answer to
        "the next signal must be a sell": ``StrategyEngine.step`` opens nothing while a position
        is held, so from the moment this returns, the only orders this driver can send are the
        exits — a signal turn, or a level the engine tests the bars against.

        The levels come from the broker's own resting exits when there are any, and from the
        configuration only as a fallback. That order is deliberate: the orders are what actually
        protect the position, and a stop placed before a settings edit must keep the level it was
        sized for rather than moving because the configuration did.

        Returns what it adopted, or ``None`` when there was nothing it could adopt — a position
        whose entry price the broker cannot report is left to :meth:`reconcile`, which refuses
        loudly rather than inventing the levels a stop would be derived from.
        """
        if self.broker is None:
            return None
        if self.state.position is not None:
            return None
        actual = self.broker.position()
        if actual.is_flat:
            return None
        entry_px = actual.entry_price
        if entry_px is None or not float(entry_px):
            logger.warning(
                "%s: the broker holds %g %s but reported no entry price — cannot adopt it",
                self.name, actual.quantity, self.name,
            )
            return None

        entry_px = float(entry_px)
        derived_stop, derived_take = self.engine.levels(entry_px, actual.short)
        resting = self.broker.resting_levels() or {}
        stop_lvl = resting.get("stop")
        take_lvl = resting.get("take")
        stop_lvl = derived_stop if stop_lvl is None else float(stop_lvl)
        take_lvl = derived_take if take_lvl is None else float(take_lvl)

        self.state.position = Position(
            entry_index=bar.index,
            entry_price=entry_px,
            raw_entry_price=entry_px,
            short=bool(actual.short),
            stop=stop_lvl,
            take=take_lvl,
            weight=float(self.state.first_weight or 1.0),
            stop_pct=self.engine.config.stop_loss_percent,
        )
        self.save_state()
        logger.warning(
            "%s: adopted the %s position the broker holds — %g at %.4f, stop %s, take %s",
            self.name, "short" if actual.short else "long", actual.quantity, entry_px,
            stop_lvl if stop_lvl is not None else "none",
            take_lvl if take_lvl is not None else "none",
        )
        protected = resting.get("stop") is not None or resting.get("take") is not None
        return {
            "short": bool(actual.short),
            "quantity": float(actual.quantity),
            "price": entry_px,
            "stop": stop_lvl,
            "take": take_lvl,
            "order_id": None,
            "detail": (
                f"the {self.env} account already held {actual.quantity:g} {self.name} "
                f"({'short' if actual.short else 'long'}) when trading was armed, so the "
                f"strategy adopted it at {entry_px:.4f}; it will be CLOSED — by its "
                f"{'resting exit' if protected else 'strategy levels'}, or by a signal that "
                "turns — and nothing new is opened until it is"
            ),
        }

    def drop_never_opened_position(self) -> Optional[Dict[str, Any]]:
        """Forget a LOCAL position the state itself proves was never opened.

        The case: an entry the broker REFUSED, recorded before anything was sent, left its
        position behind (an older run did that; ``_act`` drops it now). The tick after it stopped
        on the mismatch — correctly, by the rule at the top of this module — and had no way out:
        the order history will never show a close for a position that was never opened, so the bot
        refused every bar for ever until someone edited a file by hand.

        So the evidence is read here instead. All of it has to hold: the state's own record of the
        last entry says the broker REJECTED it, and the broker is verifiably FLAT. A refusal is a
        position that never existed, and the local record of it is the only thing left saying
        otherwise. The caller runs this AFTER :meth:`adopt_broker_exit`, so a position the broker
        really did close has already been booked at the price its history proves — this cannot take
        a trade away from the log.

        Not a licence to ignore drift: a position with no such record, or one whose entry was
        merely not FILLED yet (which may still fill), still refuses.
        """
        if self.broker is None:
            return None
        pos = self.state.position
        if pos is None:
            return None
        entry = self.state.unfilled_entry or {}
        if str(entry.get("status") or "").lower() != REJECTED:
            return None
        if not self.broker.position().is_flat:
            # It IS held. Nothing to drop, and reconcile() reports the disagreement.
            return None

        self.state.position = None
        self.state.unfilled_entry = None
        self.save_state()
        logger.warning(
            "%s: dropped a position %s was holding that the broker never opened — its entry "
            "(%s) was rejected", self.name, self.env, entry.get("client_order_id") or "no id",
        )
        return {
            "bar": entry.get("bar"),
            "order_id": entry.get("client_order_id"),
            "detail": (
                f"a recorded position in {self.name} was dropped — the broker is flat and the "
                f"entry it came from was REFUSED ({entry.get('detail') or entry.get('status')}), "
                "so nothing was ever opened. The strategy trades on from here"
            ),
        }

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
            # The adoption step already ran, so either the broker closed this position without
            # leaving a record we could price, or nothing was ever opened. Book nothing and stop.
            return (
                f"local state holds a position in {self.name} but the broker is flat, and "
                "its order history does not show what closed it — refusing to trade until "
                "they agree" + self._unfilled_entry_note()
            )
        if local is not None and local.short != bool(actual.short):
            return (
                f"local state says {'short' if local.short else 'long'} in {self.name} but "
                f"the broker says {'short' if actual.short else 'long'} — refusing to trade "
                "until they agree"
            )
        return None

    def _unfilled_entry_note(self) -> str:
        """Why the local state may hold a position the broker does not have.

        One case still leaves one: an entry order the broker ACCEPTED but had not filled when the
        tick ended. It may fill at any moment, so the position is kept — and this is what names
        the order to go and look at if it never does. A REFUSED entry is not this case: it left a
        position behind only in runs before ``_act`` rolled them back, and
        :meth:`drop_never_opened_position` clears one of those before the caller ever gets here.
        """
        entry = self.state.unfilled_entry or {}
        if not entry:
            return ""
        when = entry.get("bar") or "the last entry"
        said = entry.get("detail") or entry.get("status") or "no fill"
        order = entry.get("order_id") or entry.get("client_order_id")
        named = f" (broker order {order})" if order else ""
        return (
            f". The entry for bar {when} was placed but had not filled when the tick ended: "
            f"{said}{named} — its position is recorded locally until it does"
        )

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

        # Everything booked during this call, so the loop can write it down. The ledger is
        # cumulative for the driver's life and the loop builds a driver per tick, so this
        # slice is the whole difference between "the trades so far" and "the trades now" —
        # and it is the only place a live exit price survives at all once the tick ends.
        booked_before = len(self.ledger.legs)

        # A resting exit can have closed the position while this driver slept between
        # bars. Adopt it BEFORE reconciling: the mismatch it creates is not drift, and
        # leaving it unbooked would refuse every future tick for ever.
        adopted = self.adopt_broker_exit(fill_bar)
        # The size that exit filled, carried on the tick: an exit the broker made has no order
        # row behind it, so without this the round trip it closed would have no money on it.
        adopted_size = (adopted or {}).get("filled_qty")
        # ...and a position the LOCAL state does not know about is taken on for the same reason,
        # from the other side: arming on top of one is an allowed decision, and a driver that
        # refused every bar over it would be armed and idle with no way out. Adopted, it is the
        # strategy's — and the engine can only close it (see ``adopt_broker_position``).
        taken_on = self.adopt_broker_position(fill_bar)
        # ...and a position the broker never opened has to go before the same reconcile, or it
        # wedges the bot for the same reason: nothing in the history can ever settle it.
        dropped = self.drop_never_opened_position()

        mismatch = self.reconcile()
        if mismatch:
            logger.warning(mismatch)
            # The refusal does not undo the booking: an exit the broker made happened, and
            # dropping it here is how a closed trade disappears from the log entirely.
            return {
                "action": "refused",
                "reason": mismatch,
                "bar": bar_key,
                "trades": self._booked_since(booked_before),
                "closed_qty": adopted_size,
            }

        intents = self.engine.step(self.state, fill_bar, act, veto=str(self.halt or ""))
        done = []
        for index, intent in enumerate(intents):
            # Named from the SIGNAL bar, not the fill bar: it is the bar a reader will look
            # the order up against, and it is the one the local record names too. The index
            # separates the orders one bar can produce — a close followed by an open.
            done.append(
                self._act(fill_bar, intent, order_id_for(self.name, self.env, bar_key, index))
            )

        self.state.bar_index = fill_bar.index
        self.state.last_decided_bar = bar_key
        self.save_state()
        self.log.extend(done)
        report = {"action": "decided", "bar": bar_key, "signal": act, "intents": done}
        if self.halt:
            # Carried even when nothing was skipped: "this strategy is not taking entries
            # today" is the first thing someone staring at a quiet panel needs to know, and
            # the tick record is where they will look for it.
            report["reason"] = self.halt
        booked = self._booked_since(booked_before)
        if booked:
            report["trades"] = booked
        if adopted is not None:
            report["adopted"] = adopted
            report["closed_qty"] = adopted_size
        if taken_on is not None:
            report["adopted_position"] = taken_on
        if dropped is not None:
            report["dropped"] = dropped
        return report

    def _booked_since(self, mark: int) -> list:
        """The legs booked since ``mark``, as plain dicts for the record to carry."""
        return [dict(leg) for leg in self.ledger.legs[mark:]]

    def _act(self, bar: Bar, intent: Intent, client_order_id: str = "") -> Dict[str, Any]:
        """Submit one intent and book what came back."""
        if intent.action == SKIP:
            self.engine.settle(self.ledger, self.state, bar, intent)
            return {"intent": intent.action, "reason": intent.reason, "skipped": True}
        if intent.action == CLOSE and self.state.position is None:
            # Nothing to close. The entry this exit was built on was refused a moment ago — a stop
            # can fire inside the bar its entry fills, so one bar can produce both — and a position
            # that does not exist is not an order: sending the exit would flatten nothing and land
            # in the orders log as something the broker never saw.
            return {
                "intent": intent.action,
                "reason": intent.reason,
                "skipped": True,
                "detail": "the entry was refused, so there was no position to close",
            }

        fill = self._submit(intent, client_order_id)
        # What the broker really paid wins over the expected price. For the simulated
        # broker they are the same by construction; for a real one they can differ, and
        # that difference is the honest cost of trading rather than a modelling choice.
        real = fill.price if fill.filled else None
        # The broker's id and ours, on every outcome including a rejected order: an order
        # that was refused still has a client_order_id, and that is what makes a rejection
        # traceable in the log rather than a line saying "something did not work".
        identity = {
            "order_id": fill.order_id,
            "client_order_id": fill.client_order_id or client_order_id or None,
        }
        if intent.action == OPEN:
            refused = fill.status == REJECTED
            if refused:
                # A refusal at the broker is the refusal of the POSITION. The engine records one
                # while it builds the intent — before anything is sent — so keeping it here left a
                # position that never existed, and the next tick stopped on the drift it created
                # and refused every bar after that. The attempt is not lost: the order row the loop
                # writes from this report carries the broker's status and its own words, and the
                # strategy is free to try again on the next bar.
                self.state.position = None
            # What to tell an operator if the two sides disagree about a position later. An order
            # that was PLACED and had not filled when the tick ended is the only case that leaves
            # one the broker does not have yet — it may fill at any moment — so it is the only one
            # worth naming.
            self.state.unfilled_entry = (
                None if real is not None or refused
                else {
                    "bar": str(getattr(bar, "time", "") or ""),
                    "status": fill.status,
                    "detail": fill.detail,
                    **identity,
                }
            )
            report = {
                "intent": intent.action,
                "reason": intent.reason,
                "price": real if real is not None else intent.expected_price,
                "expected": intent.expected_price,
                # How many shares the broker actually filled. It is the ONE thing a return needs
                # to become money, it exists nowhere else — the strategy's own ``weight`` is a
                # fraction of a notional nobody wrote down — and the order row is where the loop
                # can still see it when it records the round trip.
                "filled_qty": fill.quantity,
                "status": fill.status,
                # The broker's words, on every outcome: for a refusal this is the only
                # explanation of it, and the orders log is where someone looks for one.
                "detail": fill.detail or "",
                **identity,
            }
            if real is not None:
                self._reprice_entry(real)
                exits = self._reprice_exits()
                if exits is not None:
                    report["exits"] = exits
            return report
        if intent.action == CLOSE:
            self.state.unfilled_entry = None
            self.engine.settle(self.ledger, self.state, bar, intent, exit_price=real)
            return {
                "intent": intent.action,
                "reason": intent.reason,
                "price": real if real is not None else intent.expected_price,
                "expected": intent.expected_price,
                # The size that was SOLD or BOUGHT back — the shares the round trip was actually
                # made of, and therefore the multiplier the loop needs to write down a profit.
                "filled_qty": fill.quantity,
                "status": fill.status,
                "detail": fill.detail or "",
                **identity,
            }
        return {"intent": intent.action, "reason": intent.reason, **identity}

    def _submit(self, intent: Intent, client_order_id: str = "") -> Fill:
        if self.broker is None:
            # No broker wired (a dry run that only wants decisions): the expectation is
            # the fill, which is exactly what the simulated broker would say.
            return Fill(price=intent.expected_price, detail="no broker wired")
        if self.dry_run and intent.action == CLOSE and intent.reason == "signal":
            logger.info("[dry run] would close %s", self.name)
        return self.broker.submit(intent, client_order_id=client_order_id or None)

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
