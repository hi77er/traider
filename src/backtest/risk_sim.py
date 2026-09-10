"""Apply the live risk layer inside the backtest (plan task 24b).

The engine used to replay raw signals: full-notional entries, exits only on the
opposite signal, no halt. Live, every entry goes through the risk validator —
sized by ``RISK_LIMIT_PERCENT`` behind a ``STOP_LOSS_PERCENT`` stop, closed early
by that stop or by the take profit, and blocked outright while the circuit
breaker is tripped. So the Gate was measuring a *different system* from the one
that would trade.

This module replays the same rules over the **same legs** the engine already
derives (``engine.position_intervals``), so entry/exit semantics cannot drift:

* each leg is sized exactly like ``src.risk.position_sizing.size_position`` would
  size the live order (same function, same caps);
* a stop/take hit truncates the leg and books the exit at that level;
* a tripped breaker drops the leg entirely (the position is never opened);
* every bar's return is scaled by the leg's weight.

**Invariant:** with ``RiskConfig(enabled=False)`` this reproduces
``engine.simulate_frame`` exactly — same returns, same bars, no stops, weight 1.
A test asserts it, so the two paths cannot silently diverge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from src.risk.circuit_breaker import CircuitBreaker
from src.risk.position_sizing import (
    DEFAULT_VOLATILITY_PERIOD,
    realized_volatility_percent,
    stop_distance,
    target_weight,
)

logger = logging.getLogger(__name__)

__all__ = ["RiskConfig", "RiskResult", "apply_risk_layer"]

STOP, TAKE, SIGNAL, FORCED, BREAKER = "stop", "take", "signal", "forced", "circuit_breaker"


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
    """Replay ``signals`` with sizing, stops/takes and the circuit breaker.

    Mirrors ``engine.position_intervals`` exactly — a signal on bar *s* fills at
    bar *s+1*'s open, one position at a time, opposing repeats ignored — but adds
    the risk gate. After a stop-out the strategy is flat and **can re-enter** on
    the next signal, exactly as the live loop would.

    ``days`` are the per-bar day keys the breaker groups by; without them the
    breaker is skipped (there is no notion of "today" to halt).
    """
    cost = float(slippage) + float(commission)
    returns: List[float] = [0.0] * max(n - 1, 0)
    active: List[bool] = [False] * max(n - 1, 0)
    legs: List[dict] = []

    use_breaker = bool(config.enabled and config.circuit_breaker_enabled and days)
    breaker = (
        CircuitBreaker(
            max_consecutive_losses=config.max_consecutive_losses,
            max_loss_percent=config.max_loss_percent,
            enabled=True,
        )
        if use_breaker
        else None
    )
    stats: Dict[str, object] = {
        "applied": bool(config.enabled),
        "weight": 1.0,
        "stop_exits": 0,
        "take_exits": 0,
        "signal_exits": 0,
        "forced_exits": 0,
        "breaker_skips": 0,
        "breaker_trips": 0,
        "breaker": None,
    }

    def day_at(idx: int) -> Optional[str]:
        if not days:
            return None
        return str(days[min(max(idx, 0), len(days) - 1)])[:10]

    def book(
        entry_bar: int,
        last_bar: int,
        exit_idx: int,
        short: bool,
        entry_px: float,
        exit_px: float,
        weight: float,
        reason: str,
        stop_pct: float,
    ) -> None:
        """Write the leg's bar returns and record it.

        ``last_bar`` is the final bar carrying a return (the leg is closed at the
        NEXT bar's open, as in ``simulate_frame``); ``exit_idx`` is the bar whose
        price ended the trade, which is what the trade log shows.
        """
        for k in range(entry_bar, last_bar + 1):
            if k >= len(returns):
                break
            den = entry_px if k == entry_bar else opens[k]
            num = exit_px if k == last_bar else opens[k + 1]
            bar = (den / num - 1.0) if short else (num / den - 1.0)
            returns[k] = weight * bar
            active[k] = True

        price_ret = (entry_px / exit_px - 1.0) if short else (exit_px / entry_px - 1.0)
        equity_ret = price_ret * weight
        legs.append(
            {
                "entry_idx": entry_bar,
                "exit_idx": exit_idx,
                "direction": "short" if short else "long",
                "entry_price": entry_px,
                "exit_price": exit_px,
                "raw_entry_price": opens[entry_bar],
                "raw_exit_price": exit_px,
                "ret": price_ret,
                "equity_ret": equity_ret,
                "weight": weight,
                "bars": max(exit_idx - entry_bar, 0),
                "reason": reason,
                "stop_percent": stop_pct,
                "skipped": False,
            }
        )
        stats[f"{reason}_exits"] = int(stats.get(f"{reason}_exits", 0)) + 1
        if breaker is not None:
            breaker.record_trade(equity_ret * 100.0, day=day_at(exit_idx))
            stats["breaker"] = breaker.snapshot()
            stats["breaker_trips"] = breaker.state.trips

    # Bar ``k`` carries the fill for the signal on bar ``k-1``.
    pos: Optional[dict] = None
    for k in range(1, n):
        act = str(signals[k - 1]) if k - 1 < len(signals) else "HOLD"

        if pos is None:
            want_short: Optional[bool] = None
            if act == "BUY":
                want_short = False
            elif act == "SELL" and allow_short:
                want_short = True
            if want_short is not None:
                if breaker is not None and breaker.tripped(day_at(k)):
                    stats["breaker_skips"] = int(stats["breaker_skips"]) + 1
                    legs.append(
                        {
                            "entry_idx": k, "exit_idx": k,
                            "direction": "short" if want_short else "long",
                            "skipped": True, "reason": BREAKER,
                            "weight": 0.0, "bars": 0, "ret": 0.0,
                        }
                    )
                else:
                    short = bool(want_short)
                    stop_pct = float(config.stop_loss_percent)
                    if config.enabled and config.sizing_mode == "volatility_target":
                        vol = realized_volatility_percent(
                            list(closes[: k + 1]), config.volatility_period
                        )
                        if vol:
                            stop_pct = vol
                    weight = (
                        target_weight(
                            config.risk_limit_percent, stop_pct, config.max_exposure_percent
                        )
                        if config.enabled
                        else 1.0
                    )
                    if stats["weight"] == 1.0:
                        stats["weight"] = weight
                    entry_px = opens[k] * (1.0 - cost) if short else opens[k] * (1.0 + cost)
                    stop_lvl, take_lvl = _levels(config, entry_px, short)
                    pos = {
                        "entry_bar": k, "short": short, "entry_px": entry_px,
                        "stop": stop_lvl, "take": take_lvl, "weight": weight,
                        "stop_pct": stop_pct,
                    }
        else:
            short = pos["short"]
            closing = (act == "BUY" and short) or (act == "SELL" and not short)
            if closing:
                raw_exit = opens[k]
                exit_px = raw_exit * (1.0 + cost) if short else raw_exit * (1.0 - cost)
                book(
                    pos["entry_bar"], k - 1, k, short, pos["entry_px"],
                    exit_px, pos["weight"], SIGNAL, pos["stop_pct"],
                )
                pos = None

        # A stop/take can fire inside any bar the position is open — including
        # the bar it was opened on.
        if pos is not None:
            hit = _level_hit(pos["short"], pos["stop"], pos["take"], highs, lows, k)
            if hit is not None:
                level, reason = hit
                exit_px = level * (1.0 + cost) if pos["short"] else level * (1.0 - cost)
                book(
                    pos["entry_bar"], k, k, pos["short"], pos["entry_px"],
                    exit_px, pos["weight"], reason, pos["stop_pct"],
                )
                pos = None

    # Still open at the end of the dataset -> force-close at the final close,
    # exactly like ``simulate_frame``.
    if pos is not None:
        short = pos["short"]
        raw_exit = closes[n - 1]
        exit_px = raw_exit * (1.0 + cost) if short else raw_exit * (1.0 - cost)
        book(
            pos["entry_bar"], n - 2, n - 1, short, pos["entry_px"],
            exit_px, pos["weight"], FORCED, pos["stop_pct"],
        )

    stats["in_position_bars"] = sum(1 for a in active if a)
    return RiskResult(
        returns=returns,
        legs=legs,
        active=active,
        in_position_bars=int(stats["in_position_bars"]),
        stats=stats,
    )


def _levels(config: RiskConfig, entry_px: float, short: bool):
    """``(stop, take)`` price levels for an entry, or ``(None, None)``."""
    stop_level = take_level = None
    if config.enabled and config.stop_loss_percent > 0:
        d = stop_distance(entry_px, config.stop_loss_percent)
        stop_level = entry_px + d if short else entry_px - d
    if config.enabled and config.take_profit_percent > 0:
        d = stop_distance(entry_px, config.take_profit_percent)
        take_level = entry_px - d if short else entry_px + d
    return stop_level, take_level


def _level_hit(short: bool, stop_level, take_level, highs, lows, k: int):
    """``(level, reason)`` when bar ``k`` touches the stop or the take.

    The **stop is checked before the take** on each bar: when both levels sit
    inside one bar's range the intrabar order is unknowable, so the pessimistic
    assumption is used (the stop filled)."""
    if (stop_level is None and take_level is None) or highs is None or lows is None:
        return None
    if k >= len(highs) or k >= len(lows):
        return None
    high, low = float(highs[k]), float(lows[k])
    if short:
        if stop_level is not None and high >= stop_level:
            return stop_level, STOP
        if take_level is not None and low <= take_level:
            return take_level, TAKE
    else:
        if stop_level is not None and low <= stop_level:
            return stop_level, STOP
        if take_level is not None and high >= take_level:
            return take_level, TAKE
    return None
