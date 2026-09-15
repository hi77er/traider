"""The seam between a decision and a fill.

A strategy decides; something else fills. That "something else" is the ONLY thing that
differs between a backtest and a live run, which is why it is an interface with three
methods rather than a branch inside the strategy.

Two implementations:

* :class:`SimulatedBroker` — accepts the price the engine expected (the next bar's open,
  or the level a stop was touched at) and charges the configured costs. Used by the
  backtest and by a live DRY RUN, and it is deliberately conservative: it never fills
  better than the model says.
* an Alpaca-backed broker (``src/execution``) — submits a real order and reports what
  it got. This is the one place a fill price can differ from the expectation, and a live
  run records the difference rather than pretending it does not exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from src.strategy.engine import CLOSE, OPEN, Intent

logger = logging.getLogger(__name__)

__all__ = ["FILLED", "NO_FILL", "REJECTED", "Broker", "BrokerPosition", "Fill", "SimulatedBroker"]

FILLED, NO_FILL, REJECTED = "filled", "no_fill", "rejected"


@dataclass
class Fill:
    """What actually happened to an intent."""

    status: str = FILLED
    price: Optional[float] = None
    quantity: float = 0.0
    detail: str = ""

    @property
    def filled(self) -> bool:
        return self.status == FILLED and self.price is not None


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
    """Three methods: what is held, place an order, get out.

    Deliberately small. Every extra method here is another thing a simulated and a real
    broker can disagree about without any test noticing.
    """

    def position(self) -> BrokerPosition:
        """The position the broker holds right now."""

    def submit(self, intent: Intent) -> Fill:
        """Place the order ``intent`` describes and report the fill."""

    def close(self, reason: str = "") -> Fill:
        """Flatten whatever is held (used when trading is switched off)."""


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

    def submit(self, intent: Intent) -> Fill:
        if intent.skipped or intent.expected_price is None:
            return Fill(status=NO_FILL, detail="nothing to fill")
        price = float(intent.expected_price)
        # Track what is held, like a real broker would: a live driver reconciles against
        # this, and a simulator that always reported "flat" would make every run refuse
        # to trade — which is how this was noticed.
        if intent.action == OPEN:
            self.quantity = -1.0 if intent.short else 1.0
            self.entry_price = price
        elif intent.action == CLOSE:
            self.quantity = 0.0
            self.entry_price = None
        return Fill(status=FILLED, price=price, quantity=abs(self.quantity))

    def close(self, reason: str = "") -> Fill:
        self.quantity = 0.0
        self.entry_price = None
        return Fill(status=FILLED, detail=reason)
