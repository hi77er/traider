"""The risk layer applied inside the backtest — an ADAPTER, not an implementation.

This module used to hold its own copy of the trading logic: fill at the next bar's
open, stop before take, sizing, the breaker, the one-position rule. So did
``engine.simulate_frame``, kept in step by a docstring promising they mirrored each
other. Two copies of the same rules is one copy too many, and a live loop would have
been the third.

The rules now live in ``src/strategy`` — the same object a live run drives — and what
is left here is the translation between the backtest's batch vocabulary (arrays,
indices, a preallocated return series) and that machine's bar-at-a-time one. Nothing in
this file decides anything: :meth:`src.strategy.engine.StrategyEngine.step` does, and
``tests/test_strategy_parity.py`` fails if a decision ever reappears here. The raw
replay (``engine.simulate_frame``, ``engine.position_intervals``) delegates here too,
with the risk layer OFF, so that third copy is gone as well.

``RiskConfig`` stays as the settings-shaped face of ``StrategyConfig``, so the callers
(``engine``, ``signal_service``, the tests) read the same names they always did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from src.risk.position_sizing import DEFAULT_VOLATILITY_PERIOD
from src.strategy.config import StrategyConfig
from src.strategy.engine import (
    BREAKER,
    FORCED,
    SIGNAL,
    STOP,
    TAKE,
    Bar,
    StrategyEngine,
)

logger = logging.getLogger(__name__)

__all__ = ["RiskConfig", "RiskResult", "apply_risk_layer", "STOP", "TAKE", "SIGNAL", "FORCED", "BREAKER"]


@dataclass(frozen=True)
class RiskConfig:
    """The risk settings a backtest replays. ``enabled=False`` = raw strategy."""

    enabled: bool = True
    risk_limit_percent: float = 2.0
    stop_loss_percent: float = 2.0
    take_profit_percent: float = 4.0
    max_exposure_percent: float = 100.0
    max_loss_percent: float = 10.0
    max_consecutive_losses: int = 3
    circuit_breaker_enabled: bool = True
    sizing_mode: str = "fixed_risk"
    volatility_period: int = DEFAULT_VOLATILITY_PERIOD
    allow_short: bool = False

    @classmethod
    def from_settings(cls, settings, enabled: bool = True) -> "RiskConfig":
        """Build from a ``Settings``-shaped object (works with test stubs too)."""
        return cls(
            enabled=bool(enabled),
            risk_limit_percent=float(getattr(settings, "risk_limit_percent", 2.0) or 0.0),
            stop_loss_percent=float(getattr(settings, "stop_loss_percent", 0.0) or 0.0),
            take_profit_percent=float(getattr(settings, "take_profit_percent", 0.0) or 0.0),
            max_exposure_percent=float(getattr(settings, "max_exposure_percent", 100.0) or 0.0),
            max_loss_percent=float(getattr(settings, "max_loss_percent", 10.0) or 0.0),
            max_consecutive_losses=int(getattr(settings, "max_consecutive_losses", 3) or 3),
            circuit_breaker_enabled=bool(getattr(settings, "circuit_breaker_enabled", True)),
            sizing_mode=str(getattr(settings, "position_sizing_mode", "fixed_risk")),
            allow_short=bool(getattr(settings, "allow_short", False)),
        )

    def as_dict(self) -> dict:
        return {
            "applied": self.enabled,
            "risk_limit_percent": self.risk_limit_percent,
            "stop_loss_percent": self.stop_loss_percent,
            "take_profit_percent": self.take_profit_percent,
            "max_exposure_percent": self.max_exposure_percent,
            "max_loss_percent": self.max_loss_percent,
            "max_consecutive_losses": self.max_consecutive_losses,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "sizing_mode": self.sizing_mode,
        }

    def to_strategy_config(self, *, slippage: float = 0.0, commission: float = 0.0) -> StrategyConfig:
        """The machine's config, carrying this backtest's cost model."""
        return StrategyConfig(
            enabled=self.enabled,
            allow_short=self.allow_short,
            risk_limit_percent=self.risk_limit_percent,
            stop_loss_percent=self.stop_loss_percent,
            take_profit_percent=self.take_profit_percent,
            max_exposure_percent=self.max_exposure_percent,
            max_loss_percent=self.max_loss_percent,
            max_consecutive_losses=self.max_consecutive_losses,
            circuit_breaker_enabled=self.circuit_breaker_enabled,
            sizing_mode=self.sizing_mode,
            volatility_period=self.volatility_period,
            slippage=slippage,
            commission=commission,
        )


@dataclass
class RiskResult:
    """Per-bar returns, the risk-adjusted legs, and what the layer did."""

    returns: List[float] = field(default_factory=list)
    legs: List[dict] = field(default_factory=list)
    active: List[bool] = field(default_factory=list)
    in_position_bars: int = 0
    stats: Dict[str, object] = field(default_factory=dict)

    @property
    def trades(self) -> List[dict]:
        """Only the legs that actually traded."""
        return [leg for leg in self.legs if not leg.get("skipped")]


def apply_risk_layer(
    signals: Sequence[str],
    opens: Sequence[float],
    highs: Optional[Sequence[float]],
    lows: Optional[Sequence[float]],
    closes: Sequence[float],
    n: int,
    *,
    config: RiskConfig,
    allow_short: bool = False,
    slippage: float = 0.0,
    commission: float = 0.0,
    days: Optional[Sequence[str]] = None,
) -> RiskResult:
    """Replay ``signals`` through the shared strategy machine.

    Same signature as before, so every existing caller — and the tests that pinned its
    behaviour — reads exactly what it did. ``days`` are the per-bar day keys the breaker
    groups by; without them the breaker is skipped, because there is no notion of
    "today" to halt.
    """
    engine_config = config.to_strategy_config(slippage=slippage, commission=commission)
    if allow_short and not engine_config.allow_short:
        engine_config = StrategyConfig(**{**engine_config.__dict__, "allow_short": True})
    engine = StrategyEngine(engine_config, days_available=bool(days))

    bars = [
        Bar(
            index=k,
            time=(days[k] if days and k < len(days) else None),
            open=float(opens[k]),
            close=float(closes[k]),
            high=(float(highs[k]) if highs is not None and k < len(highs) else None),
            low=(float(lows[k]) if lows is not None and k < len(lows) else None),
        )
        for k in range(n)
    ]
    ledger = engine.run(list(signals), bars, days=days)
    stats = ledger.stats(applied=bool(config.enabled))
    return RiskResult(
        returns=ledger.returns,
        legs=ledger.legs,
        active=ledger.active,
        in_position_bars=ledger.in_position_bars,
        stats=stats,
    )
