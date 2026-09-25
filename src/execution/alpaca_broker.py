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

__all__ = [
    "AlpacaBroker",
    "MIN_NOTIONAL",
    "authorised_notional",
    "broker_position",
    "closing_fill_from_history",
    "exit_leg_level",
    "minimum_exposure_percent",
    "shares_for",
]

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


def exit_leg_level(leg: Dict[str, Any]) -> Optional[tuple]:
    """``(kind, level)`` for one resting exit leg — ``"stop"`` or ``"take"``, else ``None``.

    The kind comes from the ORDER TYPE because that is what decides it: a stop_limit carries
    both a ``stop_price`` and a ``limit_price``, so reading a price field would classify it
    by accident. The price fields are the fallback, for a payload whose type we do not know.

    Public because the dashboard has to ask the same question the driver does — "is there a
    leg resting at the level this position was opened with?" — and two classifiers would be
    free to disagree about the same order.
    """
    if not isinstance(leg, dict):
        return None
    kind = str(leg.get("type") or "").strip().lower()
    stop_price = _to_float(leg.get("stop_price"))
    limit_price = _to_float(leg.get("limit_price"))
    if kind in _STOP_TYPES:
        return ("stop", stop_price) if stop_price else None
    if kind in ("limit", "limit_on_close"):
        return ("take", limit_price) if limit_price else None
    if stop_price:
        return ("stop", stop_price)
    if limit_price:
        return ("take", limit_price)
    return None


def shares_for(weight: float, price: float, equity: float) -> int:
    """Whole shares for a fraction of equity. Zero when one share is unaffordable.

    Whole shares rather than fractional because a bracket requires them, because
    ``risk.position_sizing.size_position`` produces them, and because a fractional
    remainder is a position the strategy does not know it has when it reconciles.

    Zero is not a refusal on its own — it is the caller's cue to send the SAME weight as a
    notional order instead (``authorised_notional``), which is what a fraction of an account
    smaller than one share has always meant.
    """
    price = float(price or 0.0)
    equity = float(equity or 0.0)
    if price <= 0 or equity <= 0:
        return 0
    weight = min(max(float(weight or 0.0), 0.0), 1.0)
    return int(math.floor(equity * weight / price))


#: Alpaca's own floor for a fractional order. Below it there is nothing to send, and the
#: refusal quotes the exposure that would fix it rather than leaving the arithmetic to the
#: reader. Checked here as a courtesy; if the broker's minimum is higher, its error says so.
MIN_NOTIONAL = 1.0


def authorised_notional(weight: float, equity: float) -> float:
    """The dollars a weight authorises, to the cent.

    ``weight`` is the fraction of equity the risk layer sized (``StrategyConfig.deploy_weight``),
    so this is the size the strategy ASKED for — and the same unit a notional order is placed
    in, which is why the two are one decision rather than a conversion. Capped at the whole
    account for the reason ``shares_for`` caps the weight: a weight above 1 is a caller's
    arithmetic error, not an instruction to overdraw.
    """
    weight = min(max(float(weight or 0.0), 0.0), 1.0)
    return round(max(0.0, float(equity or 0.0)) * weight, 2)


def minimum_exposure_percent(price: float, equity: float) -> float:
    """The smallest ``MAX_EXPOSURE_PERCENT`` that affords ONE whole share.

    Quoted in the refusal so the setting that would fix it is on screen: at $100,000 of equity
    and NVDA at $226.89 this is 0.23%. A cap under it can only be traded as a fractional order.
    """
    equity = float(equity or 0.0)
    if equity <= 0:
        return 0.0
    return float(price or 0.0) / equity * 100.0


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

    def submit(self, intent: Intent, client_order_id: Optional[str] = None) -> Fill:
        """Place the order an intent describes. Never raises; reports instead.

        ``client_order_id``, when given, is the driver's idempotency key for this order —
        the same string every time the same bar is decided. Alpaca deduplicates on it, so a
        tick that crashed between sending an order and recording it cannot place a second
        one: the retry is the SAME order, not another one.
        """
        if intent is None or intent.skipped or intent.action in (SKIP, NONE):
            return Fill(status=NO_FILL, detail="nothing to submit")
        try:
            if intent.action == OPEN:
                return self._open(intent, client_order_id=client_order_id)
            if intent.action == CLOSE:
                return self._close(intent)
        except OrderRefused as exc:
            # Nothing was sent: the switch, the sizes or the prices said no.
            logger.error("%s refused a %s: %s", self.label, intent.action, exc)
            return Fill(status=REJECTED, detail=str(exc), client_order_id=client_order_id)
        except AlpacaError as exc:
            logger.error("%s broker error on %s: %s", self.label, intent.action, exc)
            return Fill(status=REJECTED, detail=str(exc), client_order_id=client_order_id)
        except Exception as exc:  # noqa: BLE001 - a tick must not die inside a broker
            logger.exception("%s unexpected failure on %s", self.label, intent.action)
            return Fill(
                status=REJECTED,
                detail=f"unexpected broker failure: {exc}",
                client_order_id=client_order_id,
            )
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

    def resting_levels(self) -> Dict[str, Optional[float]]:
        """The levels the broker's own exit legs sit at. Never raises.

        Read when a position is adopted, and the reason it is the broker's answer rather
        than the configuration's: the orders are what actually protect the position, and a
        stop placed before a settings edit must keep the level it was sized for. A leg whose
        level cannot be read is left ``None``, which the panel reports as uncovered rather
        than as protected.
        """
        levels: Dict[str, Optional[float]] = {"stop": None, "take": None}
        try:
            legs = self.executor.resting_exits(self.symbol)
        except (AlpacaError, OrderRefused) as exc:
            logger.warning("%s could not read the resting exits: %s", self.label, exc)
            return levels
        for leg in legs or ():
            pair = exit_leg_level(leg)
            if not pair:
                continue
            kind, level = pair
            if levels.get(kind) is None:
                levels[kind] = float(level)
        return levels

    # -- entries and exits -------------------------------------------------
    def _open(self, intent: Intent, *, client_order_id: Optional[str] = None) -> Fill:
        # Every refusal below carries ``client_order_id``. Nothing was sent, so our own name
        # for the order is the ONLY identifier it will ever have — and a log line saying
        # "the entry was skipped" without one cannot be tied to the bar that asked for it.
        price = _to_float(intent.expected_price)
        if not price:
            return Fill(
                status=REJECTED,
                detail="no price on the intent to size against",
                client_order_id=client_order_id,
            )

        equity = self.equity()
        if equity <= 0:
            return Fill(
                status=REJECTED,
                detail="account equity is unknown — cannot size an entry",
                client_order_id=client_order_id,
            )

        # The bracket carries the protection to the broker, where it survives this
        # process. Without both levels there is nothing to bracket, and sending one leg
        # alone would leave the other exit non-existent.
        stop = _to_float(intent.stop)
        take = _to_float(intent.take)
        bracket = bool(stop and take)

        quantity = shares_for(intent.weight, price, equity)
        notional: Optional[float] = None
        if quantity < 1:
            # THE CAP IS THE ORDER, and the cap is a FRACTION of equity, not a share count. When
            # one whole share costs more than the strategy is allowed to deploy, the authorised
            # amount still goes in — as a NOTIONAL order the broker fills with fractional shares
            # — instead of the entry being refused. This is the size the backtest has always
            # traded (it carries a weight, never a share count), so refusing here made live and
            # simulated disagree at exactly the equities and prices where the cap is smallest.
            notional = authorised_notional(intent.weight, equity)
            if notional < MIN_NOTIONAL:
                # Below the broker's own floor there is no order to send, and the useful answer
                # is the setting that would fix it.
                return Fill(
                    status=REJECTED,
                    detail=(
                        f"equity {equity:.2f} at weight {float(intent.weight):.3f} authorises "
                        f"${notional:.2f} of {self.symbol}, under Alpaca's ${MIN_NOTIONAL:.2f} "
                        f"minimum for a fractional order — raise Max exposure to at least "
                        f"{minimum_exposure_percent(price, equity):.2f}% to afford one share "
                        f"${price:.2f}"
                    ),
                    client_order_id=client_order_id,
                )
            if bracket:
                # A fractional order cannot carry a bracket, and an unprotected entry is worse
                # than none — so this refusal names both ways out rather than sending the entry
                # without its stop.
                return Fill(
                    status=REJECTED,
                    detail=(
                        f"equity {equity:.2f} at weight {float(intent.weight):.3f} affords "
                        f"${notional:.2f} of {self.symbol} at {price:.2f}, less than one share "
                        f"— and a fractional order cannot carry a stop/take bracket. Raise Max "
                        f"exposure to at least {minimum_exposure_percent(price, equity):.2f}% "
                        f"to afford a whole share, or configure no stop and no take-profit to "
                        f"trade the fraction"
                    ),
                    client_order_id=client_order_id,
                )

        if notional is None:
            result = self.executor.place_order(
                self.symbol,
                "SELL" if intent.short else "BUY",
                quantity,
                stop_loss_price=stop if bracket else None,
                take_profit_price=take if bracket else None,
                reference_price=price,
                client_order_id=client_order_id,
            )
        else:
            result = self.executor.place_order(
                self.symbol,
                "SELL" if intent.short else "BUY",
                notional=notional,
                reference_price=price,
                client_order_id=client_order_id,
            )
        return self._record(
            result,
            side="SHORT" if intent.short else "LONG",
            # The size the order asked for. The FILLED quantity comes back from the broker and is
            # what the record books; this is the fallback for an order still working.
            quantity=quantity if notional is None else 0.0,
            notional=notional,
        )

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

    def _record(self, result: dict, *, side: str, quantity: float,
                notional: Optional[float] = None) -> Fill:
        """Turn an order summary into a :class:`Fill`, honestly.

        An order that is live at the broker but not filled yet is NOT a fill: the driver
        would otherwise book a price it never got. It comes back as ``NO_FILL`` with the
        real status, and the next reconciliation sees the position once it exists.

        ``quantity`` is the size that was ASKED for and is only a fallback: a filled order's
        quantity is the broker's own ``filled_qty``, which for a notional order is a fraction.
        """
        status = str(result.get("status") or "")
        price = _to_float(result.get("filled_avg_price"))
        filled_qty = _to_float(result.get("filled_qty")) or 0.0
        detail = f"{self.label} {side} {self.symbol} {status}"
        if notional is not None:
            # The order was sent as dollars, and the fill will be a fraction of a share: the
            # summary says which so the two are not read as a rounding difference.
            detail += f" (${float(notional):.2f} notional)"
        # The broker's ids travel with the fill whatever its status: an order that is
        # still working has one too, and it is what the log is joined on.
        ids = {
            "order_id": str(result.get("order_id") or "") or None,
            "client_order_id": str(result.get("client_order_id") or "") or None,
        }

        if status in DEAD_STATUSES:
            return Fill(status=REJECTED, detail=detail, **ids)
        if price and (filled_qty > 0 or side == "EXIT"):
            fill = Fill(
                status=FILLED, price=price, quantity=filled_qty or quantity, detail=detail, **ids
            )
            self.fills.append({"side": side, "fill": fill})
            return fill
        if result.get("timed_out"):
            detail += f" — still working after the timeout (order {result.get('order_id')})"
        logger.warning("%s not filled yet: %s", self.label, detail)
        return Fill(status=NO_FILL, quantity=filled_qty, detail=detail, **ids)
