"""Circuit breaker — stop trading for the day when losses pile up.

Two independent triggers (plan task 23):

* ``MAX_CONSECUTIVE_LOSSES`` closed trades in a row at a loss, or
* the day's realised P&L falling past ``MAX_LOSS_PERCENT``.

Both are evaluated per trading *day* and reset automatically when the day rolls
over, so a bad morning cannot poison the next session. ``CIRCUIT_BREAKER_ENABLED``
switches the whole thing off.

The state lives in memory here. Persisting it (so a crash/restart cannot clear
the loss counter mid-drawdown) is the state tracker's job — task 13 in Phase 5 —
which is why :meth:`CircuitBreaker.snapshot` / :meth:`restore` exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["BreakerState", "CircuitBreaker"]


def _day_key(day) -> str:
    """Normalise a day to ``YYYY-MM-DD`` (accepts date/datetime/str/None)."""
    if day is None:
        return date.today().isoformat()
    if isinstance(day, str):
        return day[:10]
    isoformat = getattr(day, "isoformat", None)
    if callable(isoformat):
        return isoformat()[:10]
    return str(day)[:10]


@dataclass
class BreakerState:
    """Mutable breaker state for one strategy."""

    day: Optional[str] = None
    consecutive_losses: int = 0
    daily_pnl_percent: float = 0.0
    trades_today: int = 0
    tripped: bool = False
    reason: str = ""
    trips: int = 0

    def as_dict(self) -> dict:
        return {
            "day": self.day,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl_percent": round(self.daily_pnl_percent, 4),
            "trades_today": self.trades_today,
            "tripped": self.tripped,
            "reason": self.reason,
            "trips": self.trips,
        }


class CircuitBreaker:
    """Tracks consecutive losses and the day's P&L; vetoes trading when tripped."""

    def __init__(
        self,
        max_consecutive_losses: int = 3,
        max_loss_percent: float = 10.0,
        enabled: bool = True,
    ) -> None:
        self.max_consecutive_losses = max(1, int(max_consecutive_losses))
        self.max_loss_percent = max(0.0, float(max_loss_percent))
        self.enabled = bool(enabled)
        self.state = BreakerState()

    # ── day handling ────────────────────────────────────────────────
    def roll(self, day=None) -> bool:
        """Start a new day when ``day`` differs from the state's day.

        A new day is a clean slate for the things that are scoped to a day: the
        streak, the day's P&L and the halt all reset ("stop trading for the day,
        reset next day"). ``trips`` is deliberately CARRIED ACROSS: it is a
        run-level statistic, not day state, and rebuilding the state without it
        made the reported count depend on whether the run happened to end inside
        the tripping day.

        NOTE the consequence for a **daily** bar backtest, where a day holds
        exactly one decision and the day therefore changes between every entry and
        the next:

        * the *consecutive-losses* trigger can never fire at all — the streak is
          reset before it can reach the limit;
        * the *daily-loss* trigger does fire, but the halt is cleared by the next
          day's roll before any entry is evaluated, so it refuses nothing.

        So on daily bars the breaker is inert either way, and a run's stops and
        sizing are what protect it. It bites at intraday bar sizes (and live),
        which is where multiple decisions share a day.
        Returns True when a roll happened."""
        key = _day_key(day)
        if self.state.day == key:
            return False
        self.state = BreakerState(day=key, trips=self.state.trips)
        logger.debug("Circuit breaker reset for a new day (%s)", key)
        return True

    def reset(self, day=None) -> None:
        """Force a reset (new day, or an operator clearing the breaker).

        The trip COUNT survives, for the same reason it survives a roll: clearing a
        halt does not un-happen it.
        """
        self.state = BreakerState(day=_day_key(day), trips=self.state.trips)

    # ── evaluation ──────────────────────────────────────────────────
    def check(self, day=None) -> Tuple[bool, str]:
        """``(tripped, reason)`` for ``day``. Rolls the day first."""
        if not self.enabled:
            return False, ""
        self.roll(day)
        if self.state.tripped:
            return True, self.state.reason
        return False, ""

    def tripped(self, day=None) -> bool:
        return self.check(day)[0]

    def _trip(self, reason: str) -> None:
        if not self.state.tripped:
            self.state.tripped = True
            self.state.reason = reason
            self.state.trips += 1
            logger.warning("Circuit breaker TRIPPED: %s", reason)

    def stop_trading_today(self, day=None, reason: str = "manual halt") -> None:
        """Halt the rest of the day (plan name: ``STOP_TRADING_TODAY()``)."""
        self.roll(day)
        self._trip(reason)

    # ── bookkeeping ─────────────────────────────────────────────────
    def record_trade(self, pnl_percent: float, day=None) -> BreakerState:
        """Record a *closed* trade's result and re-evaluate the triggers."""
        self.roll(day)
        pnl = float(pnl_percent)
        self.state.trades_today += 1
        self.state.daily_pnl_percent += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        if self.enabled and not self.state.tripped:
            if self.state.consecutive_losses >= self.max_consecutive_losses:
                self._trip(
                    f"{self.state.consecutive_losses} consecutive losses "
                    f"(limit {self.max_consecutive_losses})"
                )
            elif self.state.daily_pnl_percent <= -self.max_loss_percent:
                self._trip(
                    f"daily loss {self.state.daily_pnl_percent:.2f}% "
                    f"(limit -{self.max_loss_percent:.2f}%)"
                )
        return self.state

    # ── persistence hooks (state tracker, task 13) ──────────────────
    def snapshot(self) -> Dict[str, object]:
        return self.state.as_dict()

    def restore(self, data: Optional[dict]) -> None:
        """Restore from :meth:`snapshot` (e.g. after a restart)."""
        if not data:
            return
        self.state = BreakerState(
            day=data.get("day"),
            consecutive_losses=int(data.get("consecutive_losses") or 0),
            daily_pnl_percent=float(data.get("daily_pnl_percent") or 0.0),
            trades_today=int(data.get("trades_today") or 0),
            tripped=bool(data.get("tripped")),
            reason=str(data.get("reason") or ""),
            trips=int(data.get("trips") or 0),
        )
