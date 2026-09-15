"""Rule-based signal generation (``MODEL_TYPE=rule_based``).

Turns a strategy's rule set (schema in ``src/model/rules.py``) plus a candle /
feature frame into ``BUY`` / ``SELL`` / ``HOLD`` decisions with a confidence.
The same computation runs in backtest and live, so results transfer.

Evaluation semantics
--------------------
* A condition compares a feature/series against a number (``value``) or
  another series (``ref``). A missing or non-finite value makes the condition
  FALSE — a bad/warmup row never fabricates a signal, and there is no
  lookahead (each bar only uses data up to itself).
* ``crosses_above`` / ``crosses_below`` need the previous bar: the signed
  difference ``feature - target`` flipping from ``<= 0`` to ``> 0`` (above) or
  from ``>= 0`` to ``< 0`` (below).
* A rule fires when ALL its conditions hold (``mode="all"``) or ANY one holds
  (``mode="any"``). Disabled rules never fire.
* Fired rules produce candidates ``(side, confidence)``. The side with the
  HIGHEST confidence wins; a tie between BUY and SELL is a HOLD (conflict).
* The winning side's confidence must clear its threshold
  (``MODEL_BUY_THRESHOLD`` / ``MODEL_SELL_THRESHOLD``), otherwise the signal
  is suppressed to HOLD.

The pure helpers here take plain row dicts (``name -> float``) so they are
trivially unit-testable; :class:`RuleBasedSignalGenerator` wires them to
candles + features (via ``FeatureEngineer``) and to the strategy store.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.config import session
from src.config.effective import get_effective_settings, resolve_effective
from src.config.settings import Settings
from src.data import dataset
from src.features.engineering import FeatureEngineer
from src.features.schema import allowed_series
from src.model import rules as rules_mod

logger = logging.getLogger(__name__)

__all__ = [
    "Signal",
    "condition_holds",
    "rule_fires",
    "fired_candidates",
    "resolve_signal",
    "decide_row",
    "resolve_ruleset",
    "RuleBasedSignalGenerator",
]

HOLD = "HOLD"
_TIE_TOL = 1e-9


@dataclass(frozen=True)
class Signal:
    """One model decision: a side (or HOLD) plus a confidence and a reason."""

    signal: str = HOLD
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "signal": self.signal,
            "confidence": round(float(self.confidence), 6),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Pure evaluation helpers (row dicts in, decisions out)
# ---------------------------------------------------------------------------


def _finite(row: Optional[Mapping[str, float]], name: str) -> Optional[float]:
    """Numeric value of ``name`` in ``row``, or None when missing/non-finite."""
    if row is None:
        return None
    val = row.get(name)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def condition_holds(
    cond: rules_mod.RuleCondition,
    cur: Mapping[str, float],
    prev: Optional[Mapping[str, float]] = None,
) -> bool:
    """True when ``cond`` holds on the current (and, for crosses, previous) row.

    Any missing / non-finite operand makes the condition FALSE.
    """
    a = _finite(cur, cond.feature)
    if a is None:
        return False

    if cond.ref is not None:
        b = _finite(cur, cond.ref)
    else:
        b = cond.value if cond.value is not None else None
    if b is None:
        return False

    op = cond.op
    if op in ("crosses_above", "crosses_below"):
        pa = _finite(prev, cond.feature) if prev is not None else None
        if cond.ref is not None:
            pb = _finite(prev, cond.ref) if prev is not None else None
        else:
            pb = cond.value if cond.value is not None else None
        if pa is None or pb is None:
            return False
        d_prev = pa - pb
        d_cur = a - b
        if op == "crosses_above":
            return d_prev <= 0.0 and d_cur > 0.0
        return d_prev >= 0.0 and d_cur < 0.0

    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    logger.warning("Unknown comparison op %r — treating condition as false", op)
    return False


def rule_fires(
    rule: rules_mod.Rule,
    cur: Mapping[str, float],
    prev: Optional[Mapping[str, float]] = None,
) -> bool:
    """True when an ENABLED rule's conditions are met on this bar."""
    if not rule.enabled or not rule.conditions:
        return False
    results = [condition_holds(c, cur, prev) for c in rule.conditions]
    return all(results) if rule.mode == "all" else any(results)


def fired_candidates(
    rules: Sequence[rules_mod.Rule],
    cur: Mapping[str, float],
    prev: Optional[Mapping[str, float]] = None,
) -> List[Tuple[str, float]]:
    """``(side, confidence)`` for every rule that fires on this bar."""
    return [
        (rule.side, float(rule.confidence))
        for rule in rules
        if rule.enabled and rule_fires(rule, cur, prev)
    ]


def resolve_signal(
    candidates: Sequence[Tuple[str, float]],
    buy_threshold: float = 0.6,
    sell_threshold: float = 0.6,
) -> Signal:
    """Turn fired candidates into one gated decision.

    The side with the highest confidence wins; an equal BUY/SELL top is a
    HOLD (conflict). The winner must clear its side's threshold or the signal
    is suppressed to HOLD.
    """
    buy_conf = max((c for s, c in candidates if s == "BUY"), default=None)
    sell_conf = max((c for s, c in candidates if s == "SELL"), default=None)

    if buy_conf is None and sell_conf is None:
        return Signal(HOLD, 0.0, "no rule fired")

    if buy_conf is not None and sell_conf is not None and math.isclose(
        buy_conf, sell_conf, rel_tol=_TIE_TOL, abs_tol=_TIE_TOL
    ):
        return Signal(HOLD, float(buy_conf), "conflicting BUY and SELL at equal confidence")

    if buy_conf is not None and (sell_conf is None or buy_conf > sell_conf):
        side, conf, threshold = "BUY", buy_conf, buy_threshold
    else:
        side, conf, threshold = "SELL", sell_conf, sell_threshold

    if conf < threshold:
        return Signal(
            HOLD,
            float(conf),
            f"{side} suppressed: confidence {conf:.3f} below threshold {threshold:.3f}",
        )
    return Signal(side, float(conf), f"{side} by rule(s) at confidence {conf:.3f}")


def decide_row(
    rules: Sequence[rules_mod.Rule],
    cur: Mapping[str, float],
    prev: Optional[Mapping[str, float]] = None,
    buy_threshold: float = 0.6,
    sell_threshold: float = 0.6,
) -> Signal:
    """Evaluate every rule against one row and return the gated decision."""
    return resolve_signal(
        fired_candidates(rules, cur, prev),
        buy_threshold=buy_threshold,
        sell_threshold=sell_threshold,
    )


# ---------------------------------------------------------------------------
# Strategy store -> rules
# ---------------------------------------------------------------------------


def resolve_ruleset(
    settings: Settings,
    strategy_name: Optional[str] = None,
) -> Optional[rules_mod.RuleSet]:
    """The named strategy's RuleSet, or the ACTIVE one when no name is given.

    Returns None when the store is missing, the name is unknown, or the
    strategy is soft-deleted (mirrors ``normalize_active`` / the portal).
    """
    store = rules_mod.load_store(settings)
    name = strategy_name or store.active
    if not name:
        return None
    rs = store.strategies.get(name)
    if rs is None or rs.deleted:
        return None
    return rs


# ---------------------------------------------------------------------------
# Generator wiring candles + features to the pure evaluator
# ---------------------------------------------------------------------------


def _row_context(crow: pd.Series, frow: Optional[pd.Series]) -> Dict[str, float]:
    """Merge one candle's finite raw fields + its feature row into a dict.

    Raw series (open/high/low/close/volume) and engineered feature columns
    share one namespace so a rule may compare ``close`` to ``sma_50`` etc.
    Non-finite values are omitted (the evaluator treats them as FALSE).
    """
    out: Dict[str, float] = {}
    for name, val in crow.items():
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if np.isfinite(f):
            out[str(name).lower()] = f
    if frow is not None:
        for name, val in frow.items():
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            if np.isfinite(f):
                out[name] = f
    return out


class RuleBasedSignalGenerator:
    """Evaluate a strategy's rules against candles, identically in live + backtest.

    ``evaluate_frame`` returns one decision per candle (for the backtester);
    ``evaluate_latest`` is the live path and simply takes the last row of the
    same computation — mirroring ``FeatureEngineer.compute_frame/compute_latest``.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        rules: Optional[Sequence[rules_mod.Rule]] = None,
    ) -> None:
        self.settings = settings or get_effective_settings()
        # This generator implements MODEL_TYPE=rule_based — it must never run
        # under a different model type.
        if str(self.settings.model_type).lower() != "rule_based":
            raise ValueError(
                f"Rule-based signals require MODEL_TYPE=rule_based "
                f"(current model_type={self.settings.model_type!r})"
            )
        if rules is None:
            rs = resolve_ruleset(self.settings)
            rules = list(rs.rules) if rs else []
        enabled = [r for r in rules if r.enabled]
        # A rule that references a series the active config does NOT produce
        # (e.g. sma_50 while FEATURE_SMA_ENABLED=False) is skipped entirely:
        # it is never evaluated and can never fire.
        available = set(allowed_series(self.settings))
        self.rules = [r for r in enabled if self._supported_by(r, available)]
        self.skipped_rules = [r for r in enabled if not self._supported_by(r, available)]
        self.buy_threshold = float(self.settings.model_buy_threshold)
        self.sell_threshold = float(self.settings.model_sell_threshold)
        if self.skipped_rules:
            logger.warning(
                "Skipped %d rule(s) referencing disabled features: %s",
                len(self.skipped_rules),
                [r.side for r in self.skipped_rules],
            )
        if not self.rules:
            logger.info("Rule-based model has no evaluable rules — always HOLD")

    @staticmethod
    def _supported_by(rule: rules_mod.Rule, available: set) -> bool:
        """True when every series the rule references is produced by config."""
        return all(
            cond.feature in available and (cond.ref is None or cond.ref in available)
            for cond in rule.conditions
        )

    @classmethod
    def for_strategy(
        cls,
        strategy_name: Optional[str] = None,
        settings: Optional[Settings] = None,
    ) -> "RuleBasedSignalGenerator":
        """Build a generator in the context of a named (or active) strategy.

        Resolves the effective settings for that strategy (thresholds, feature
        config) and loads its rules from the strategy store.
        """
        ctx = resolve_effective(strategy_name) if strategy_name else (settings or get_effective_settings())
        rs = resolve_ruleset(ctx, strategy_name)
        return cls(settings=ctx, rules=list(rs.rules) if rs else [])

    # -- decision surface ---------------------------------------------------
    def decide(self, cur: Mapping[str, float], prev: Optional[Mapping[str, float]] = None) -> Signal:
        """Evaluate the configured rules against merged row dicts."""
        return decide_row(
            self.rules,
            cur,
            prev,
            buy_threshold=self.buy_threshold,
            sell_threshold=self.sell_threshold,
        )

    def evaluate_frame(self, candles: pd.DataFrame) -> pd.DataFrame:
        """One ``{signal, confidence, reason}`` row per candle (chronological).

        Shares the exact feature computation used live, so backtest results
        transfer. Bars inside feature warmup have NaN features and therefore
        evaluate to HOLD.

        A bar OUTSIDE the trading window is a HOLD too, whatever the rules say: the
        window is when an order may be placed, so a pre-market, after-hours or
        overnight bar must not produce a decision. This is the single place the
        window is applied to decisions, so the backtest, the chart's signals and a
        live tick all read the same series — the features are still computed for
        every bar, only the DECISION is suppressed.
        """
        columns = ["signal", "confidence", "reason"]
        if candles is None or candles.empty:
            return pd.DataFrame(columns=columns)
        # With zero rules every bar is a HOLD ("no rule fired") — still emit a
        # full series so callers/UI can render an all-HOLD result cleanly.
        candles = candles.sort_index()
        feats = FeatureEngineer(self.settings).compute_frame(candles)
        outside = {
            ts for ts in candles.index if not dataset.bar_in_trading_window(self.settings, ts)
        }
        if outside:
            logger.info(
                "%d of %d bar(s) are outside the trading window (%s) — HOLD on those",
                len(outside), len(candles), session.describe(self.settings),
            )
        closed = f"outside the trading window ({session.describe(self.settings)})"
        records: List[Tuple[str, float, str]] = []
        prev: Optional[Dict[str, float]] = None
        for ts, crow in candles.iterrows():
            frow = feats.loc[ts] if ts in feats.index else None
            cur = _row_context(crow, frow)
            if ts in outside:
                sig = Signal(HOLD, 0.0, closed)
            else:
                sig = decide_row(
                    self.rules,
                    cur,
                    prev,
                    buy_threshold=self.buy_threshold,
                    sell_threshold=self.sell_threshold,
                )
            records.append((sig.signal, sig.confidence, sig.reason))
            # ``prev`` advances on every bar, in or out of the window: a rule that
            # compares with the previous row (a cross) must see the real previous
            # bar, not the last one it was allowed to decide on.
            prev = cur
        return pd.DataFrame(records, index=candles.index, columns=columns)

    def evaluate_latest(self, candles: pd.DataFrame) -> Signal:
        """Live decision for the most recent bar of ``candles``."""
        frame = self.evaluate_frame(candles)
        if frame.empty:
            return Signal(HOLD, 0.0, "no data")
        last = frame.iloc[-1]
        return Signal(
            signal=str(last["signal"]),
            confidence=float(last["confidence"]),
            reason=str(last["reason"]),
        )
