"""Report analytics for a stored backtest run (pure — no web/HTTP imports).

``build_report(run)`` turns a stored run (see ``store.runs/<run_id>.json``) into
every series and table a full report page renders.

Two design rules:

* **Derived only.** Nothing here needs data that is not already in the run, so a
  report can be produced for any stored run — including ones recorded before an
  analytic was introduced.
* **Never raises on a sparse run.** A missing curve or trade list yields an empty
  section rather than an error, so a partially-populated/legacy run still renders.

The engine historically produced only the closing metrics, which is why monthly
and yearly returns, the drawdown path, the trade-return distribution and the
trade streaks had to be added before a report could show the whole picture.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - only on a broken stdlib
    ZoneInfo = None  # type: ignore[assignment]

__all__ = [
    "build_report",
    "period_returns",
    "monthly_returns",
    "yearly_returns",
    "monthly_matrix",
    "drawdown_series",
    "drawdown_stats",
    "trade_distribution",
    "trade_extras",
    "exit_breakdown",
]

MONTH_LABELS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# Trade-return histogram edges (% per round trip).
_DIST_EDGES: Sequence[float] = (
    -math.inf, -10.0, -5.0, -2.0, -1.0, 0.0, 1.0, 2.0, 5.0, 10.0, math.inf,
)
_DIST_LABELS = (
    "< -10%", "-10% … -5%", "-5% … -2%", "-2% … -1%", "-1% … 0%",
    "0% … +1%", "+1% … +2%", "+2% … +5%", "+5% … +10%", "> +10%",
)


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------
def _tz(name: Optional[str]):
    """Market timezone from the run's settings snapshot, falling back to UTC."""
    if not name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except Exception:  # noqa: BLE001 - unknown tz must not break the report
        return timezone.utc


def _as_date(value, tz) -> Optional[date]:
    """Coerce an equity-curve time key to a local date.

    ``chart_time`` yields a ``YYYY-MM-DD`` string for daily bars and a UTC unix
    timestamp (int) for intraday bars — both are handled."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=tz).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# period returns
# ---------------------------------------------------------------------------
def period_returns(
    curve: Sequence[dict], tz, key_fn: Callable[[date], str]
) -> List[dict]:
    """Compounded return per period (month/year) from an equity curve.

    A period's return is ``last equity in period / last equity of the previous
    period - 1``, with the first period measured from the curve's starting
    value (1.0). This is the standard "monthly returns" definition and matches
    what the closing equity/stats imply."""
    out: List[dict] = []
    cur_key: Optional[str] = None
    prev_eq = 1.0
    last_eq = 1.0
    bars = 0
    first_date: Optional[date] = None
    last_date: Optional[date] = None

    for pt in curve or ():
        d = _as_date(pt.get("time"), tz)
        eq = pt.get("equity")
        if d is None or not isinstance(eq, (int, float)):
            continue
        k = key_fn(d)
        if k != cur_key:
            if cur_key is not None:
                out.append(_period_row(cur_key, prev_eq, last_eq, bars, first_date, last_date))
            prev_eq = last_eq if cur_key is not None else 1.0
            cur_key = k
            bars = 0
            first_date = d
        last_eq = float(eq)
        last_date = d
        bars += 1

    if cur_key is not None:
        out.append(_period_row(cur_key, prev_eq, last_eq, bars, first_date, last_date))
    return out


def _period_row(key, start_eq, end_eq, bars, first_date, last_date) -> dict:
    ret = (end_eq / start_eq - 1.0) if start_eq else 0.0
    return {
        "key": key,
        "ret_pct": ret * 100.0,
        "equity_end": end_eq,
        "bars": bars,
        "start_date": str(first_date) if first_date else None,
        "end_date": str(last_date) if last_date else None,
    }


def monthly_returns(curve: Sequence[dict], tz) -> List[dict]:
    """One row per calendar month, oldest first."""
    rows = period_returns(curve, tz, lambda d: f"{d.year:04d}-{d.month:02d}")
    for row in rows:
        year, month = str(row["key"]).split("-")
        row["year"] = int(year)
        row["month"] = int(month)
        row["label"] = f"{MONTH_LABELS[int(month) - 1]} {year}"
    return rows


def yearly_returns(curve: Sequence[dict], tz) -> List[dict]:
    """One row per calendar year, oldest first."""
    rows = period_returns(curve, tz, lambda d: f"{d.year:04d}")
    for row in rows:
        row["year"] = int(row["key"])
    return rows


def monthly_matrix(monthly: Sequence[dict]) -> dict:
    """Year rows × month columns, the classic monthly-returns table.

    ``rows[i]["months"]`` maps a month number (1-12) to that month's return;
    months the window does not cover are absent. ``total`` is the compounded
    return of the whole year."""
    years: "OrderedDict[int, Dict[int, float]]" = OrderedDict()
    for row in monthly or ():
        years.setdefault(int(row["year"]), {})[int(row["month"])] = row["ret_pct"]

    out = []
    for year, months in years.items():
        compounded = 1.0
        for ret in months.values():
            compounded *= 1.0 + ret / 100.0
        out.append(
            {
                "year": year,
                "months": {str(m): months[m] for m in sorted(months)},
                "total_pct": (compounded - 1.0) * 100.0,
                "num_months": len(months),
            }
        )
    return {"rows": out, "month_labels": list(MONTH_LABELS)}


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------
def drawdown_series(curve: Sequence[dict]) -> List[dict]:
    """Per-point drawdown (% below the running peak) — the drawdown path."""
    peak = -math.inf
    out: List[dict] = []
    for pt in curve or ():
        eq = pt.get("equity")
        if not isinstance(eq, (int, float)):
            continue
        eq = float(eq)
        if eq > peak:
            peak = eq
        dd = ((eq / peak) - 1.0) * 100.0 if peak > 0 else 0.0
        out.append({"time": pt.get("time"), "dd_pct": dd, "peak": peak})
    return out


def drawdown_stats(series: Sequence[dict]) -> dict:
    """Worst/current/longest drawdown over the period."""
    if not series:
        return {
            "max_dd_pct": 0.0, "max_dd_time": None, "longest_bars": 0,
            "longest_end": None, "current_dd_pct": 0.0, "recovered": True,
        }

    max_dd = 0.0
    max_dd_time = None
    longest = 0
    longest_end = None
    run = 0
    for row in series:
        dd = float(row.get("dd_pct") or 0.0)
        if dd < max_dd:
            max_dd = dd
            max_dd_time = row.get("time")
        if dd < -1e-9:  # underwater
            run += 1
            if run > longest:
                longest = run
                longest_end = row.get("time")
        else:
            run = 0

    current = float(series[-1].get("dd_pct") or 0.0)
    return {
        "max_dd_pct": max_dd,
        "max_dd_time": max_dd_time,
        "longest_bars": longest,
        "longest_end": longest_end,
        "current_dd_pct": current,
        "recovered": current > -1e-9,
    }


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------
def _bucket_index(ret: float) -> int:
    for i in range(len(_DIST_EDGES) - 1):
        if ret < _DIST_EDGES[i + 1]:
            return i
    return len(_DIST_EDGES) - 2


def trade_distribution(trades: Sequence[dict]) -> dict:
    """Histogram of per-trade returns (%) plus the counts behind it."""
    counts = [0] * len(_DIST_LABELS)
    rets: List[float] = []
    for t in trades or ():
        ret = t.get("ret_pct")
        if not isinstance(ret, (int, float)):
            continue
        ret = float(ret)
        rets.append(ret)
        counts[_bucket_index(ret)] += 1

    total = len(rets) or 1
    buckets = [
        {
            "label": label,
            "count": counts[i],
            "pct": counts[i] / total * 100.0,
        }
        for i, label in enumerate(_DIST_LABELS)
    ]
    return {
        "buckets": buckets,
        "total": len(rets),
        "best_pct": max(rets) if rets else 0.0,
        "worst_pct": min(rets) if rets else 0.0,
        "median_pct": _median(rets),
        "mean_pct": (sum(rets) / len(rets)) if rets else 0.0,
        "stdev_pct": _stdev(rets),
    }


def trade_extras(trades: Sequence[dict]) -> dict:
    """Streaks, hold times and gross win/loss — the trade-quality picture."""
    seq = [t for t in trades or () if isinstance(t.get("ret_pct"), (int, float))]
    wins = [t for t in seq if float(t["ret_pct"]) > 0]
    losses = [t for t in seq if float(t["ret_pct"]) <= 0]

    max_win_streak = max_loss_streak = 0
    win_streak = loss_streak = 0
    for t in seq:
        if float(t["ret_pct"]) > 0:
            win_streak += 1
            loss_streak = 0
            max_win_streak = max(max_win_streak, win_streak)
        else:
            loss_streak += 1
            win_streak = 0
            max_loss_streak = max(max_loss_streak, loss_streak)

    def _avg_bars(group: Sequence[dict]) -> float:
        bars = [t.get("bars") for t in group if isinstance(t.get("bars"), (int, float))]
        return (sum(bars) / len(bars)) if bars else 0.0

    gross_win = sum(float(t["ret_pct"]) for t in wins)
    gross_loss = abs(sum(float(t["ret_pct"]) for t in losses))
    shorts = [t for t in seq if str(t.get("side") or "long") == "short"]

    return {
        "count": len(seq),
        "wins": len(wins),
        "losses": len(losses),
        "shorts": len(shorts),
        "longs": len(seq) - len(shorts),
        "best_pct": max((float(t["ret_pct"]) for t in seq), default=0.0),
        "worst_pct": min((float(t["ret_pct"]) for t in seq), default=0.0),
        "avg_win_pct": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_pct": (-gross_loss / len(losses)) if losses else 0.0,
        "gross_win_pct": gross_win,
        "gross_loss_pct": gross_loss,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_bars": _avg_bars(seq),
        "avg_win_bars": _avg_bars(wins),
        "avg_loss_bars": _avg_bars(losses),
        "total_bars_in_market": sum(
            int(t["bars"]) for t in seq if isinstance(t.get("bars"), (int, float))
        ),
        "first_entry": seq[0].get("entry_time") if seq else None,
        "last_exit": seq[-1].get("exit_time") if seq else None,
    }


# How a position ended, in the order the report lists them. Anything else in a
# stored run is grouped by its own label rather than dropped.
EXIT_REASON_ORDER = ("stop", "take", "signal", "forced")


def exit_breakdown(trades: Sequence[dict]) -> dict:
    """Group the round trips by how the risk layer ended them.

    ``stop`` = stopped out, ``take`` = take profit hit, ``signal`` = the opposite
    rule closed it, ``forced`` = still open when the data ran out. Derived from
    the trade rows themselves, so it also works on a run recorded before these
    reasons existed (its rows simply say ``signal``)."""
    seq = [t for t in trades or () if isinstance(t.get("ret_pct"), (int, float))]
    groups: "OrderedDict[str, List[dict]]" = OrderedDict(
        (reason, []) for reason in EXIT_REASON_ORDER
    )
    for t in seq:
        groups.setdefault(str(t.get("exit_reason") or "signal"), []).append(t)

    total = len(seq)
    rows = []
    for reason, group in groups.items():
        if not group:
            continue
        rets = [float(t["ret_pct"]) for t in group]
        wins = sum(1 for r in rets if r > 0)
        rows.append(
            {
                "reason": reason,
                "count": len(group),
                "pct": (len(group) / total * 100.0) if total else 0.0,
                "wins": wins,
                "win_rate_pct": (wins / len(group) * 100.0) if group else 0.0,
                "avg_ret_pct": (sum(rets) / len(rets)) if rets else 0.0,
                "total_ret_pct": sum(rets),
                "avg_bars": _avg(
                    [t.get("bars") for t in group if isinstance(t.get("bars"), (int, float))]
                ),
            }
        )
    return {"total": total, "rows": rows}


def _avg(values: Sequence[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def build_report(run: dict) -> dict:
    """Everything a full report page renders, from one stored run."""
    run = run or {}
    curve = list(run.get("equity_curve") or [])
    trades = list(run.get("trades") or [])
    inputs = run.get("inputs") or {}
    tz = _tz(((inputs.get("settings") or {}) or {}).get("market_timezone"))

    monthly = monthly_returns(curve, tz)
    dd = drawdown_series(curve)
    benchmark = list(run.get("benchmark_curve") or [])
    benchmark_total = (
        (float(benchmark[-1]["equity"]) - 1.0) * 100.0
        if benchmark and isinstance(benchmark[-1].get("equity"), (int, float))
        else None
    )
    metrics = run.get("metrics") or {}

    # Everything the risk layer did, in one block: the settings + outcome counts
    # the engine recorded under ``inputs.risk``, plus WHICH entries were refused
    # before reaching a broker. Nothing refuses one today — the loss limits that
    # will are deferred to the execution loop — so ``vetoed`` is normally empty
    # and ``skipped_entries`` is 0.
    risk = dict(inputs.get("risk") or {})
    risk["vetoed"] = list((run.get("risk_events") or {}).get("vetoed") or [])

    return {
        # identity
        "run_id": run.get("run_id"),
        "schema_version": run.get("schema_version"),
        "strategy": run.get("strategy"),
        "generated_at": run.get("generated_at"),
        "symbol": run.get("symbol"),
        "bar_size": run.get("bar_size"),
        "model_type": run.get("model_type"),
        "rows": run.get("rows"),
        "start": run.get("start"),
        "end": run.get("end"),
        # verdict + numbers
        "metrics": metrics,
        "gate": run.get("gate") or {},
        "notes": list(run.get("notes") or []),
        "signals": run.get("signals") or {},
        "rule_stats": run.get("rule_stats") or {},
        # What the risk layer did: how positions ended + the entries it refused.
        "exits": exit_breakdown(trades),
        "risk": risk,
        # provenance (what was tested)
        "inputs": inputs,
        # series
        "equity_curve": curve,
        "benchmark_curve": benchmark,
        "drawdown": dd,
        "trades": trades,
        # derived tables
        "monthly": monthly,
        "monthly_matrix": monthly_matrix(monthly),
        "yearly": yearly_returns(curve, tz),
        "drawdown_stats": drawdown_stats(dd),
        "distribution": trade_distribution(trades),
        "trade_stats": trade_extras(trades),
        "benchmark": {
            "curve": benchmark,
            "total_return_pct": benchmark_total,
            # How much of the strategy's return the benchmark explains.
            "excess_return_pct": (
                (metrics.get("total_return_pct") - benchmark_total)
                if benchmark_total is not None
                and isinstance(metrics.get("total_return_pct"), (int, float))
                else None
            ),
        },
    }
