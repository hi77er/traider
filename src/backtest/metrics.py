"""Backtest performance metrics (pure functions, no pandas required).

Metrics are computed from a per-bar/per-leg strategy return series plus the
closed-trade log. The engine owns the semantics (see ``engine._simulate``);
these helpers just turn raw numbers into the report the UI and the Gate show.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple


def _weekly_key(ts) -> str:
    """Group a timestamp by ISO year+week for the worst-week check."""
    try:
        iso = ts.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except Exception:  # noqa: BLE001 - string/naive timestamps
        return str(ts)[:10]


def annualized(returns: Sequence[float], periods_per_year: float) -> Tuple[float, float]:
    """Annualized (return, vol) from simple per-period returns."""
    n = len(returns)
    if n == 0:
        return 0.0, 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    std = math.sqrt(var)
    return mean * periods_per_year, std * math.sqrt(periods_per_year) if periods_per_year else 0.0


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough drop on the equity curve (fraction, positive)."""
    peak = -math.inf
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def trade_stats(trades: Sequence[dict]) -> dict:
    """Win/loss summary over *closed* trades (each has ``ret`` as a fraction)."""
    closed = [t for t in trades if t.get("ret") is not None]
    wins = [t["ret"] for t in closed if t["ret"] > 0]
    losses = [t["ret"] for t in closed if t["ret"] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "num_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "avg_win_pct": (gross_win / len(wins) * 100.0) if wins else 0.0,
        "avg_loss_pct": (gross_loss / len(losses) * 100.0) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "best_trade_pct": (max(wins) * 100.0) if wins else 0.0,
        "worst_trade_pct": (min(losses) * 100.0) if losses else 0.0,
    }


def compute_metrics(
    returns: Sequence[float],
    times: Sequence,
    trades: Sequence[dict],
    periods_per_year: float,
    in_position_legs: int = 0,
) -> dict:
    """Full metrics report from per-leg returns, their labels and the trade log.

    ``returns[i]`` corresponds to ``times[i]`` (the bar whose open starts that
    leg). ``periods_per_year`` annualizes Sharpe/vol; ``in_position_legs`` is
    used to derive exposure.
    """
    n = len(returns)
    if n == 0:
        return {
            "periods": 0, "total_return_pct": 0.0, "annualized_return_pct": 0.0,
            "annualized_vol_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0, "num_trades": 0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "profit_factor": 0.0, "worst_week_pct": 0.0, "exposure_pct": 0.0, "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0, "years": 0.0,
        }

    equity: List[float] = [1.0]
    for r in returns:
        equity.append(equity[-1] * (1.0 + r))
    equity = equity[1:]

    ann_ret, ann_vol = annualized(returns, periods_per_year)
    years = (n / periods_per_year) if periods_per_year else 0.0
    total = equity[-1] - 1.0
    compounded_ann = ((equity[-1] ** (1.0 / years)) - 1.0) if years > 0 and equity[-1] > 0 else ann_ret
    mean = sum(returns) / n
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / n) if n > 1 else 0.0
    sharpe = (mean / std * math.sqrt(periods_per_year)) if std > 0 else 0.0

    weekly: Dict[str, float] = {}
    for r, ts in zip(returns, times):
        weekly[_weekly_key(ts)] = weekly.get(_weekly_key(ts), 0.0) + r
    worst_week = min(weekly.values()) if weekly else 0.0

    stats = trade_stats(trades)
    exposure = (in_position_legs / n * 100.0) if n else 0.0

    return {
        "periods": n,
        "years": round(years, 4),
        "total_return_pct": total * 100.0,
        "annualized_return_pct": compounded_ann * 100.0,
        "annualized_vol_pct": ann_vol * 100.0,
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": max_drawdown(equity) * 100.0,
        "worst_week_pct": worst_week * 100.0,
        "exposure_pct": round(exposure, 1),
        **stats,
    }


def gate_result(settings, metrics: dict) -> dict:
    """Compare metrics against the configured Gate thresholds.

    Returns ``pass`` plus per-check booleans and the thresholds that were used.
    """
    checks = {
        "sharpe": {"pass": metrics["sharpe"] >= settings.gate_min_sharpe, "value": metrics["sharpe"], "target": settings.gate_min_sharpe},
        "max_drawdown": {"pass": metrics["max_drawdown_pct"] <= settings.gate_max_drawdown_percent, "value": metrics["max_drawdown_pct"], "target": settings.gate_max_drawdown_percent},
        "win_rate": {"pass": metrics["win_rate_pct"] >= settings.gate_min_win_rate_percent, "value": metrics["win_rate_pct"], "target": settings.gate_min_win_rate_percent},
        "worst_week": {"pass": metrics["worst_week_pct"] >= -settings.gate_max_weekly_loss_percent, "value": metrics["worst_week_pct"], "target": settings.gate_max_weekly_loss_percent},
    }
    return {
        "pass": all(c["pass"] for c in checks.values()),
        "checks": checks,
    }
