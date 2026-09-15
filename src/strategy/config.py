"""The knobs the strategy machine reads, resolved from a strategy's settings.

One object, built the same way by every driver: the backtest resolves it from the
effective settings of the strategy under test, and a live run resolves it from the
active strategy. That is what makes "the backtest measured what will run" checkable —
the two cannot differ unless the settings do.

Costs are part of the configuration rather than of the broker because they are a MODEL
of execution, not execution: the simulator charges them, and live passes zero and
records what the broker really charged. Keeping them here means the stop LEVEL is
derived from a cost-adjusted entry in both paths, so a stop is where the simulation
says it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.position_sizing import DEFAULT_VOLATILITY_PERIOD


@dataclass(frozen=True)
class StrategyConfig:
    """Everything the decision machine needs, and nothing about I/O."""

    # Is the risk layer applied at all? Off = raw signals, full notional, no stops.
    enabled: bool = True
    allow_short: bool = False
    # A fired rule's confidence must clear its side's threshold, or the bar is a HOLD.
    buy_threshold: float = 0.6
    sell_threshold: float = 0.6
    # Risk layer
    risk_limit_percent: float = 2.0
    stop_loss_percent: float = 2.0
    take_profit_percent: float = 4.0
    max_exposure_percent: float = 100.0
    max_loss_percent: float = 10.0
    max_consecutive_losses: int = 3
    circuit_breaker_enabled: bool = True
    sizing_mode: str = "fixed_risk"
    volatility_period: int = DEFAULT_VOLATILITY_PERIOD
    # Cost model, as a fraction charged on each side (0.0005 = 5 bps).
    slippage: float = 0.0
    commission: float = 0.0

    @property
    def cost(self) -> float:
        """Fraction charged per fill (charged on entry and on exit)."""
        return float(self.slippage) + float(self.commission)

    @property
    def uses_breaker(self) -> bool:
        return bool(self.enabled and self.circuit_breaker_enabled)

    @classmethod
    def from_settings(
        cls,
        settings,
        *,
        enabled: bool = True,
        slippage: float = 0.0,
        commission: float = 0.0,
    ) -> "StrategyConfig":
        """Resolve from a ``Settings``-shaped object (works with test stubs too).

        ``slippage``/``commission`` are passed in rather than read here: they are the
        simulator's cost model, and a live run deliberately passes zero so the real
        fill from the broker is what counts.
        """
        return cls(
            enabled=bool(enabled),
            allow_short=bool(getattr(settings, "allow_short", False)),
            buy_threshold=float(getattr(settings, "model_buy_threshold", 0.6) or 0.0),
            sell_threshold=float(getattr(settings, "model_sell_threshold", 0.6) or 0.0),
            risk_limit_percent=float(getattr(settings, "risk_limit_percent", 2.0) or 0.0),
            stop_loss_percent=float(getattr(settings, "stop_loss_percent", 0.0) or 0.0),
            take_profit_percent=float(getattr(settings, "take_profit_percent", 0.0) or 0.0),
            max_exposure_percent=float(getattr(settings, "max_exposure_percent", 100.0) or 0.0),
            max_loss_percent=float(getattr(settings, "max_loss_percent", 10.0) or 0.0),
            max_consecutive_losses=int(getattr(settings, "max_consecutive_losses", 3) or 3),
            circuit_breaker_enabled=bool(getattr(settings, "circuit_breaker_enabled", True)),
            sizing_mode=str(getattr(settings, "position_sizing_mode", "fixed_risk")),
            slippage=float(slippage),
            commission=float(commission),
        )
