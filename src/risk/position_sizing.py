"""Position sizing for the risk layer (pure — no web/broker imports).

Every signal is *sized* before it can reach a broker::

    position_notional = equity x (RISK_LIMIT_PERCENT / stop_distance_percent)

The idea is that risking ``RISK_LIMIT_PERCENT`` of the account means the position
is just large enough that being stopped out ``stop_distance_percent`` away costs
exactly that much. The result is capped by ``MAX_EXPOSURE_PERCENT`` and by
whatever exposure open positions already use.

Two modes (``POSITION_SIZING_MODE``):

* ``fixed_risk`` — the stop distance is ``STOP_LOSS_PERCENT``.
* ``volatility_target`` — the stop distance is the *trailing realised
  volatility* (``volatility_percent`` passed in by the caller), so a quiet market
  gets a bigger position and a wild one a smaller position for the same risk.

**The backtest calls this same code**, so a simulated trade is sized exactly like
the live one (see ``src/strategy/engine.py``, the single implementation both
runs share).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

__all__ = [
    "MIN_VOLATILITY_PERCENT",
    "Sizing",
    "stop_distance",
    "target_weight",
    "realized_volatility_percent",
    "size_position",
]

# Floor for volatility-derived stop distances: a dead-quiet stretch would
# otherwise produce a near-zero stop and a wild position size.
MIN_VOLATILITY_PERCENT = 0.25
DEFAULT_VOLATILITY_PERIOD = 14


@dataclass(frozen=True)
class Sizing:
    """The result of sizing one entry."""

    quantity: int
    notional: float
    weight: float            # notional / equity (0 when nothing can be opened)
    risk_amount: float       # what being stopped out costs at this size
    stop_distance: float     # absolute price distance to the stop
    stop_percent: float      # the distance as a % of price (the one actually used)
    stop_price: Optional[float]
    take_profit_price: Optional[float]
    capped: bool             # True when MAX_EXPOSURE_PERCENT shrank the position
    mode: str

    def as_dict(self) -> dict:
        return {
            "quantity": self.quantity,
            "notional": round(self.notional, 2),
            "weight": round(self.weight, 6),
            "risk_amount": round(self.risk_amount, 2),
            "stop_distance": round(self.stop_distance, 6),
            "stop_percent": round(self.stop_percent, 4),
            "stop_price": None if self.stop_price is None else round(self.stop_price, 6),
            "take_profit_price": (
                None if self.take_profit_price is None else round(self.take_profit_price, 6)
            ),
            "capped": self.capped,
            "mode": self.mode,
        }


def stop_distance(price: float, percent: float) -> float:
    """Absolute price distance for a ``percent`` move of ``price``."""
    return abs(float(price)) * max(0.0, float(percent)) / 100.0


def target_weight(
    risk_limit_percent: float, stop_percent: float, max_exposure_percent: float = 100.0
) -> float:
    """Fraction of equity to deploy, before the exposure cap (0..1).

    With ``RISK_LIMIT_PERCENT=2`` and a 4% stop this is 0.5 (half the account);
    with a 2% stop it is 1.0 (fully invested). A stop of 0 has no defined
    distance, so nothing can be sized — that returns 0.0.
    """
    if stop_percent <= 0:
        return 0.0
    weight = max(0.0, float(risk_limit_percent)) / float(stop_percent)
    return min(weight, max(0.0, float(max_exposure_percent)) / 100.0)


def realized_volatility_percent(
    closes: Sequence[float], period: int = DEFAULT_VOLATILITY_PERIOD
) -> Optional[float]:
    """Std-dev of the last ``period`` simple returns, in %.

    Returns ``None`` when there is not enough history, so the caller can fall
    back to the fixed stop distance. The result is floored at
    ``MIN_VOLATILITY_PERCENT``.
    """
    series = [float(c) for c in (closes or ()) if c is not None and float(c) > 0]
    if len(series) < 3:
        return None
    window: List[float] = series[-(int(period) + 1):]
    rets = [window[i] / window[i - 1] - 1.0 for i in range(1, len(window))]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var) * 100.0, MIN_VOLATILITY_PERCENT)


def size_position(
    equity: float,
    price: float,
    stop_loss_percent: float,
    *,
    take_profit_percent: float = 0.0,
    risk_limit_percent: float = 2.0,
    max_exposure_percent: float = 100.0,
    existing_notional: float = 0.0,
    side: str = "long",
    mode: str = "fixed_risk",
    volatility_percent: Optional[float] = None,
) -> Sizing:
    """Size one entry. ``quantity`` is 0 when nothing can be opened.

    ``side`` decides which way the stop/take levels sit (a short's stop is
    *above* its entry). ``existing_notional`` is the notional already committed
    by open positions, which is subtracted from the exposure allowance.
    """
    equity = max(0.0, float(equity))
    price = abs(float(price))
    short = str(side).lower() in ("short", "sell")
    stop_pct = max(0.0, float(stop_loss_percent))
    mode = "volatility_target" if str(mode) == "volatility_target" else "fixed_risk"

    if mode == "volatility_target" and volatility_percent:
        stop_pct = max(float(volatility_percent), MIN_VOLATILITY_PERCENT)

    # Unusable inputs: no price, no equity or no stop distance to size against.
    if price <= 0 or equity <= 0 or stop_pct <= 0:
        return Sizing(
            quantity=0, notional=0.0, weight=0.0, risk_amount=0.0,
            stop_distance=0.0, stop_percent=stop_pct,
            stop_price=None, take_profit_price=None, capped=False, mode=mode,
        )

    risk_amount = equity * max(0.0, float(risk_limit_percent)) / 100.0
    allowance = max(
        0.0, equity * max(0.0, float(max_exposure_percent)) / 100.0 - max(0.0, float(existing_notional))
    )
    wanted = min(risk_amount / (stop_pct / 100.0), allowance)
    capped = wanted < risk_amount / (stop_pct / 100.0) - 1e-9

    distance = stop_distance(price, stop_pct)
    quantity = int(math.floor(wanted / price)) if price > 0 else 0
    notional = quantity * price
    take = abs(float(take_profit_percent))

    return Sizing(
        quantity=quantity,
        notional=notional,
        weight=(notional / equity) if equity else 0.0,
        risk_amount=notional * stop_pct / 100.0,
        stop_distance=distance,
        stop_percent=stop_pct,
        stop_price=price + distance if short else price - distance,
        take_profit_price=(
            price - stop_distance(price, take) if short else price + stop_distance(price, take)
        ) if take > 0 else None,
        capped=capped,
        mode=mode,
    )
