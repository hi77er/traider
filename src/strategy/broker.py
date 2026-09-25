"""The seam between a decision and a fill.

A strategy decides; something else fills. That "something else" is the ONLY thing that
differs between a backtest and a live run, which is why it is an interface rather than a
branch inside the strategy.

Two implementations:

* :class:`SimulatedBroker` — accepts the price the engine expected (the next bar's open,
  or the level a stop was touched at) and charges the configured costs. Used by the
  backtest and by a live DRY RUN, and it is deliberately conservative: it never fills
  better than the model says.
* an Alpaca-backed broker (``src/execution``) — submits a real order and reports what
  it got. This is the one place a fill price can differ from the expectation, and a live
  run records the difference rather than pretending it does not exist.

**Five methods, three of them load-bearing.** ``position``/``submit``/``close`` are what
both implementations do. The other two describe things that only exist at a real broker,
and both are deliberately NO-OPS in the simulated one:

* :meth:`Broker.closing_fill` — an exit the broker performed on its own, which a live run
  must adopt. Nothing can exit a simulated position except the engine, in the same step
  that decides it, so there is nothing to adopt and ``SimulatedBroker`` returns ``None``.
* :meth:`Broker.reprice_exits` — moving the resting exits after a fill. A simulated run's
  exits are levels the engine tests against each bar's range, so there is no order to
  move and ``SimulatedBroker`` does nothing.

Keeping them on the interface rather than testing for ``AlpacaBroker`` is what stops the
driver from growing a branch that only one implementation ever takes: the parity test
drives the same code path as live, and these are the two places where "the same code"
legitimately means "and nothing happens here".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, runtime_checkable

from src.strategy.engine import CLOSE, OPEN, Intent

logger = logging.getLogger(__name__)

__all__ = [
    "FILLED",
    "NO_FILL",
    "REJECTED",
    "Broker",
    "BrokerPosition",
    "ClosingFill",
    "Fill",
    "SimulatedBroker",
]

FILLED, NO_FILL, REJECTED = "filled", "no_fill", "rejected"


@dataclass
class Fill:
    """What actually happened to an intent.

    ``order_id`` and ``client_order_id`` are the broker's name for the order and ours
    (see ``LiveDriver.order_id_for``). They are carried here because they are the only
    way an order can be attributed later: without them a tick can say a bar was decided
    and a position exists, but not which order did it — and nothing in the live log could
    be joined to the broker's own list. ``SimulatedBroker`` has no broker-side id to
    report and echoes the client id it was given, so a dry run's log is still joinable.
    """

    status: str = FILLED
    price: Optional[float] = None
    quantity: float = 0.0
    detail: str = ""
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None

    @property
    def filled(self) -> bool:
        return self.status == FILLED and self.price is not None


@dataclass
class ClosingFill:
    """An exit the BROKER made, which the driver did not ask for and has not seen.

    A resting bracket order closing between two ticks is the ordinary way a live position
    ends — the strategy is asleep at the time and only finds out on its next tick, by which
    point the broker is flat and the local state is not. This is the proof that the exit
    happened and at what price, so the driver can book it instead of refusing for ever.

    ``reason`` uses the engine's own vocabulary (``stop`` / ``take`` / ``forced``) so the
    trade log reads the same whether the exit was ours or the broker's.
    """

    price: float
    reason: str = ""
    order_id: Optional[str] = None
    quantity: float = 0.0
    filled_at: Optional[str] = None
    detail: str = ""


@dataclass
class BrokerPosition:
    """What the broker says is held — the truth a live run reconciles against."""

    quantity: float = 0.0
    entry_price: Optional[float] = None
    short: bool = False

    @property
    def is_flat(self) -> bool:
        return abs(float(self.quantity)) < 1e-12


@runtime_checkable
class Broker(Protocol):
    """What is held, place an order, get out — plus the two live-only questions.

    Deliberately small. Every extra method here is another thing a simulated and a real
    broker can disagree about without any test noticing, which is why the two additions
    below are read-only descriptions of a REAL broker's behaviour rather than new
    decisions the strategy delegates.
    """

    def position(self) -> BrokerPosition:
        """The position the broker holds right now."""

    def submit(self, intent: Intent, client_order_id: Optional[str] = None) -> Fill:
        """Place the order ``intent`` describes and report the fill.

        ``client_order_id`` is the driver's name for this order, derived from what the
        order IS — the strategy, the account, the bar and its position among that bar's
        intents (see ``LiveDriver.order_id_for``). A real broker uses it as an idempotency
        key, which is what makes re-deciding a bar after a crash safe rather than a way to
        double a position; a simulated one has nothing to deduplicate against and ignores
        it. It is on the interface rather than read off the intent because naming an order
        is a broker's concern, and because the engine's intents are the same in a backtest,
        where no such id exists.
        """

    def close(self, reason: str = "") -> Fill:
        """Flatten whatever is held (used when trading is switched off)."""

    def closing_fill(self, short: bool) -> Optional[ClosingFill]:
        """An exit the broker made on its own since the last tick, or ``None``.

        ``short`` says which way the local position points, so the broker can tell a
        closing order from an opening one without being told a broker-side side string.
        Asked only when the local state holds a position and the broker is flat — the
        case where refusing would wedge the bot for ever.
        """

    def reprice_exits(self, stop: Optional[float], take: Optional[float]) -> Optional[dict]:
        """Move the RESTING exits to these levels, or return why it could not.

        Called after a fill whose real price differed from the price the exits were
        derived from. A broker with no resting orders does nothing.
        """

    def resting_levels(self) -> Dict[str, Optional[float]]:
        """Where the broker's resting exits sit now, as ``{"stop": …, "take": …}``.

        Asked when a position is ADOPTED — trading armed while something was already open.
        The levels that protect a position are the broker's orders, not the current
        configuration: a stop placed before a settings edit must not silently move because
        the edit happened, so the levels come from the legs themselves. ``None`` for a level
        with no leg behind it, which is the honest answer and keeps the panel's "is it
        protected" comparison a comparison rather than a guess.
        """


class SimulatedBroker:
    """Fills at the price the engine expected, charging the configured costs.

    The cost model lives in the engine's ``StrategyConfig``, so the expected price is
    already cost-adjusted; this broker therefore only has to accept it. The point of
    the class is that a live DRY RUN and the parity test drive the SAME code path a real
    broker would — if the strategy started depending on broker behaviour, these would
    be the tests that notice.
    """

    def __init__(self, *, quantity: float = 0.0):
        self.quantity = float(quantity)
        self.entry_price: Optional[float] = None

    def position(self) -> BrokerPosition:
        return BrokerPosition(
            quantity=self.quantity,
            entry_price=self.entry_price,
            short=self.quantity < 0,
        )

    def submit(self, intent: Intent, client_order_id: Optional[str] = None) -> Fill:
        # There is no broker-side order to deduplicate against, so the id is echoed back
        # rather than used: a simulated run's log then carries the same join key a real
        # one does, and the code that writes it needs no special case.
        if intent.skipped or intent.expected_price is None:
            return Fill(status=NO_FILL, detail="nothing to fill", client_order_id=client_order_id)
        price = float(intent.expected_price)
        # Track what is held, like a real broker would: a live driver reconciles against
        # this, and a simulator that always reported "flat" would make every run refuse
        # to trade — which is how this was noticed.
        filled = abs(self.quantity)
        if intent.action == OPEN:
            self.quantity = -1.0 if intent.short else 1.0
            self.entry_price = price
            filled = abs(self.quantity)
        elif intent.action == CLOSE:
            # What a close FILLS is the position it flattened, so the size is taken before the
            # book is zeroed. Reporting zero made a simulated round trip look like a trade of
            # no size, and a trade of no size has no profit — the one number the live trade log
            # needs to turn a return into money.
            self.quantity = 0.0
            self.entry_price = None
        return Fill(
            status=FILLED,
            price=price,
            quantity=filled,
            client_order_id=client_order_id,
        )

    def close(self, reason: str = "") -> Fill:
        self.quantity = 0.0
        self.entry_price = None
        return Fill(status=FILLED, detail=reason)

    def closing_fill(self, short: bool) -> Optional[ClosingFill]:
        """Always ``None``: nothing exits a simulated position but the engine.

        There is no resting order to fire between ticks, so there is never an out-of-band
        exit to adopt — and returning ``None`` is what makes the driver's refusal path
        (the one the parity test exercises) reachable in a simulated run.
        """
        return None

    def reprice_exits(self, stop: Optional[float], take: Optional[float]) -> Optional[dict]:
        """Nothing to move: a simulated run's exits are levels, not resting orders.

        The engine tests those levels against each bar's range as it replays, so a
        simulated stop already sits where the fill put it and there is no order to amend.
        """
        return None

    def resting_levels(self) -> Dict[str, Optional[float]]:
        """No legs, so no levels: a simulated position is protected by the engine's own
        levels, which the driver derives from the configuration when it adopts.
        """
        return {"stop": None, "take": None}
