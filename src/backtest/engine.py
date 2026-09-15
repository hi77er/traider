"""Backtest engine for TRAIDER (pure library — no web/HTTP imports).

Replays the ACTIVE strategy's rule-based model over a canonical OHLCV dataset
and reports Gate metrics. Design constraints:

- **Same features/data as live**: signals come from
  ``model.simple_model.RuleBasedSignalGenerator`` which computes features with
  the exact ``FeatureEngineer`` used live, so results transfer.
- **No lookahead**: a signal decided on bar *t* (at its close) is filled at the
  *next* bar's open.
- **One position at a time** (long or short): when the strategy enables short
  positions (``allow_short``), a SELL while flat opens a SHORT and a BUY
  while short closes it. A SELL while long always closes the long; repeats
  while a position is open are ignored. With shorting disabled this is the
  original long/flat behaviour — SELL is only ever an exit.
- **Costs**: slippage (price %) + commission (fraction of notional) are charged
  on each fill.
- **Same risk layer as live**: entries are sized, stopped, and halted by the
  same rules the live bot applies — one shared engine, ``src/strategy/engine.py``,
  driven from here through the ``risk_sim`` adapter (setting ``APPLY_RISK_LAYER``).
  Set it to False to get the raw strategy numbers.
- Mid-day bars are marked open-to-open between fills; an open position at the
  end of the dataset is force-closed at the final close.
- **Full fidelity**: the equity curve, the buy & hold benchmark and the trade
  log are returned *untruncated*, and the result carries an ``inputs``
  provenance block (effective settings, rules + hash, window, costs) so a run is
  reproducible and reportable. The web layer trims its own copy for the panel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import List, Optional

import pandas as pd

from src.config.settings import Settings
from src.data.dataset import bar_label, chart_time, load_dataset
from src.execution.config import execution_status
from src.model.simple_model import RuleBasedSignalGenerator

from . import metrics
from . import risk_sim
from .store import jsonable

logger = logging.getLogger(__name__)

__all__ = ["run_backtest", "simulate_frame"]


def _periods_per_year(df: pd.DataFrame, fallback: int = 252) -> int:
    """Approximate bars per year from the dataset span (for annualization)."""
    if df is None or len(df) < 2:
        return fallback
    days = (df.index.max() - df.index.min()).total_seconds() / 86400.0
    years = max(days / 365.25, 1e-9)
    ppy = int(round(len(df) / years))
    return max(ppy, 1)


def simulate_frame(
    opens,
    closes,
    signals,
    slippage: float = 0.0,
    commission: float = 0.0,
    allow_short: bool = False,
):
    """Simulate a long/flat (or long+short) strategy from prices + signals.

    ``signals`` is any index-aligned sequence of BUY/SELL/HOLD. Fills happen at
    the open of the bar AFTER the signal bar. ``allow_short=False`` keeps the
    original long/flat behaviour (SELL only ever closes a long; ignored while
    flat). With ``allow_short=True`` a SELL while flat opens a SHORT. Only ONE
    position exists at a time either way — a BUY while long and a SELL while
    short are ignored. Returns ``(returns, trades, in_position_legs,
    active_flags)`` where ``returns`` are per-leg strategy returns in dataset
    order (short legs are inverted, so a falling price makes money).
    """
    n = len(opens)
    if n < 2:
        return [], [], 0, []

    run = _raw_run(opens, closes, signals, n, allow_short=allow_short, slippage=slippage, commission=commission)
    trades = [
        {key: leg.get(key) for key in ("entry_price", "exit_price", "ret", "bars", "direction")}
        for leg in run.trades
    ]
    return run.returns, trades, sum(run.active), run.active


def _raw_run(opens, closes, signals, n: int, *, allow_short=False, slippage=0.0, commission=0.0):
    """Drive the shared strategy machine with the risk layer OFF.

    ``APPLY_RISK_LAYER=False`` is exactly this path, so the raw replay that
    :func:`simulate_frame` and :func:`position_intervals` describe is produced by
    ``src/strategy`` rather than by a second copy of the rules living here. Callers
    that only need the fill bars pass dummy prices (see ``position_intervals``).
    """
    config = risk_sim.RiskConfig(enabled=False, allow_short=allow_short)
    return risk_sim.apply_risk_layer(
        [str(s) for s in signals],
        list(opens),
        None,
        None,
        list(closes),
        n,
        config=config,
        allow_short=allow_short,
        slippage=slippage,
        commission=commission,
        days=None,
    )


# Keys that look like credentials are dropped from the settings snapshot so a
# run file is safe to share or commit into a report folder.
_SECRET_KEY_RE = re.compile(r"key|token|secret|password|credential", re.IGNORECASE)


def _dump_model(obj) -> dict:
    """``model_dump()`` for pydantic objects, ``__dict__`` for plain stubs."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:  # noqa: BLE001 - provenance must never break a run
            logger.debug("model_dump() failed", exc_info=True)
    return dict(getattr(obj, "__dict__", None) or {})


def _settings_snapshot(settings) -> dict:
    """Effective settings as JSON-safe values, minus credential-shaped keys."""
    return {
        str(k): jsonable(v)
        for k, v in _dump_model(settings).items()
        if not _SECRET_KEY_RE.search(str(k))
    }


def _execution_snapshot(settings) -> dict:
    """Where an order WOULD go for this run's strategy — paper or live.

    Recorded because paper and live results legitimately differ (paper simulates
    no slippage, fees or dividends), so a run without this stamp cannot be
    attributed to an environment later. Deliberately non-raising: an unconfigured
    credential must not stop a backtest, it must simply be recorded as such.
    Never contains a key or secret — only the environment and its base URL.
    """
    status = execution_status(settings)
    return {
        "broker": status["broker"],
        "env": status["env"],
        "live": bool(status["live"]),
        "base_url": status["base_url"],
        "configured": bool(status["ok"]),
    }


def _inputs_snapshot(
    settings, generator, df: pd.DataFrame, ppy: int, allow_short: bool, slippage: float, commission: float
) -> dict:
    """Record WHAT a run tested, so the result is attributable and reproducible.

    ``rules`` + ``rules_hash`` pin the rule set that was actually evaluated,
    ``settings`` pins the effective configuration, and ``window`` pins the data
    that was replayed (the window is also why re-running after a data refresh
    yields a different ``run_id``).
    """
    def dump(attr: str) -> List[dict]:
        return [jsonable(_dump_model(r)) for r in (getattr(generator, attr, None) or [])]

    rules, skipped = dump("rules"), dump("skipped_rules")
    blob = json.dumps(rules, sort_keys=True, separators=(",", ":"), default=str)

    return {
        "settings": _settings_snapshot(settings),
        "execution": _execution_snapshot(settings),
        "model_type": str(settings.model_type),
        "rules": rules,
        "skipped_rules": skipped,
        "rules_hash": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12],
        "allow_short": bool(allow_short),
        "costs": {"slippage": float(slippage), "commission": float(commission)},
        "window": {
            "instrument": settings.instrument,
            "bar_size": settings.historical_bar_size,
            "rows": int(len(df)),
            "start": str(df.index.min()),
            "end": str(df.index.max()),
            "periods_per_year": int(ppy),
            "backtest_start_date": getattr(settings, "backtest_start_date", None),
            "backtest_end_date": getattr(settings, "backtest_end_date", None),
        },
    }


def run_backtest(settings: Settings, dataset: Optional[pd.DataFrame] = None) -> dict:
    """Run a full backtest for ``settings`` (an *effective* settings snapshot).

    When ``dataset`` is omitted the canonical dataset for the strategy's
    instrument/bar size is loaded. Returns a JSON-ready result dict with
    ``ok=True`` on success or ``ok=False`` + ``error`` (e.g. not rule_based,
    empty dataset).
    """
    base = {
        "ok": False,
        "symbol": settings.instrument,
        "bar_size": settings.historical_bar_size,
        "model_type": settings.model_type,
        "error": None,
        "metrics": None,
        "gate": None,
        "equity_curve": [],
        "trades": [],
        "notes": [],
        "rule_stats": {"evaluated": 0, "skipped": 0},
    }

    if str(settings.model_type).lower() != "rule_based":
        base["error"] = (
            f"Backtest requires MODEL_TYPE=rule_based (current: {settings.model_type}). "
            f"Set it in the active strategy's Configuration, then re-run."
        )
        return base

    df = dataset if dataset is not None else load_dataset(settings, settings.instrument, settings.historical_bar_size)
    if df is None or df.empty:
        base["error"] = "No dataset yet for this strategy — download historical data first."
        return base
    df = df.sort_index()
    if settings.backtest_start_date:
        df = df[df.index >= pd.Timestamp(settings.backtest_start_date)]
    if settings.backtest_end_date:
        df = df[df.index <= pd.Timestamp(settings.backtest_end_date)]
    if df.empty:
        base["error"] = "Dataset is empty after applying the backtest window."
        return base

    generator = RuleBasedSignalGenerator(settings=settings)
    dec = generator.evaluate_frame(df)  # same index as df

    notes: List[str] = []
    if generator.skipped_rules:
        notes.append(f"{len(generator.skipped_rules)} rule(s) skipped (reference disabled features).")
    if not generator.rules:
        notes.append("No evaluable rules — every bar is HOLD.")
    rule_stats = {"evaluated": len(generator.rules), "skipped": len(generator.skipped_rules)}

    sig = [str(v) for v in dec["signal"].values]
    allow_short = bool(getattr(settings, "allow_short", False))
    slippage = float(settings.backtest_slippage_percent) / 100.0
    commission = float(settings.backtest_commission_per_trade)
    n = len(df)

    # The strategy's signals, replayed through the SAME risk layer the live bot
    # applies (position sizing, stop/take, circuit breaker) — see risk_sim. With
    # APPLY_RISK_LAYER=False this reproduces the raw simulate_frame() numbers,
    # so the Gate can be read either way.
    risk_config = risk_sim.RiskConfig.from_settings(
        settings, enabled=bool(getattr(settings, "apply_risk_layer", True))
    )
    risk_result = risk_sim.apply_risk_layer(
        sig,
        df["open"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        n,
        config=risk_config,
        allow_short=allow_short,
        slippage=slippage,
        commission=commission,
        days=[str(ts)[:10] for ts in df.index],
    )
    returns = risk_result.returns
    in_pos_legs = risk_result.in_position_bars

    # Per-leg labels are the leg-start bar timestamps; flat legs before the
    # first fill are included so the equity curve spans the whole window.
    leg_times = [df.index[k] for k in range(len(returns))]
    ppy = _periods_per_year(df)

    report = metrics.compute_metrics(
        returns,
        leg_times,
        risk_result.trades,
        periods_per_year=float(ppy),
        in_position_legs=in_pos_legs,
    )
    gate = metrics.gate_result(settings, report)

    inputs = _inputs_snapshot(
        settings, generator, df, ppy, allow_short, slippage, commission
    )
    # What the risk layer actually did — part of the provenance, and part of the
    # run id, so a risk-on run can never be confused with a raw one.
    inputs["risk"] = {
        **risk_config.as_dict(),
        "weight": round(float(risk_result.stats.get("weight") or 0.0), 6),
        "stop_exits": int(risk_result.stats.get("stop_exits") or 0),
        "take_exits": int(risk_result.stats.get("take_exits") or 0),
        "signal_exits": int(risk_result.stats.get("signal_exits") or 0),
        "forced_exits": int(risk_result.stats.get("forced_exits") or 0),
        "breaker_skips": int(risk_result.stats.get("breaker_skips") or 0),
        "breaker_trips": int(risk_result.stats.get("breaker_trips") or 0),
    }

    # The bars the risk layer REFUSED to trade (circuit breaker). Deliberately a
    # top-level key rather than part of ``inputs``: the inputs snapshot feeds
    # ``store.new_run_id``, so adding a field there would re-key every existing
    # run. Stops/takes need nothing extra — each trade row already carries the
    # ``exit_reason`` and the price it left at.
    risk_events = {
        "applied": bool(risk_config.enabled),
        "vetoed": [
            {
                "time": chart_time(
                    df.index[int(leg["entry_idx"])],
                    settings.historical_bar_size,
                    settings.market_timezone,
                ),
                # Human-readable stamp for the report (``time`` is the chart
                # key: a date string daily, a UTC unix int intraday).
                "label": bar_label(df.index[int(leg["entry_idx"])], settings.historical_bar_size),
                "index": int(leg["entry_idx"]),
                "side": str(leg.get("direction") or "long"),
                "reason": str(leg.get("reason") or "circuit_breaker"),
            }
            for leg in risk_result.legs
            if leg.get("skipped")
        ],
    }

    # Equity curve using the same time keys the chart uses. FULL resolution:
    # an untruncated series is what a report (or a later parameter sweep)
    # needs, while the web layer trims its own copy for the panel — see
    # ``backtest_service._ui_view``.
    eq = 1.0
    curve: List[dict] = [
        {"time": chart_time(df.index[0], settings.historical_bar_size, settings.market_timezone), "equity": 1.0}
    ]
    for k, r in enumerate(returns):
        eq *= 1.0 + r
        curve.append({"time": chart_time(df.index[k], settings.historical_bar_size, settings.market_timezone), "equity": round(eq, 6)})
    if curve[-1]["equity"] != round(eq, 6):
        curve.append({"time": chart_time(df.index[-1], settings.historical_bar_size, settings.market_timezone), "equity": round(eq, 6)})

    # Buy & hold benchmark over the SAME window and time keys, so the report can
    # answer "did the strategy beat simply holding the instrument?". Point k
    # mirrors equity point k (both are the close of the bar whose leg produced
    # it), so the two curves are directly comparable — and so the report can
    # compute the excess return without needing the price series again.
    closes = df["close"].to_numpy(dtype=float)
    first_close = float(closes[0]) if len(closes) else 0.0
    benchmark: List[dict] = []
    if first_close:
        benchmark = [{"time": curve[0]["time"], "equity": 1.0}]
        for k in range(min(len(returns), len(closes))):
            benchmark.append(
                {
                    "time": curve[k + 1]["time"],
                    "equity": round(float(closes[k]) / first_close, 6),
                }
            )

    # Trade log timestamps (index into the dataset). Full list — the web layer
    # trims its own copy for the panel. A stop/take shortens its leg and is
    # reported with the reason it exited.
    nrows = len(df)
    trade_rows = []
    for i, leg in enumerate(risk_result.trades):
        trade_rows.append(
            {
                "i": i + 1,
                "side": leg["direction"],
                "entry_time": str(df.index[leg["entry_idx"]]),
                "exit_time": str(df.index[min(int(leg["exit_idx"]), nrows - 1)]),
                "entry_price": round(float(leg["entry_price"]), 4),
                "exit_price": round(float(leg["exit_price"]), 4),
                "ret_pct": round(float(leg["ret"]) * 100.0, 3),
                "equity_ret_pct": round(float(leg["equity_ret"]) * 100.0, 3),
                "bars": int(leg["bars"]),
                "exit_reason": leg["reason"],
            }
        )

    notes.append(f"{ppy} bars/year used for annualization.")
    notes.append(
        (
            f"risk layer: {risk_config.risk_limit_percent:g}% risk/trade behind a "
            f"{risk_config.stop_loss_percent:g}% stop, {risk_config.take_profit_percent:g}% take, "
            f"max exposure {risk_config.max_exposure_percent:g}%"
            + (" + circuit breaker" if risk_config.circuit_breaker_enabled else "")
        )
        if risk_config.enabled
        else "risk layer NOT applied — raw strategy results."
    )

    return {
        **base,
        "ok": True,
        "error": None,
        "symbol": settings.instrument,
        "bar_size": settings.historical_bar_size,
        "model_type": settings.model_type,
        "inputs": inputs,
        "rows": int(len(df)),
        "start": str(df.index.min()),
        "end": str(df.index.max()),
        "signals": {str(k): int(v) for k, v in dec["signal"].value_counts().items()},
        "rule_stats": rule_stats,
        "risk_events": risk_events,
        "metrics": report,
        "gate": gate,
        "equity_curve": curve,
        "benchmark_curve": benchmark,
        "trades": trade_rows,
        "notes": notes,
    }


def position_intervals(sig, n: int, allow_short: bool = False) -> List[tuple]:
    """Map BUY/SELL/HOLD decisions to ``(entry_idx, exit_idx, direction)``.

    ``exit_idx == n`` means the position is still open at the end of the
    dataset and is force-closed at the final close. A single position is open
    at any time: BUY opens a long when flat and closes a short; SELL closes a
    long when long and — only when ``allow_short`` — opens a short when flat.

    Derived from the shared strategy machine (raw mode: no sizing, stops or
    breaker), so the "what opens and closes a position" rule has one home. Dummy
    prices are used because in raw mode nothing but the signals can open or close a
    leg; only the fill bars are asked for here.
    """
    if n < 2:
        return []
    flat = [1.0] * n
    run = _raw_run(flat, flat, sig, n, allow_short=allow_short)
    out: List[tuple] = []
    for leg in run.legs:
        if leg.get("skipped"):
            continue
        still_open = leg.get("reason") == "forced"
        out.append(
            (
                int(leg["entry_idx"]),
                n if still_open else int(leg["exit_idx"]),
                "short" if leg.get("direction") == "short" else "long",
            )
        )
    return out


def intervals_for(sig, n: int, allow_short: bool = False) -> List[tuple]:
    """``(entry_idx, exit_idx)`` pairs only (direction dropped).

    Mirrors :func:`position_intervals` for callers that only need the fill bar
    indices.
    """
    return [(e, x) for e, x, _d in position_intervals(sig, n, allow_short)]
