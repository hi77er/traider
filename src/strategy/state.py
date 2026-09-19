"""What the strategy remembers between bars — and what gets persisted between runs.

The whole point of a bar-at-a-time machine is that its memory is explicit: a position
and which bar was decided last. In the backtest that memory is a loop variable; live it
is a file. One class, serialized, so a restart cannot invent a different answer to "am
I long?" than the run it continues.

``last_decided_bar`` is the idempotency key. A tick that arrives twice for the same
closed bar must not open a second position, and a tick that arrives after a restart must
not re-decide a bar the previous process already acted on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open position, in the terms the strategy reasons about."""

    entry_index: int
    entry_price: float          # cost-adjusted: what was actually paid/received
    raw_entry_price: float      # the untouched price at that bar's open
    short: bool
    # ``None`` when the level was not configured. A position without a stop is a
    # position the operator asked to hold until the signal turns, and the machine
    # must not invent a level for it.
    stop: Optional[float]
    take: Optional[float]
    weight: float
    stop_pct: Optional[float] = None

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
            stop_pct=data.get("stop_pct"),
        )


@dataclass
class StrategyState:
    """The strategy's memory: at most one open position, and the last bar decided."""

    position: Optional[Position] = None
    # The index of the bar being decided (0-based), so trade rows keep their indices
    # in a live run as they do in a backtest.
    bar_index: int = 0
    # Timestamp of the bar whose decision has been acted on — the idempotency key.
    last_decided_bar: Optional[str] = None
    # The first weight the sizing produced, reported as "the" weight of the run.
    first_weight: float = 1.0
    weight_seen: bool = False
    # The last entry the BROKER refused, and what it said. The engine records a position when
    # it builds the intent, before the order is sent, so a refused entry leaves a position the
    # broker never opened — and this is the only thing that can explain it to an operator.
    refused_entry: Optional[Dict[str, Any]] = None

    # -- serialization -----------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return {
            "position": self.position.as_dict() if self.position else None,
            "bar_index": self.bar_index,
            "last_decided_bar": self.last_decided_bar,
            "first_weight": self.first_weight,
            "weight_seen": self.weight_seen,
            "refused_entry": dict(self.refused_entry) if self.refused_entry else None,
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
            state.first_weight = float(data.get("first_weight", 1.0) or 1.0)
            state.weight_seen = bool(data.get("weight_seen", False))
            state.refused_entry = data.get("refused_entry") or None
        except (TypeError, ValueError, KeyError) as exc:  # noqa: BLE001
            logger.warning("Unreadable strategy state — starting flat: %s", exc)
            return cls()
        return state
