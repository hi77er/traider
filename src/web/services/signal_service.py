"""Rule-based signal business logic for the Web Portal.

Evaluates the ACTIVE strategy's rules over the canonical dataset using
``src.model.simple_model`` and returns what the dashboard needs to test the
rule engine from the UI: the latest decision, per-bar decisions (for chart
markers), per-side counts, the enabled rules that were evaluated, and the
position fills the chart shades.

The fills are replayed through the SAME risk layer as the backtest
(``src.backtest.risk_sim``), so the chart's held periods, its exit markers and
its executed-signal filter always agree with the Backtest panel and the report.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from src.backtest import risk_sim
from src.backtest.engine import position_intervals
from src.config.effective import get_effective_settings
from src.data.dataset import bar_label, chart_time, load_dataset
from src.model.simple_model import RuleBasedSignalGenerator

logger = logging.getLogger(__name__)

__all__ = ["signal_payload"]


def _rule_view(rule) -> dict:
    """JSON-safe view of an enabled rule for the panel's 'enabled rules' list."""
    return {
        "side": rule.side,
        "mode": rule.mode,
        "confidence": float(rule.confidence),
        "conditions": [c.model_dump() for c in rule.conditions],
    }


def signal_payload() -> dict:
    """Evaluate the active strategy's rules over the dataset and report it.

    Rule-based generation is only valid under ``MODEL_TYPE=rule_based``; for
    any other model type the endpoint reports why no rule signals are shown.
    """
    settings = get_effective_settings()
    symbol = settings.instrument
    interval = settings.historical_bar_size

    payload: Dict[str, object] = {
        "ok": True,
        "available": False,
        "model_type": settings.model_type,
        "context": {
            "instrument": symbol,
            "bar_size": interval,
            "model_type": settings.model_type,
        },
        "enabled_rules": [],
        "latest": None,
        "series": [],
        "fills": [],
        "counts": {},
    }

    if str(settings.model_type).lower() != "rule_based":
        payload["reason"] = (
            f"Rule-based signals only run when MODEL_TYPE=rule_based "
            f"(current: {settings.model_type}). Set it in the active strategy's "
            f"Configuration or in .env, then save."
        )
        return payload

    gen = RuleBasedSignalGenerator(settings=settings)  # loads ACTIVE strategy rules
    payload["context"]["buy_threshold"] = gen.buy_threshold
    payload["context"]["sell_threshold"] = gen.sell_threshold
    payload["enabled_rules"] = [_rule_view(r) for r in gen.rules]

    df = load_dataset(settings, symbol, interval)
    if df.empty:
        payload["available"] = True
        payload["reason"] = "No dataset yet for this strategy."
        return payload
    payload["available"] = True

    out = gen.evaluate_frame(df)
    if out.empty:
        # No evaluable rows (defensive — a non-empty dataset always yields a
        # decision per bar).
        payload["available"] = True
        payload["latest"] = None
        payload["series"] = []
        payload["counts"] = {}
        return payload
    latest = gen.evaluate_latest(df)

    counts = out["signal"].value_counts().to_dict()
    payload["counts"] = {str(k): int(v) for k, v in counts.items()}
    payload["latest"] = {
        **latest.to_dict(),
        "time": bar_label(out.index[-1], interval),
    }
    payload["series"] = [
        {
            "time": chart_time(ts, interval, settings.market_timezone),
            "signal": str(row["signal"]),
            "confidence": round(float(row["confidence"]), 6),
        }
        for ts, row in out.iterrows()
    ]

    # Position fills under the one-position state machine (a fill happens at the
    # next bar's open; an open position at the end is force-closed at the final
    # close). With ALLOW_SHORT the fills track short legs too.
    #
    # These MUST come from the same risk layer the backtest replays
    # (src.backtest.risk_sim), otherwise the chart tells a different story from
    # the Backtest panel and the report: a leg the risk layer stopped out early
    # would still be shaded to its raw exit, and a circuit-breaker vetoed entry
    # would still count as an executed fill. With APPLY_RISK_LAYER=False the raw
    # strategy is shown instead, matching a backtest run with it off.
    sig = [str(v) for v in out["signal"].values]
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    nrows = len(df)
    allow_short = bool(settings.allow_short)
    tz = settings.market_timezone
    fills: List[dict] = []
    vetoed: List[dict] = []

    if bool(getattr(settings, "apply_risk_layer", True)):
        risk_result = risk_sim.apply_risk_layer(
            sig,
            opens,
            df["high"].to_numpy(dtype=float),
            df["low"].to_numpy(dtype=float),
            closes,
            nrows,
            config=risk_sim.RiskConfig.from_settings(settings, enabled=True),
            allow_short=allow_short,
            days=[str(ts)[:10] for ts in df.index],
        )
        for leg in risk_result.legs:
            e = int(leg["entry_idx"])
            side = str(leg.get("direction") or "long")
            if leg.get("skipped"):
                # The circuit breaker refused this entry — no position was ever
                # opened, so it is reported separately (never as a fill).
                vetoed.append(
                    {
                        "time": chart_time(df.index[e], interval, tz),
                        "side": side,
                        "reason": str(leg.get("reason") or "circuit_breaker"),
                    }
                )
                continue
            x = min(int(leg["exit_idx"]), nrows - 1)
            fills.append(
                {
                    "time": chart_time(df.index[e], interval, tz),
                    "kind": "open",
                    "side": side,
                    "price": round(float(leg.get("raw_entry_price") or opens[e]), 6),
                }
            )
            # The outcome of the WHOLE round trip rides on its close fill: the
            # chart colours the held-period band by it (green when the position
            # made money, red when it lost). ``equity_ret`` is the return after
            # position sizing, i.e. the actual equity change.
            equity_ret = float(leg.get("equity_ret") or 0.0)
            fills.append(
                {
                    "time": chart_time(df.index[x], interval, tz),
                    "kind": "close",
                    "side": side,
                    "price": round(float(leg.get("exit_price") or closes[x]), 6),
                    "reason": str(leg.get("reason") or "signal"),
                    "ret_pct": round(float(leg.get("ret") or 0.0) * 100.0, 3),
                    "equity_ret_pct": round(equity_ret * 100.0, 3),
                    "win": equity_ret > 0.0,
                }
            )
        stats = risk_result.stats or {}
        payload["risk"] = {
            "applied": True,
            "weight": round(float(stats.get("weight", 1.0) or 0.0), 6),
            "stop_exits": int(stats.get("stop_exits", 0) or 0),
            "take_exits": int(stats.get("take_exits", 0) or 0),
            "signal_exits": int(stats.get("signal_exits", 0) or 0),
            "forced_exits": int(stats.get("forced_exits", 0) or 0),
            "breaker_skips": int(stats.get("breaker_skips", 0) or 0),
            "breaker_trips": int(stats.get("breaker_trips", 0) or 0),
            "risk_limit_percent": float(getattr(settings, "risk_limit_percent", 0.0) or 0.0),
            "stop_loss_percent": float(getattr(settings, "stop_loss_percent", 0.0) or 0.0),
            "take_profit_percent": float(getattr(settings, "take_profit_percent", 0.0) or 0.0),
            "max_exposure_percent": float(getattr(settings, "max_exposure_percent", 100.0) or 0.0),
            "sizing_mode": str(getattr(settings, "position_sizing_mode", "fixed_risk")),
        }
    else:
        for e, x, side in position_intervals(sig, nrows, allow_short):
            entry_px = float(opens[e])
            exit_px = float(opens[x]) if x < nrows else float(closes[nrows - 1])
            if entry_px and exit_px:
                price_ret = (
                    (entry_px / exit_px - 1.0) if side == "short" else (exit_px / entry_px - 1.0)
                )
            else:
                price_ret = 0.0
            fills.append(
                {
                    "time": chart_time(df.index[e], interval, tz),
                    "kind": "open",
                    "side": side,
                    "price": round(entry_px, 6),
                }
            )
            outcome = {"ret_pct": round(price_ret * 100.0, 3), "win": price_ret > 0.0}
            if x < nrows:
                fills.append(
                    {
                        "time": chart_time(df.index[x], interval, tz),
                        "kind": "close",
                        "side": side,
                        "price": round(exit_px, 6),
                        "reason": "signal",
                        **outcome,
                    }
                )
            else:
                # Still open when the data ran out — closed at the last close.
                fills.append(
                    {
                        "time": chart_time(df.index[nrows - 1], interval, tz),
                        "kind": "close",
                        "side": side,
                        "price": round(exit_px, 6),
                        "reason": "forced",
                        **outcome,
                    }
                )
        payload["risk"] = {"applied": False}

    payload["fills"] = fills
    payload["vetoed"] = vetoed
    logger.info(
        "Signal check: %s %s | enabled rules=%d | latest=%s (%s)",
        symbol, interval, len(gen.rules), payload["latest"]["signal"], payload["latest"]["time"],
    )
    return payload
