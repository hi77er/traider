"""``AlpacaBroker`` — the strategy's broker seam, implemented against Alpaca.

``src/strategy/broker.py`` defines three methods (what is held, place an order, get
out) and the live driver is written against exactly those. This file is the other
side of that seam, and it is deliberately thin: it *translates*, it does not decide.
The intent arrives already sized, already stopped and already cleared by the risk
layer, because on the strategy path the risk layer is the engine itself — the same
one the backtest runs. Adding a second, independently-stateful validator here would
be a second opinion that drifts from the first.

Four things worth knowing about this translation:

**A position is asked for, never assumed.** The driver reconciles local state against
the broker before every decision, so ``position()`` lets a broker failure propagate.
An unreachable broker that answers "flat" is worse than a crash: the driver would
conclude it is safe to open a position it may already be in. ``submit()`` is the
opposite — it returns a REJECTED :class:`Fill` for every failure, because the driver
records what happened and must not be interrupted mid-tick.

**An entry carries its exits.** The intent's stop and take become a broker-side
bracket, so the protection lives at Alpaca even if this process dies. A strategy
configured with no stop gets no bracket — a naked entry, which is what was asked for.

**An exit may already have happened.** Live, the resting bracket can close the
position before the strategy's next bar notices. So an exit first asks the broker
what is held: already flat means the bracket won, and the price reported is the one
the broker actually filled at, read back from its order history. Never re-derived
locally.

**A close cancels before it closes.** Flattening cancels the symbol's resting orders
first (see ``AlpacaExecutor.flatten``), so a surviving stop cannot open the opposite
position after the strategy believes it is out.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Optional

from src.execution.alpaca_client import AlpacaError
from src.execution.alpaca_executor import AlpacaExecutor, OrderRefused
from src.strategy.broker import FILLED, NO_FILL, REJECTED, BrokerPosition, ClosingFill, Fill
from src.strategy.engine import CLOSE, FORCED, NONE, OPEN, SKIP, STOP, TAKE, Intent

logger = logging.getLogger(__name__)

__all__ = ["AlpacaBroker", "broker_position", "closing_fill_from_history", "shares_for"]

# Alpaca says "this order is over and did not fill".
DEAD_STATUSES = frozenset({"rejected", "canceled", "cancelled", "expired", "stopped"})

# The order types that can only be an EXIT: an entry is a market order and, in this
# project, always a bracket parent. Reading the reason off the type is what lets the
# adopted exit land in the trade log as a stop or a take rather than as "something".
_STOP_TYPES = frozenset({"stop", "stop_limit", "trailing_stop"})


def broker_position(payload: Optional[dict]) -> BrokerPosition:
    """Alpaca's position payload as the protocol's :class:`BrokerPosition`.

    ``quantity`` is signed — negative for a short — because that is how
    ``SimulatedBroker`` reports it and the driver compares the two.
    """
    if not payload:
        return BrokerPosition()
    qty = _to_float(payload.get("qty")) or 0.0
    side = str(payload.get("side") or "").strip().lower()
    short = side == "short" if side else qty < 0
    magnitude = abs(qty)
    return BrokerPosition(
        quantity=-magnitude if short else magnitude,
        entry_price=_to_float(payload.get("avg_entry_price")),
        short=short,
    )


def shares_for(weight: float, price: float, equity: float) -> int:
    """Whole shares for a fraction of equity. Zero when one share is unaffordable.

    Whole shares rather than fractional because a bracket requires them, because
    ``risk.position_sizing.size_position`` produces them, and because a fractional
    remainder is a position the strategy does not know it has when it reconciles.
    """
    price = float(price or 0.0)
    equity = float(equity or 0.0)
    if price <= 0 or equity <= 0:
        return 0
    weight = min(max(float(weight or 0.0), 0.0), 1.0)
    return int(math.floor(equity * weight / price))


def closing_fill_from_history(orders, *, symbol: str, short: bool) -> Optional[ClosingFill]:
    """Find the order that closed our position, if the history proves one did.

    Two shapes are searched, because Alpaca uses both: a plain exit order listed on its
    own, and the ``legs`` of a bracket parent (the usual case — the parent is the entry,
    and its stop and take-profit hang off it). Only FILLED orders with a price count; a
    cancelled leg is not an exit.

    ``short`` is how the closing side is derived — a long is closed by a SELL and a short
    by a BUY — and it comes from the local position rather than the broker, because by the
    time this is asked the broker is flat and no longer remembers which way round it was.

    Returns ``None`` when nothing in the history proves a close. That is the case the
    driver must keep refusing on: an exit with no price is not something to book.
    """
    closing_side = "buy" if short else "sell"
    best = None
    best_at = ""

    candidates: list = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        candidates.append(order)
        candidates.extend(leg for leg in (order.get("legs") or []) if isinstance(leg, dict))

    for order in candidates:
        if str(order.get("status") or "").lower() != "filled":
            continue
        if str(order.get("side") or "").strip().lower() != closing_side:
            continue
        price = _to_float(order.get("filled_avg_price"))
        if not price:
            continue
        filled_at = str(order.get("filled_at") or "")
        # Newest wins. The list is newest-first, but legs arrive nested and unordered
        # relative to each other, so the timestamp is what actually decides.
        if best is None or filled_at > best_at:
            best, best_at = order, filled_at

    if best is None:
        return None

    kind = str(best.get("type") or "").lower()
    reason = STOP if kind in _STOP_TYPES else (TAKE if kind == "limit" else FORCED)
    return ClosingFill(
        price=float(_to_float(best.get("filled_avg_price"))),
        reason=reason,
        order_id=str(best.get("id") or "") or None,
        quantity=float(_to_float(best.get("filled_qty")) or 0.0),
        filled_at=best_at or None,
        detail=f"closed by a resting {kind or 'order'}",
    )


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AlpacaBroker:
    """Implements :class:`src.strategy.broker.Broker` on top of :class:`AlpacaExecutor`."""

    def __init__(
        self,
        settings,
        *,
        executor: Optional[AlpacaExecutor] = None,
        symbol: Optional[str] = None,
        equity_provider: Optional[Callable[[], float]] = None,
        guard=None,
        target=None,
        client=None,
    ) -> None:
        self.settings = settings
        self.executor = executor or AlpacaExecutor(settings, target=target, client=client, guard=guard)
        self.symbol = str(symbol or getattr(settings, "instrument", "") or "").upper()
        self._equity_provider = equity_provider
        self.fills: list = []

    # -- facts -------------------------------------------------------------
    @property
    def label(self) -> str:
        return self.executor.label

    @property
    def env(self) -> str:
        return self.executor.target.env

    def equity(self) -> float:
        """Account equity — the number a fractional weight is a fraction OF."""
        if self._equity_provider is not None:
            return float(self._equity_provider())
        return self.executor.equity()

    # -- the protocol ------------------------------------------------------
    def position(self) -> BrokerPosition:
        """What the broker holds. Raises if it cannot be asked — see the module docstring."""
        return broker_position(self.executor.position(self.symbol))

    def submit(self, intent: Intent) -> Fill:
        """Place the order an intent describes. Never raises; reports instead."""
        if intent is None or intent.skipped or intent.action in (SKIP, NONE):
            return Fill(status=NO_FILL, detail="nothing to submit")
        try:
            if intent.action == OPEN:
                return self._open(intent)
            if intent.action == CLOSE:
                return self._close(intent)
        except OrderRefused as exc:
            # Nothing was sent: the switch, the sizes or the prices said no.
            logger.error("%s refused a %s: %s", self.label, intent.action, exc)
            return Fill(status=REJECTED, detail=str(exc))
        except AlpacaError as exc:
            logger.error("%s broker error on %s: %s", self.label, intent.action, exc)
            return Fill(status=REJECTED, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - a tick must not die inside a broker
            logger.exception("%s unexpected failure on %s", self.label, intent.action)
            return Fill(status=REJECTED, detail=f"unexpected broker failure: {exc}")
        return Fill(status=NO_FILL, detail=f"unsupported action {intent.action!r}")

    def close(self, reason: str = "") -> Fill:
        """Flatten what is held — used when trading is switched off."""
        try:
            return self._flatten(reason or "manual close")
        except (OrderRefused, AlpacaError) as exc:
            logger.error("%s could not flatten %s: %s", self.label, self.symbol, exc)
            return Fill(status=REJECTED, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s unexpected failure flattening %s", self.label, self.symbol)
            return Fill(status=REJECTED, detail=f"unexpected broker failure: {exc}")

    def closing_fill(self, short: bool) -> Optional[ClosingFill]:
        """What closed our position, if a resting exit did it while we were not looking.

        Asked only in the case that used to deadlock the driver: local state holds a
        position and the broker is flat. A bracket's stop or take-profit firing between two
        ticks is the ordinary way a live position ends, and re-deriving the price locally
        would be a guess about the broker's behaviour rather than a record of it — so the
        answer comes from the order history, and the REASON comes from the leg's own type.

        Returns ``None`` when it cannot be proved, which is the honest answer: the driver
        refuses in that case rather than inventing an exit price.
        """
        try:
            orders = self.executor.closed_orders(self.symbol, limit=10)
        except (AlpacaError, OrderRefused) as exc:
            logger.warning("%s could not read order history: %s", self.label, exc)
            return None
        return closing_fill_from_history(orders, symbol=self.symbol, short=short)

    def reprice_exits(self, stop: Optional[float], take: Optional[float]) -> Optional[dict]:
        """Move the resting exits to the levels derived from the REAL fill price.

        Only the broker can do this: the levels are the driver's (they come from
        ``StrategyEngine.levels``, measured from what was actually paid), but the orders
        that have to move are the broker's. Never raises — a leg that keeps its old level
        is still protection, and failing an entry because an amendment did not land would
        be trading nothing at all instead of trading something slightly wider.
        """
        if stop is None and take is None:
            return None
        try:
            report = self.executor.amend_exits(self.symbol, stop=stop, take=take)
        except Exception as exc:  # noqa: BLE001 - an amendment must never break a tick
            logger.exception("%s unexpected failure amending exits", self.label)
            return {"amended": [], "failed": [{"reason": str(exc)}], "left": []}
        if report.get("failed"):
            logger.error(
                "%s left %d exit leg(s) at their old level: %s",
                self.label, len(report["failed"]), report["failed"],
            )
        return report

    # -- entries and exits -------------------------------------------------
    def _open(self, intent: Intent) -> Fill:
        price = _to_float(intent.expected_price)
        if not price:
            return Fill(status=REJECTED, detail="no price on the intent to size against")

        equity = self.equity()
        if equity <= 0:
            return Fill(status=REJECTED, detail="account equity is unknown — cannot size an entry")

        quantity = shares_for(intent.weight, price, equity)
        if quantity < 1:
            # A real live condition, not a bug: at this equity one share is unaffordable.
            # Reported rather than rounded up, because rounding up would risk more than
            # the strategy sized for.
            return Fill(
                status=REJECTED,
                detail=(
                    f"equity {equity:.2f} at weight {float(intent.weight):.3f} cannot afford one "
                    f"share of {self.symbol} at {price:.2f} — entry skipped"
                ),
            )

        # The bracket carries the protection to the broker, where it survives this
        # process. Without both levels there is nothing to bracket, and sending one leg
        # alone would leave the other exit non-existent.
        stop = _to_float(intent.stop)
        take = _to_float(intent.take)
        bracket = bool(stop and take)

        result = self.executor.place_order(
            self.symbol,
            "SELL" if intent.short else "BUY",
            quantity,
            stop_loss_price=stop if bracket else None,
            take_profit_price=take if bracket else None,
            reference_price=price,
        )
        return self._record(result, side="SHORT" if intent.short else "LONG", quantity=quantity)

    def _close(self, intent: Intent) -> Fill:
        return self._flatten(intent.reason or "exit")

    def _flatten(self, reason: str) -> Fill:
        result = self.executor.flatten(self.symbol, reason)
        if result.get("was_flat"):
            # The resting bracket got there first. The position is gone, so the exit has
            # happened — reporting NO_FILL would leave the driver believing it is still
            # in a position the broker has already closed.
            price = _to_float(result.get("filled_avg_price"))
            return Fill(
                status=FILLED,
                price=price,
                quantity=0.0,
                detail=f"already closed by the resting exit at {reason}" + ("" if price else " (price unknown)"),
            )
        return self._record(result, side="EXIT", quantity=0.0)

    def _record(self, result: dict, *, side: str, quantity: float) -> Fill:
        """Turn an order summary into a :class:`Fill`, honestly.

        An order that is live at the broker but not filled yet is NOT a fill: the driver
        would otherwise book a price it never got. It comes back as ``NO_FILL`` with the
        real status, and the next reconciliation sees the position once it exists.
        """
        status = str(result.get("status") or "")
        price = _to_float(result.get("filled_avg_price"))
        filled_qty = _to_float(result.get("filled_qty")) or 0.0
        detail = f"{self.label} {side} {self.symbol} {status}"

        if status in DEAD_STATUSES:
            return Fill(status=REJECTED, detail=detail)
        if price and (filled_qty > 0 or side == "EXIT"):
            fill = Fill(status=FILLED, price=price, quantity=filled_qty or quantity, detail=detail)
            self.fills.append({"side": side, "fill": fill})
            return fill
        if result.get("timed_out"):
            detail += f" — still working after the timeout (order {result.get('order_id')})"
        logger.warning("%s not filled yet: %s", self.label, detail)
        return Fill(status=NO_FILL, quantity=filled_qty, detail=detail)
