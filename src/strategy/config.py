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

from dataclasses import dataclass, replace
from typing import Optional

from src.risk.position_sizing import DEFAULT_VOLATILITY_PERIOD, target_weight


@dataclass(frozen=True)
class StrategyConfig:
    """Everything the decision machine needs, and nothing about I/O."""

    allow_short: bool = False
    # A fired rule's confidence must clear its side's threshold, or the bar is a HOLD.
    buy_threshold: float = 0.6
    sell_threshold: float = 0.6
    # ── Risk layer ────────────────────────────────────────────────────
    # ``None`` means NOT APPLIED, for every one of these. There is no master switch:
    # what is configured is what the machine does, and what is empty it does not do.
    # The distinction is real rather than cosmetic — a 0% stop and no stop are
    # different instructions, and a 0% risk limit would size every position to
    # nothing — which is why they are ``Optional`` and not zero-defaulted.
    risk_limit_percent: Optional[float] = None
    stop_loss_percent: Optional[float] = None
    take_profit_percent: Optional[float] = None
    max_exposure_percent: float = 100.0
    max_loss_percent: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
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
    def uses_stop(self) -> bool:
        """A stop is configured. Zero is not a stop — it is a stop at the entry."""
        return self.stop_loss_percent is not None and float(self.stop_loss_percent) > 0

    @property
    def uses_take(self) -> bool:
        return self.take_profit_percent is not None and float(self.take_profit_percent) > 0

    @property
    def sizes_by_risk(self) -> bool:
        """Risk-per-trade sizing needs BOTH halves: what to risk and where the stop is.

        A risk percentage with no stop has no distance to divide by, so it cannot
        size anything — and treating it as "risk nothing" would silently stop the bot
        from trading at all. Instead it falls back to the exposure cap, which is what
        the operator asked for by leaving the stop empty.
        """
        return (
            self.risk_limit_percent is not None
            and float(self.risk_limit_percent) > 0
            and self.uses_stop
        )

    @property
    def exposure_percent(self) -> float:
        """The exposure cap in percent, always defined and clamped to 0..100."""
        value = 100.0 if self.max_exposure_percent is None else float(self.max_exposure_percent)
        return min(max(value, 0.0), 100.0)

    @property
    def exposure_fraction(self) -> float:
        """The exposure cap as a fraction of equity, always defined (default 100%)."""
        return self.exposure_percent / 100.0

    def deploy_weight_for(self, stop_pct: Optional[float]) -> float:
        """Fraction of equity one position deploys, given the stop actually in force.

        The stop is a parameter because the machine may use a volatility-derived one
        instead of the configured percentage, and the size must divide by whichever
        stop is really there. One formula (``risk.position_sizing.target_weight``)
        serves every mode: risk-per-trade when there is a risk limit and a stop, and
        otherwise the exposure cap alone.
        """
        risk = self.risk_limit_percent
        if stop_pct and risk:
            return target_weight(float(risk), float(stop_pct), self.exposure_percent)
        return self.exposure_fraction

    @property
    def deploy_weight(self) -> float:
        """The deploy weight for the CONFIGURED stop (the non-volatility modes)."""
        return self.deploy_weight_for(self.stop_loss_percent)

    @property
    def applied(self) -> bool:
        """Is ANY risk setting in effect? What a run's provenance reports."""
        return bool(
            self.uses_stop
            or self.uses_take
            or self.sizes_by_risk
            or self.exposure_fraction < 1.0
            or self.max_loss_percent is not None
            or self.max_consecutive_losses is not None
        )

    def as_dict(self) -> dict:
        """The risk settings as a run's provenance records them.

        ``applied`` is derived rather than stored: with no master switch there is
        nothing to store, and a run can only claim the risk layer was in effect if at
        least one of these settings actually was.
        """
        return {
            "applied": self.applied,
            "risk_limit_percent": self.risk_limit_percent,
            "stop_loss_percent": self.stop_loss_percent,
            "take_profit_percent": self.take_profit_percent,
            "max_exposure_percent": self.max_exposure_percent,
            "max_loss_percent": self.max_loss_percent,
            "max_consecutive_losses": self.max_consecutive_losses,
            "sizing_mode": self.sizing_mode,
        }

    def to_strategy_config(self, *, slippage: float = 0.0, commission: float = 0.0) -> "StrategyConfig":
        """This config with the given cost model — the backtest's entry point.

        The adapter that used to wrap this (``backtest.risk_sim.RiskConfig``) no longer
        needs to: a config, and the same config with costs, are the same object.
        """
        return replace(self, slippage=float(slippage), commission=float(commission))

    @classmethod
    def from_settings(
        cls,
        settings,
        *,
        slippage: float = 0.0,
        commission: float = 0.0,
    ) -> "StrategyConfig":
        """Resolve from a ``Settings``-shaped object (works with test stubs too).

        ``slippage``/``commission`` are passed in rather than read here: they are the
        simulator's cost model, and a live run deliberately passes zero so the real
        fill from the broker is what counts.

        Every risk value passes through ``_optional``: an unset field stays ``None``
        all the way to the machine, so "empty" is never laundered into a number on
        the way in.
        """
        return cls(
            allow_short=bool(getattr(settings, "allow_short", False)),
            buy_threshold=float(getattr(settings, "model_buy_threshold", 0.6) or 0.0),
            sell_threshold=float(getattr(settings, "model_sell_threshold", 0.6) or 0.0),
            risk_limit_percent=_optional(settings, "risk_limit_percent"),
            stop_loss_percent=_optional(settings, "stop_loss_percent"),
            take_profit_percent=_optional(settings, "take_profit_percent"),
            max_exposure_percent=(
                _optional(settings, "max_exposure_percent")
                if _optional(settings, "max_exposure_percent") is not None
                else 100.0
            ),
            max_loss_percent=_optional(settings, "max_loss_percent"),
            max_consecutive_losses=_optional_int(settings, "max_consecutive_losses"),
            sizing_mode=str(getattr(settings, "position_sizing_mode", "fixed_risk")),
            slippage=float(slippage),
            commission=float(commission),
        )


def _optional(settings, name: str) -> Optional[float]:
    """A risk setting as a float, or ``None`` when it is unset or empty.

    ``or 0.0`` must NOT be used here: it would turn an empty box into a very real
    instruction ("stop at the entry", "risk nothing"), which is the whole thing the
    empty-means-unset rule exists to prevent.
    """
    raw = getattr(settings, name, None)
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _optional_int(settings, name: str) -> Optional[int]:
    value = _optional(settings, name)
    return None if value is None else int(value)
