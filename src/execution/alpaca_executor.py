"""Placing an order — the part of TRAIDER that can spend real money.

Above this file, a strategy decides. Here, the decision becomes an order at Alpaca.
The whole file exists to make that step *narrow*: the environment was already
resolved (`config.resolve_execution_target`), the credential was already proved
(`credentials.verify`), and this module's job is to build one correct payload, send
it once, and report what the broker said.

Five rules, each of which was a decision rather than a default:

**1. Refuse before sending, never after.** Four things can stop an order, checked in
this order: the configuration cannot be traded with (`ExecutionConfigError`), trading
is switched OFF, the caller's numbers are impossible, and — when a validator is
supplied — the risk layer vetoes it. All four raise :class:`OrderRefused` and nothing
is sent. An order that is never sent is the only kind that cannot be wrong.

**2. The exits go on the order, not beside it.** A stop and a take-profit are sent as
Alpaca's `bracket` order class in the SAME call as the entry. Hand-rolling them (entry
now, exits after) is the classic bug of this shape: the process dies between the two
calls and the position is naked, or a filled take-profit leaves a resting stop that
opens the opposite position. One call, or no order.

**3. Brackets need whole shares.** Alpaca takes fractional orders DAY-only with no
bracket, and the position sizer produces whole shares anyway (`risk.position_sizing`).
So a fractional quantity *with* exits is refused with an explanation rather than
quietly sent without its stop — a silent naked entry is worse than no entry.

**4. A retry reuses its `client_order_id`.** A submit that times out may have been
received. Alpaca deduplicates on `client_order_id`, so the id is generated once per
intent and every attempt carries it; `execute_with_retry` then retries transport
failures and 429/5xx and refuses to retry anything else.

**5. The environment is printed on every line.** Paper and live accounts look
identical in Alpaca's portal. An unlabelled order log is how a live order gets missed.

What this module deliberately does NOT do: decide anything. It does not size a
position, choose a stop, or judge a signal — those live in ``src/risk`` and
``src/strategy``, shared with the backtest. ``quantity``, ``stop_loss_price`` and
``take_profit_price`` arrive as arguments.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from src.execution.alpaca_client import AlpacaClient, AlpacaError
from src.execution.config import ExecutionConfigError, ExecutionTarget, resolve_execution_target
from src.execution.retry import execute_with_retry

logger = logging.getLogger(__name__)

__all__ = [
    "AlpacaExecutor",
    "OrderRefused",
    "BUY",
    "SELL",
    "TERMINAL_STATUSES",
    "build_order_payload",
    "quantize_quantity",
]

BUY, SELL = "BUY", "SELL"
SIDES = (BUY, SELL)

# Alpaca's own vocabulary for "this order is over". Anything else is still working.
TERMINAL_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped", "done_for_day"}
)

# A stop must sit at least a cent beyond the price it protects, or Alpaca rejects the
# request outright. Checked here so the failure names the real problem.
MIN_STOP_DISTANCE = 0.01

# Fractional quantities are supported by Alpaca but are DAY-only and cannot be
# bracketed; the sizer produces whole shares, so this is the threshold for "whole".
WHOLE_SHARE_EPSILON = 1e-9


class OrderRefused(RuntimeError):
    """The order was NOT sent, and why. Distinct from a broker refusal.

    The distinction is the point: ``OrderRefused`` means nothing left this process and
    the strategy is still flat (retrying it might make sense later), while
    :class:`AlpacaError` means the broker was asked and said no (retrying it usually
    cannot help).
    """


def quantize_quantity(quantity: float, *, fractional: bool = False) -> float:
    """Whole shares unless fractions were explicitly asked for.

    Whole shares are the default because that is what a bracket requires and what
    ``risk.position_sizing.size_position`` produces — and because a fractional share
    left over from a rounding difference is a position the strategy does not know it
    has when it reconciles.
    """
    value = float(quantity)
    if fractional:
        return round(value, 6)
    return float(int(value))


def _is_whole(quantity: float) -> bool:
    return abs(float(quantity) - round(float(quantity))) < WHOLE_SHARE_EPSILON


# The order types Alpaca will replace in place: exactly the RESTING exit legs. An entry
# is a market order, so this set is also how an exit is told from an entry without
# having to know which one the caller meant.
REPLACEABLE_TYPES = frozenset({"stop", "stop_limit", "trailing_stop", "limit"})


def _exit_legs(orders) -> list:
    """The resting, replaceable exit legs among ``orders`` — parents and children alike.

    A bracket comes back as a parent whose exits hang off ``legs``, and a leg can also be
    listed on its own; both are flattened here so callers need not know which shape Alpaca
    used today. Terminal legs are dropped, because replacing a finished order is not an
    amendment — it is a new order nobody decided to place.
    """
    found: list = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        for candidate in [order] + list(order.get("legs") or []):
            if not isinstance(candidate, dict):
                continue
            kind = str(candidate.get("type") or "").lower()
            if kind not in REPLACEABLE_TYPES or not candidate.get("id"):
                continue
            if str(candidate.get("status") or "").lower() in TERMINAL_STATUSES:
                continue
            found.append(candidate)
    return found


def _level_is_replaceable(level: float, reference: Optional[float]) -> bool:
    """Is ``level`` far enough from ``reference`` to be a level at all?

    Only the distance is checked, deliberately: whether a stop belongs above or below the
    fill depends on the direction, and this layer is not told the direction because it
    does not need to be — the levels arrive from ``StrategyEngine.levels``, which derives
    them from the fill price and the position's side. Re-deriving the side here would be a
    second opinion on a question already answered (see rule 1: refuse what is impossible,
    do not re-decide what is decided).
    """
    if level <= 0:
        return False
    if reference is None:
        return True
    return abs(level - float(reference)) >= MIN_STOP_DISTANCE


def build_order_payload(
    *,
    symbol: str,
    side: str,
    quantity: float,
    client_order_id: str,
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: Optional[str] = None,
    base_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the ``POST /v2/orders`` body, or raise :class:`OrderRefused`.

    Pure on purpose: the payload is the thing most worth asserting in a test, and
    keeping it a function means the executor can be tested without one.

    ``base_price`` is the price the exits are measured from (the limit price, or the
    reference price the caller was looking at). It is only used to validate the stop
    distance, which Alpaca requires to be at least a cent.
    """
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        raise OrderRefused("No instrument given — refusing to place an order")

    side = str(side or "").upper().strip()
    if side not in SIDES:
        raise OrderRefused(f"Unknown side {side!r}; expected {BUY} or {SELL}")

    quantity = float(quantity)
    if quantity <= 0:
        raise OrderRefused(
            f"Quantity must be positive (got {quantity:g}) — refusing to place an order"
        )
    if not _is_whole(quantity) and (stop_loss_price or take_profit_price):
        raise OrderRefused(
            f"{quantity:g} shares is fractional: Alpaca takes fractional orders DAY-only "
            "and cannot attach a bracket, so the entry would go in with NO stop. Size "
            "whole shares (the position sizer does) or place the entry yourself — "
            "refusing to send an unprotected entry."
        )

    order_type = str(order_type or "market").lower()
    if order_type == "limit" and not limit_price:
        raise OrderRefused("A limit order needs a limit price")

    bracket = bool(stop_loss_price) and bool(take_profit_price)
    if (stop_loss_price or take_profit_price) and not bracket:
        # One leg of a bracket is not a bracket; Alpaca would take it as a bare order
        # and the other exit would never exist.
        raise OrderRefused(
            "Both a stop-loss and a take-profit are needed for a bracket order; only one "
            "was given. Pass both, or neither and manage the exit yourself."
        )

    payload: Dict[str, Any] = {
        "symbol": symbol,
        "qty": f"{quantize_quantity(quantity, fractional=not _is_whole(quantity)):g}",
        "side": side.lower(),
        "type": order_type,
        "client_order_id": client_order_id,
    }
    if order_type == "limit":
        payload["limit_price"] = f"{float(limit_price):.2f}"

    if bracket:
        # Fractional is DAY-only; whole-share brackets may ride GTC, but DAY is the
        # conservative default: a resting exit that outlives the session can wake up
        # inside a gap.
        payload["time_in_force"] = str(time_in_force or "day").lower()
        payload["order_class"] = "bracket"
        take = float(take_profit_price)
        stop = float(stop_loss_price)

        # The price the exits are measured from. A limit order measures from its own
        # limit; a market order can only be checked when the caller says what price it
        # was looking at — without one we validate what we can and send, because
        # inventing a reference would reject valid orders.
        reference = 0.0
        if order_type == "limit" and limit_price:
            reference = float(limit_price)
        elif base_price:
            reference = float(base_price)

        if reference > 0 and abs(stop - reference) < MIN_STOP_DISTANCE:
            raise OrderRefused(
                f"Stop {stop:.4f} is within a cent of {reference:.4f}; Alpaca requires the "
                "stop to sit at least $0.01 beyond the entry — refusing to send it"
            )
        # A stop on the wrong side of the entry is a position that closes instantly at a
        # loss, or one that can never close. Alpaca validates it too; failing here names
        # which price is wrong.
        if reference > 0:
            if side == BUY and not (stop < reference < take):
                raise OrderRefused(
                    f"A long entry at {reference:.4f} needs the stop ({stop:.4f}) below it and "
                    f"the take ({take:.4f}) above it"
                )
            if side == SELL and not (take < reference < stop):
                raise OrderRefused(
                    f"A short entry at {reference:.4f} needs the stop ({stop:.4f}) above it and "
                    f"the take ({take:.4f}) below it"
                )
        payload["take_profit"] = {"limit_price": f"{take:.2f}"}
        payload["stop_loss"] = {"stop_price": f"{stop:.2f}"}
    else:
        payload["time_in_force"] = str(time_in_force or "day").lower()

    return payload


def _default_guard(settings):
    """The master switch, read from ``src/config/trading_state``.

    That module holds the state and imports nothing but ``state_files``, so this is
    an arrow pointing ``execution -> config``. It used to reach into
    ``src/web/services/trading_service``, which meant the execution loop had to import
    the whole FastAPI dashboard to ask "am I allowed to trade?".

    Read on EVERY call, never cached: a loop that cached the switch would keep trading
    after the operator pressed stop, which is the one thing the switch must never do.

    It is deliberately not optional — if the switch cannot be read, the answer is NO. A
    guard that quietly disappears when it cannot be evaluated is worse than no guard,
    because it looks like one.
    """

    def guard() -> Optional[str]:
        try:
            from src.config import trading_state
        except Exception as exc:  # noqa: BLE001 - fail closed, with the reason
            return f"the trading switch could not be read ({exc}) — refusing to assume it is on"
        if not trading_state.is_trading_on(settings):
            return "trading is OFF"
        return None

    return guard


class AlpacaExecutor:
    """Places orders in one environment: paper or live.

    Constructed per strategy, from its effective settings, so the environment is
    resolved exactly once — the same resolution the dashboard badge and the backtest
    provenance use.
    """

    def __init__(
        self,
        settings,
        *,
        target: Optional[ExecutionTarget] = None,
        client: Optional[AlpacaClient] = None,
        guard=None,
        validator=None,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        # Raises ExecutionConfigError when the configuration cannot be traded with, and
        # that is intentional: a bot that cannot reach its broker must stop, not start.
        self.target = target or resolve_execution_target(settings)
        self.client = client or AlpacaClient(
            self.target,
            timeout=float(getattr(settings, "execution_order_timeout_seconds", 60) or 60),
        )
        self.guard = guard if guard is not None else _default_guard(settings)
        # Optional, and off by default: on the strategy path the risk layer is the
        # ENGINE (the same sizing, stops and breaker the backtest runs, already
        # reflected in the intent). Wiring a second, separately-stateful validator here
        # would be a second opinion that drifts from the first.
        self.validator = validator
        self._sleep = sleep

    # -- facts about the account -------------------------------------------
    @property
    def label(self) -> str:
        return "LIVE" if self.target.live else "PAPER"

    def account(self) -> Dict[str, Any]:
        return self.client.account()

    def equity(self) -> float:
        """Account equity, for turning a weight into a share count."""
        try:
            return float(self.account().get("equity") or 0.0)
        except (AlpacaError, TypeError, ValueError):
            logger.warning("%s could not read account equity", self.label)
            return 0.0

    def position(self, instrument: str) -> Optional[Dict[str, Any]]:
        return self.client.position(instrument)

    def positions(self) -> list:
        """EVERY position in the account, not just this strategy's instrument.

        Asked before an action that could strand one — arming the bot, switching the
        environment, changing or deleting the active strategy — because the account is
        shared and a position the strategy does not know about is still a position nobody
        is managing (see ``src/execution/positions.py``).
        """
        return self.client.positions()

    def open_orders(self, instrument: Optional[str] = None) -> list:
        return self.client.open_orders(instrument)

    def resting_exits(self, instrument: Optional[str] = None, orders: Optional[list] = None) -> list:
        """The resting exit legs among the open orders for ``instrument``.

        Separate from ``open_orders`` because the question "is this position protected?"
        is not answered by "are there orders": an entry that has not filled yet is an open
        order, and a bracket parent is an open order, while neither is an exit. Only a stop
        or limit leg protects anything.

        ``orders`` is an already-fetched open-order list. A caller that wants BOTH lists —
        the dashboard does — passes the one it has: this is a filter over those rows, not a
        different question, so asking the broker again would be a second round trip for the
        same answer.
        """
        return _exit_legs(self.client.open_orders(instrument) if orders is None else orders)

    def closed_orders(self, instrument: Optional[str] = None, limit: int = 20) -> list:
        """Recently finished orders, newest first — where a resting exit's fill is read."""
        return self.client.closed_orders(instrument, limit=limit)

    def clock(self) -> Dict[str, Any]:
        return self.client.clock()

    # -- guards ------------------------------------------------------------
    def _check_guard(self) -> None:
        if self.guard is None:
            return
        reason = self.guard()
        if reason:
            raise OrderRefused(f"Refusing to place an order — {reason}")

    # -- orders ------------------------------------------------------------
    def place_order(
        self,
        instrument: str,
        side: str,
        quantity: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        *,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
        reference_price: Optional[float] = None,
        risk_state: Optional[Dict[str, Any]] = None,
        risk_signal: Optional[Dict[str, Any]] = None,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Send one order and report what happened. Raises on refusal.

        Returns a small, stable dict — ``order_id``, ``status``, ``filled_qty``,
        ``filled_avg_price``, ``env`` … — plus ``raw``, the broker's own payload, for
        the log or an audit trail. ``wait`` polls until the order is terminal or the
        configured timeout expires; a timeout is REPORTED (``timed_out``), never
        silently retried, because by then the order may be live.
        """
        self._check_guard()

        # Two switches in one place, deliberately in this order: nothing is built and
        # nothing is sent until the risk layer has had its say.
        if self.validator is not None:
            decision = self.validator.validate_signal(
                risk_signal or {"side": side, "price": reference_price},
                risk_state or {},
                price=reference_price,
            )
            if not decision.approved:
                raise OrderRefused(f"Vetoed by the risk layer — {decision.reason}")

        # Generated once and reused by every retry: Alpaca deduplicates on it, so a
        # submit that timed out and actually landed is never sent twice.
        client_id = client_order_id or f"traider-{uuid.uuid4().hex[:24]}"
        payload = build_order_payload(
            symbol=instrument,
            side=side,
            quantity=quantity,
            client_order_id=client_id,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            base_price=reference_price,
        )

        logger.warning(
            "%s ORDER %s %s x%s%s (client_order_id %s)",
            self.label,
            payload["side"].upper(),
            payload["symbol"],
            payload["qty"],
            " + bracket" if payload.get("order_class") == "bracket" else "",
            client_id,
        )

        order = execute_with_retry(
            lambda: self.client.submit_order(payload),
            max_retries=int(getattr(self.settings, "execution_max_retries", 3) or 0),
            base_delay=float(getattr(self.settings, "execution_retry_base_delay_seconds", 1.0) or 0.0),
            sleep=self._sleep,
            label=f"{self.label} order {payload['symbol']}",
        )

        result = self._summarise(order)
        if wait and not result["terminal"]:
            result = self.poll(
                result["order_id"],
                timeout=(
                    float(timeout)
                    if timeout is not None
                    else float(getattr(self.settings, "execution_order_timeout_seconds", 60) or 60)
                ),
            )
        return result

    def poll(self, order_id: str, *, timeout: Optional[float] = None, interval: float = 1.0) -> Dict[str, Any]:
        """Watch an order until it is done or the timeout expires.

        A partial fill is reported as it happens rather than waiting for a quantity that
        may never arrive: a broker that has filled 40 of 100 shares has changed the
        strategy's position, and the caller needs to know before the next bar.
        """
        limit = float(timeout if timeout is not None else getattr(self.settings, "execution_order_timeout_seconds", 60))
        deadline = time.monotonic() + max(0.0, limit)
        last: Dict[str, Any] = {}

        while True:
            order = self.client.order(str(order_id))
            last = self._summarise(order)
            if last["terminal"]:
                return last
            if time.monotonic() >= deadline:
                last["timed_out"] = True
                logger.warning(
                    "%s order %s still %s after %.0fs — reporting it as open, NOT retrying",
                    self.label, order_id, last["status"], limit,
                )
                return last
            if interval > 0:
                self._sleep(interval)

    def cancel(self, order_id: str) -> bool:
        return self.client.cancel_order(str(order_id))

    def cancel_open_orders(self, instrument: str) -> int:
        """Cancel every resting order for one instrument. Returns how many.

        Called before flattening, and it is not housekeeping — it is the fix for the
        bug bracket orders exist to avoid. A position that is closed while its stop and
        take-profit are still resting leaves two orders that will happily open the
        OPPOSITE position when they trigger.
        """
        cancelled = 0
        for order in self.client.open_orders(instrument):
            order_id = order.get("id")
            if not order_id:
                continue
            try:
                self.client.cancel_order(str(order_id))
                cancelled += 1
            except AlpacaError as exc:
                # Filling this instant is the ordinary reason a cancel fails. The
                # position check that follows is what decides what to do about it.
                logger.warning("%s could not cancel %s: %s", self.label, order_id, exc)
        return cancelled

    def last_fill_price(self, instrument: str) -> Optional[float]:
        """What the most recent FINISHED order in this instrument filled at, if any."""
        try:
            orders = self.client.closed_orders(instrument, limit=10)
        except AlpacaError as exc:
            logger.warning("%s could not read order history: %s", self.label, exc)
            return None
        for order in orders:
            if str(order.get("symbol") or "").upper() != str(instrument).upper():
                continue
            price = _as_float(order.get("filled_avg_price"))
            if price:
                return price
        return None

    def amend_exits(
        self,
        instrument: str,
        *,
        stop: Optional[float] = None,
        take: Optional[float] = None,
        reference_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Move the RESTING exits of the open position to ``stop`` / ``take``.

        A bracket is submitted from the price the strategy EXPECTED and fills at the price
        the broker actually got. Resting an exit computed from the expectation leaves a
        real position with its stop in the wrong place — a long filled above expectation
        would carry more risk than ``RISK_LIMIT_PERCENT`` asked for — so replacing the legs
        is what makes the stop the stop the backtest modelled, not housekeeping.

        Never raises and never cancels: a leg that cannot be replaced keeps the level it
        already had, which is still protection. Returns a report for the caller to log.
        """
        report: Dict[str, Any] = {"amended": [], "failed": [], "left": [], "reason": ""}
        if stop is None and take is None:
            report["reason"] = "no levels to amend"
            return report

        try:
            orders = self.client.open_orders(instrument)
        except AlpacaError as exc:
            report["reason"] = f"could not read the resting orders ({exc})"
            logger.warning("%s cannot amend exits: %s", self.label, report["reason"])
            return report

        for leg in _exit_legs(orders):
            order_id = leg.get("id")
            kind = str(leg.get("type") or "").lower()
            wanted = stop if kind.startswith("stop") else take
            field = "stop_price" if kind.startswith("stop") else "limit_price"
            current = _as_float(leg.get(field))

            if not order_id:
                continue
            if wanted is None:
                continue
            if current is not None and abs(current - float(wanted)) < MIN_STOP_DISTANCE:
                # Already where it should be: a call would be churn, and Alpaca rejects a
                # replace that changes nothing.
                continue
            if reference_price and not _level_is_replaceable(float(wanted), reference_price):
                report["left"].append(
                    {"order_id": order_id, "leg": kind, "reason": f"{field} {float(wanted):.4f} is not a level beside {reference_price}"}
                )
                continue

            payload = {field: f"{float(wanted):.2f}"}
            try:
                execute_with_retry(
                    lambda oid=order_id, p=payload: self.client.replace_order(oid, p),
                    max_retries=int(getattr(self.settings, "execution_max_retries", 3) or 0),
                    base_delay=float(getattr(self.settings, "execution_retry_base_delay_seconds", 1.0) or 0.0),
                    sleep=self._sleep,
                    label=f"{self.label} amend {kind} of {instrument}",
                )
            except (AlpacaError, OrderRefused) as exc:
                # The original leg is still working — that is Alpaca's documented
                # behaviour for a rejected replace, and the reason this is safe.
                report["failed"].append({"order_id": order_id, "leg": kind, "reason": str(exc)})
                logger.error(
                    "%s could not amend the %s of %s to %s: %s — the leg keeps its old level",
                    self.label, kind, instrument, wanted, exc,
                )
                continue
            report["amended"].append({"order_id": order_id, "leg": kind, field: float(wanted)})
            logger.warning(
                "%s AMENDED %s %s %s -> %.2f", self.label, instrument, kind, field, float(wanted)
            )

        if not report["amended"] and not report["failed"] and not report["left"]:
            report["reason"] = "no resting exit legs to amend"
        return report

    def flatten(self, instrument: str, reason: str = "") -> Dict[str, Any]:
        """Get out and stay out: cancel the resting exits, then close what is left.

        The two steps are in that order on purpose. If the bracket already did the job,
        cancelling the survivor is what stops it re-entering, and the freshly flat
        position is reported with the price the broker actually filled it at rather than
        a locally invented one.
        """
        self._check_guard()
        cancelled = self.cancel_open_orders(instrument)
        position = self.client.position(instrument)
        if position is None:
            price = self.last_fill_price(instrument)
            logger.warning(
                "%s %s was already flat%s (cancelled %d resting order(s))",
                self.label, instrument, f" ({reason})" if reason else "", cancelled,
            )
            return {
                "order_id": "", "client_order_id": "", "status": "filled",
                "terminal": True, "filled_qty": 0.0, "filled_avg_price": price,
                "symbol": str(instrument).upper(), "side": "", "order_class": "simple",
                "timed_out": False, "env": self.target.env, "live": bool(self.target.live),
                "raw": {}, "was_flat": True, "cancelled_orders": cancelled,
            }
        closed = self.close_position(instrument, reason)
        closed["was_flat"] = False
        closed["cancelled_orders"] = cancelled
        return closed

    def close_position(self, instrument: str, reason: str = "", *, wait: bool = True) -> Dict[str, Any]:
        """Liquidate, using the broker's own close endpoint.

        Preferred over a hand-built opposite order: Alpaca computes the side and the
        quantity from the position itself, so there is no residual to explain and no
        chance of sending the wrong way round.
        """
        self._check_guard()
        logger.warning("%s CLOSE %s%s", self.label, instrument, f" ({reason})" if reason else "")
        closed = execute_with_retry(
            lambda: self.client.close_position(instrument),
            max_retries=int(getattr(self.settings, "execution_max_retries", 3) or 0),
            base_delay=float(getattr(self.settings, "execution_retry_base_delay_seconds", 1.0) or 0.0),
            sleep=self._sleep,
            label=f"{self.label} close {instrument}",
        )
        summary = self._summarise(closed or {})
        summary["was_flat"] = closed is None
        # A close that has not filled is a position still open. Waiting is what makes the
        # caller's "flat" claim true rather than merely submitted.
        if wait and not summary["was_flat"] and not summary["terminal"]:
            summary = self.poll(
                summary["order_id"],
                timeout=float(getattr(self.settings, "execution_order_timeout_seconds", 60) or 60),
            )
            summary["was_flat"] = False
        return summary

    # -- shapes ------------------------------------------------------------
    def _summarise(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """The broker's payload reduced to the handful of fields that matter.

        ``raw`` is kept alongside: when an order does something unexpected, the original
        body is the evidence, and re-fetching it later is not always possible.
        """
        order = order or {}
        status = str(order.get("status") or "").lower()
        filled_qty = _as_float(order.get("filled_qty"))
        return {
            "order_id": order.get("id") or "",
            "client_order_id": order.get("client_order_id") or "",
            "status": status,
            "terminal": status in TERMINAL_STATUSES,
            "filled_qty": filled_qty,
            "filled_avg_price": _as_float(order.get("filled_avg_price")),
            "symbol": str(order.get("symbol") or "").upper(),
            "side": str(order.get("side") or "").upper(),
            "order_class": order.get("order_class") or "simple",
            "timed_out": False,
            "env": self.target.env,
            "live": bool(self.target.live),
            "raw": order,
        }


def _as_float(value: Any) -> Optional[float]:
    """Alpaca sends numbers as strings; ``None`` means "not filled yet"."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
