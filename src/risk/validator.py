"""Unified risk validator — the single gate every signal must pass.

``RiskValidator.validate_signal(signal, state)`` returns a :class:`RiskDecision`
whose ``approved`` and ``reason`` are the contract the plan specifies:

    Execution (Alpaca) must never be called without risk approval.

The decision also carries the :class:`~src.risk.position_sizing.Sizing` used, so
the executor knows *how much* to trade, and a ``checks`` map naming each test
that ran — that is what makes "why was this rejected?" answerable afterwards.
Every outcome (approve or veto) is logged.

The same validator is used by the backtest (``src/backtest/risk_sim.py``), which
is the point of task 24b: the Gate must measure the system that will trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from src.risk.circuit_breaker import CircuitBreaker
from src.risk.position_sizing import Sizing, size_position

logger = logging.getLogger(__name__)

__all__ = ["RiskDecision", "RiskValidator", "OPEN_LONG", "OPEN_SHORT", "CLOSE", "NONE"]

OPEN_LONG = "open_long"
OPEN_SHORT = "open_short"
CLOSE = "close"
NONE = "none"


def _get(obj, name: str, default=None):
    """Read ``name`` from a mapping or an object (either shape is accepted)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of one risk evaluation.

    ``approved, reason = validator.validate_signal(...)`` also works: the
    decision unpacks to that pair, matching the documented signature."""

    approved: bool
    reason: str
    action: str
    size: Optional[Sizing] = None
    checks: Dict[str, bool] = field(default_factory=dict)

    def __iter__(self):
        """Allow ``approved, reason = decision``."""
        return iter((self.approved, self.reason))

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "action": self.action,
            "size": self.size.as_dict() if self.size else None,
            "checks": dict(self.checks),
        }


class RiskValidator:
    """Applies position sizing, stop/take sanity, exposure and the circuit breaker."""

    def __init__(self, settings, breaker: Optional[CircuitBreaker] = None) -> None:
        self.settings = settings
        self.breaker = breaker or CircuitBreaker(
            max_consecutive_losses=getattr(settings, "max_consecutive_losses", 3),
            max_loss_percent=getattr(settings, "max_loss_percent", 10.0),
            enabled=bool(getattr(settings, "circuit_breaker_enabled", True)),
        )

    # ── helpers ─────────────────────────────────────────────────────
    def _sizing_kwargs(self, state) -> dict:
        return {
            "take_profit_percent": float(getattr(self.settings, "take_profit_percent", 0.0) or 0.0),
            "risk_limit_percent": float(getattr(self.settings, "risk_limit_percent", 2.0) or 0.0),
            "max_exposure_percent": float(getattr(self.settings, "max_exposure_percent", 100.0) or 0.0),
            "mode": str(getattr(self.settings, "position_sizing_mode", "fixed_risk")),
            "volatility_percent": _get(state, "volatility_percent"),
        }

    def validate_signal(
        self,
        signal,
        state=None,
        *,
        price: Optional[float] = None,
        equity: Optional[float] = None,
        day=None,
    ) -> RiskDecision:
        """Approve or veto one signal.

        ``signal`` needs ``side`` ("BUY"/"SELL") and ``price`` (or pass ``price``).
        ``state`` carries the account context: ``equity``, ``position`` (None or
        ``{side, quantity, entry_price, notional}``), ``day`` and optionally
        ``volatility_percent`` for volatility-targeted sizing.
        """
        side = str(_get(signal, "side", "BUY") or "BUY").upper()
        px = float(price if price is not None else (_get(signal, "price", 0.0) or 0.0))
        eq = float(equity if equity is not None else (_get(state, "equity", 0.0) or 0.0))
        position = _get(state, "position")
        checks: Dict[str, bool] = {}

        # 1) circuit breaker ──────────────────────────────────────────
        stopped, why = self.breaker.check(day if day is not None else _get(state, "day"))
        checks["circuit_breaker"] = not stopped
        if stopped:
            return self._veto(f"circuit breaker active: {why}", NONE, checks)

        # 2) what is this signal trying to do? ────────────────────────
        pos_side = str(_get(position, "side", "") or "").lower()
        if not pos_side:
            action = OPEN_LONG if side == "BUY" else OPEN_SHORT
        elif side == "BUY":
            action = CLOSE if pos_side == "short" else NONE
        else:
            action = CLOSE if pos_side == "long" else NONE

        # Closing needs no sizing and no stop; repeats are simply ignored.
        if action == CLOSE:
            checks["action"] = True
            decision = RiskDecision(True, f"closes the {pos_side} position", CLOSE, None, checks)
            logger.info("Risk approve: %s (%s @ %s)", decision.reason, side, px)
            return decision
        if action == NONE:
            checks["action"] = False
            return self._veto(f"already {pos_side or 'flat'} — repeat {side} ignored", NONE, checks)
        checks["action"] = True

        # 3) shorts must be enabled for this strategy ─────────────────
        allow_short = bool(getattr(self.settings, "allow_short", False))
        checks["side_allowed"] = action != OPEN_SHORT or allow_short
        if not checks["side_allowed"]:
            return self._veto("short positions are disabled for this strategy", action, checks)

        # 4) stop / take sanity ───────────────────────────────────────
        stop_pct = float(_get(signal, "stop_loss_percent") or getattr(self.settings, "stop_loss_percent", 0.0) or 0.0)
        take_pct = float(_get(signal, "take_profit_percent") or getattr(self.settings, "take_profit_percent", 0.0) or 0.0)
        checks["stop_loss"] = 0.0 < stop_pct < 100.0
        if not checks["stop_loss"]:
            return self._veto(
                f"stop loss must be between 0% and 100% (got {stop_pct:g}%) — cannot size the trade",
                action, checks,
            )
        # A take profit at or inside the stop is not a veto (it is a valid, if
        # pessimistic, choice) but it is worth surfacing.
        checks["take_profit"] = take_pct == 0.0 or take_pct > stop_pct
        if not checks["take_profit"]:
            logger.warning(
                "Take profit (%g%%) is not wider than the stop (%g%%) — expectancy is negative",
                take_pct, stop_pct,
            )

        # 5) size it, capped by exposure ──────────────────────────────
        existing = float(_get(position, "notional", 0.0) or 0.0)
        sizing = size_position(
            equity=eq,
            price=px,
            stop_loss_percent=stop_pct,
            existing_notional=existing,
            side="short" if action == OPEN_SHORT else "long",
            **self._sizing_kwargs(state),
        )
        checks["exposure"] = sizing.quantity > 0
        if not checks["exposure"]:
            reason = (
                "no exposure left (max exposure already committed)"
                if existing > 0
                else "position size rounds to zero — equity, price or risk limit too small"
            )
            return self._veto(reason, action, checks)

        decision = RiskDecision(
            True,
            f"{action} {sizing.quantity} @ {px:g} ({sizing.weight * 100:.1f}% of equity, "
            f"stop {sizing.stop_percent:g}%)",
            action,
            sizing,
            checks,
        )
        logger.info("Risk approve: %s", decision.reason)
        return decision

    def _veto(self, reason: str, action: str, checks: Dict[str, bool]) -> RiskDecision:
        """Build + log a rejection. Every veto is logged with its reason."""
        logger.warning("Risk VETO: %s", reason)
        return RiskDecision(False, reason, action, None, checks)

    # ── bookkeeping passthrough ─────────────────────────────────────
    def record_trade(self, pnl_percent: float, day=None):
        """Feed a closed trade back into the circuit breaker."""
        return self.breaker.record_trade(pnl_percent, day=day)

    def halt_today(self, day=None, reason: str = "manual halt") -> None:
        self.breaker.stop_trading_today(day=day, reason=reason)
