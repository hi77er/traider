"""What the strategy remembers between bars — and what gets persisted between runs.

The whole point of a bar-at-a-time machine is that its memory is explicit: a position,
the breaker's counters, and which bar was decided last. In the backtest that memory is
a loop variable; live it is a file. One class, serialized, so a restart cannot invent a
different answer to "am I long?" than the run it continues.

``last_bar`` is the idempotency key. A tick that arrives twice for the same closed bar
must not open a second position, and a tick that arrives after a restart must not
re-decide a bar the previous process already acted on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open position, in the terms the strategy reasons about."""

    entry_index: int
    entry_price: float          # cost-adjusted: what was actually paid/received
    raw_entry_price: float      # the untouched price at that bar's open
    short: bool
    stop: Optional[float]
    take: Optional[float]
    weight: float
    stop_pct: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entry_index": self.entry_index,
            "entry_price": self.entry_price,
            "raw_entry_price": self.raw_entry_price,
            "short": self.short,
            "stop": self.stop,
            "take": self.take,
            "weight": self.weight,
            "stop_pct": self.stop_pct,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Position":
        return cls(
            entry_index=int(data.get("entry_index", 0)),
            entry_price=float(data["entry_price"]),
            raw_entry_price=float(data.get("raw_entry_price", data["entry_price"])),
            short=bool(data.get("short", False)),
            stop=data.get("stop"),
            take=data.get("take"),
            weight=float(data.get("weight", 1.0)),
            stop_pct=float(data.get("stop_pct", 0.0)),
        )


@dataclass
class StrategyState:
    """The strategy's memory: at most one open position, plus the breaker."""

    position: Optional[Position] = None
    # The index of the bar being decided (0-based), so trade rows keep their indices
    # in a live run as they do in a backtest.
    bar_index: int = 0
    # Timestamp of the bar whose decision has been acted on — the idempotency key.
    last_decided_bar: Optional[str] = None
    breaker: Optional[CircuitBreaker] = None
    breaker_skips: int = 0
    # The first weight the sizing produced, reported as "the" weight of the run.
    first_weight: float = 1.0
    weight_seen: bool = False

    # -- breaker -----------------------------------------------------------
    def ensure_breaker(self, max_consecutive_losses: int, max_loss_percent: float) -> CircuitBreaker:
        """The breaker for this run, created on first use and then reused."""
        if self.breaker is None:
            self.breaker = CircuitBreaker(
                max_consecutive_losses=max_consecutive_losses,
                max_loss_percent=max_loss_percent,
                enabled=True,
            )
        return self.breaker

    # -- serialization -----------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return {
            "position": self.position.as_dict() if self.position else None,
            "bar_index": self.bar_index,
            "last_decided_bar": self.last_decided_bar,
            "breaker": self.breaker.snapshot() if self.breaker is not None else None,
            "breaker_skips": self.breaker_skips,
            "first_weight": self.first_weight,
            "weight_seen": self.weight_seen,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "StrategyState":
        """Restore from a state file. A corrupt file yields a FLAT state.

        Deliberately flat rather than "keep trading as if nothing happened": an
        unreadable position is not a position, and the reconciliation step against the
        broker is what decides whether the bot is long — see ``LiveDriver``.
        """
        data = data or {}
        state = cls()
        try:
            raw_pos = data.get("position")
            state.position = Position.from_dict(raw_pos) if raw_pos else None
            state.bar_index = int(data.get("bar_index", 0) or 0)
            state.last_decided_bar = data.get("last_decided_bar")
            state.breaker_skips = int(data.get("breaker_skips", 0) or 0)
            state.first_weight = float(data.get("first_weight", 1.0) or 1.0)
            state.weight_seen = bool(data.get("weight_seen", False))
            raw_breaker = data.get("breaker")
            if raw_breaker:
                state.breaker = CircuitBreaker(
                    max_consecutive_losses=int(raw_breaker.get("max_consecutive_losses", 3) or 3),
                    max_loss_percent=float(raw_breaker.get("max_loss_percent", 10.0) or 10.0),
                    enabled=True,
                )
                state.breaker.restore(raw_breaker)
        except (TypeError, ValueError, KeyError) as exc:  # noqa: BLE001
            logger.warning("Unreadable strategy state — starting flat: %s", exc)
            return cls()
        return state
