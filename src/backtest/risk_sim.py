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

``RiskConfig`` is now literally ``StrategyConfig`` — an alias, not a copy. It used to be
a second dataclass carrying the same fields, which is exactly the sort of parallel
definition that drifts: the panel, the backtest and the machine would each have had
their own idea of what "stop loss" means and only the tests would ever notice. Callers
keep the name they have always used, and get the object the machine reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from src.strategy.config import StrategyConfig
from src.strategy.engine import (
    FORCED,
    SIGNAL,
    STOP,
    TAKE,
    Bar,
    StrategyEngine,
)

logger = logging.getLogger(__name__)

# The settings a backtest replays ARE the settings the machine reads. ``None`` on any of
# them means NOT APPLIED — leave that behaviour out of the run — and empty is therefore a
# real instruction rather than an accident (see ``StrategyConfig``).
RiskConfig = StrategyConfig

__all__ = ["RiskConfig", "RiskResult", "apply_risk_layer", "STOP", "TAKE", "SIGNAL", "FORCED"]


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
) -> RiskResult:
    """Replay ``signals`` through the shared strategy machine.

    Same signature as before — minus the master switch, which no longer exists: what
    the ``config`` sets is applied, and what it leaves ``None`` is not.
    """
    engine_config = config.to_strategy_config(slippage=slippage, commission=commission)
    if allow_short and not engine_config.allow_short:
        engine_config = StrategyConfig(**{**engine_config.__dict__, "allow_short": True})
    engine = StrategyEngine(engine_config)

    bars = [
        Bar(
            index=k,
            time=None,
            open=float(opens[k]),
            close=float(closes[k]),
            high=(float(highs[k]) if highs is not None and k < len(highs) else None),
            low=(float(lows[k]) if lows is not None and k < len(lows) else None),
        )
        for k in range(n)
    ]
    ledger = engine.run(list(signals), bars)
    return RiskResult(
        returns=ledger.returns,
        legs=ledger.legs,
        active=ledger.active,
        in_position_bars=ledger.in_position_bars,
        stats=ledger.stats(),
    )
