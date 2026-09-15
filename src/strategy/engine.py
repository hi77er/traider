"""The strategy: one bar in, one decision out.

``StrategyEngine.step`` is the whole trading logic, and it is the ONLY implementation
of it. Fill timing, stop/take levels, sizing, the breaker, one-position-at-a-time, the
re-entry rule — all here, all pure. A driver supplies bars and resolves fills:

* the backtest loops a dataset and fills at the next bar's open (or at a level), so
  results stay conservative;
* a live run reacts to each newly closed bar and records what the broker really paid.

Semantics carried over unchanged from the replay this replaces (``risk_sim``), because
every backtest number depends on them:

* a signal on bar *s* is filled at bar *s+1*'s open — one bar of delay, because bar
  *s*'s close is only knowable at the end of bar *s*;
* one position at a time: a BUY while long is ignored, a SELL while flat opens a short
  only when ``allow_short``;
* the stop is checked BEFORE the take on every bar the position is open, including the
  bar it was opened on, and the fill is booked at the LEVEL (the intrabar order is
  unknowable, so the pessimistic assumption is used);
* after a stop-out the strategy is flat and may re-enter on the next signal;
* a tripped breaker does not delay an entry, it cancels it;
* a position still open at the end of the data is force-closed at the final close.

One thing did change, deliberately: the volatility-target sizing mode now reads the
closes of bars that have CLOSED before the entry bar, where the replay read
``closes[:k+1]`` — including the entry bar's own close, which is not knowable at that
bar's open and which a live run could never reproduce. The change is a no-op for the
fixed-risk sizing every stored strategy uses, and it removes a divergence instead of
adding one.

``Ledger`` is the other half: given the fills those steps produce, it writes the per-bar
returns and the trade rows. It is accounting, not strategy — the return arithmetic
(weight, which price, the last-bar fold) lives here once instead of in each driver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from src.risk.position_sizing import realized_volatility_percent, stop_distance, target_weight
from src.strategy.config import StrategyConfig
from src.strategy.state import Position, StrategyState

logger = logging.getLogger(__name__)

__all__ = [
    "Bar", "Intent", "Ledger", "StrategyEngine", "bar_from_row", "day_key", "day_lookup",
    "OPEN", "CLOSE", "NONE", "SKIP",
]

OPEN, CLOSE, NONE, SKIP = "open", "close", "none", "skip"
# Why a position ended — the vocabulary the trade log and the stats already use, so
# existing reports read the same.
STOP, TAKE, SIGNAL, FORCED, BREAKER = "stop", "take", "signal", "forced", "circuit_breaker"


@dataclass(frozen=True)
class Bar:
    """One candle, as the machine sees it. No pandas, no index lookup."""

    index: int
    time: Any                      # the bar's timestamp (str, Timestamp, ...)
    open: float
    close: float
    high: Optional[float] = None
    low: Optional[float] = None


def bar_from_row(index: int, time: Any, row: Any) -> Bar:
    """Build a :class:`Bar` from a pandas row (or any mapping)."""

    def get(name, default=None):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return default

    high, low = get("high"), get("low")
    return Bar(
        index=index,
        time=time,
        open=float(get("open")),
        close=float(get("close")),
        high=None if high is None else float(high),
        low=None if low is None else float(low),
    )


@dataclass
class Intent:
    """What the strategy wants done, decided — not yet filled.

    ``expected_price`` is what a SIMULATED fill uses; a live driver submits the intent
    and records the price the broker reports instead. ``level`` is set when the exit is
    a stop/take touch, so a driver can fill at the resting order's level.
    """

    action: str
    reason: str = ""
    short: bool = False
    expected_price: Optional[float] = None
    level: Optional[float] = None
    stop: Optional[float] = None
    take: Optional[float] = None
    weight: float = 1.0
    stop_pct: float = 0.0
    skipped: bool = False

    @property
    def is_entry(self) -> bool:
        return self.action == OPEN

    @property
    def is_exit(self) -> bool:
        return self.action == CLOSE


class StrategyEngine:
    """The shared machine: one instance per run, ``step`` once per bar."""

    def __init__(self, config: StrategyConfig, *, days_available: bool = True):
        self.config = config
        # A breaker needs a notion of "today". Without per-bar day keys it is skipped,
        # which is what callers that pass no days have always got.
        self.days_available = bool(days_available)
        # Closes of bars that have already CLOSED, newest last — the only price history
        # the strategy may size against.
        self._closed_closes: List[float] = []

    # -- observed history --------------------------------------------------
    def observe_close(self, close: float) -> None:
        """Record a finished bar's close (the live driver calls this per bar)."""
        self._closed_closes.append(float(close))
        keep = int(self.config.volatility_period) + 2
        if len(self._closed_closes) > keep:
            del self._closed_closes[:-keep]

    # -- the decision ------------------------------------------------------
    def step(self, state: StrategyState, bar: Bar, act: str) -> List[Intent]:
        """Decide what this bar does, given ``act`` — the PREVIOUS bar's signal.

        Returns the intents for this bar in the order they happen: a signal exit
        before a level exit, and two only when a position opened on this bar is
        stopped out by the same bar's range (a bracket order filling at once).
        """
        intents: List[Intent] = []
        if state.position is None:
            entry = self._entry_intent(state, bar, act)
            if entry is not None:
                intents.append(entry)
        else:
            exit_intent = self._exit_on_signal(state, bar, act)
            if exit_intent is not None:
                intents.append(exit_intent)

        # A stop/take can fire inside ANY bar the position is open — including the bar
        # it was opened on — so this runs whether or not a signal was acted on. It only
        # skips a position that the signal exit above has just closed.
        if state.position is not None:
            hit = self._level_hit(state.position, bar)
            if hit is not None:
                level, reason = hit
                intents.append(self._close_at(state, bar, level, reason, level=level))
        return intents

    def _entry_intent(self, state: StrategyState, bar: Bar, act: str) -> Optional[Intent]:
        """Open a position, or nothing. ``act`` is the previous bar's signal."""
        cfg = self.config
        want_short: Optional[bool] = None
        if act == "BUY":
            want_short = False
        elif act == "SELL" and cfg.allow_short:
            want_short = True
        if want_short is None:
            return None

        short = bool(want_short)
        day = day_key(bar.time)
        if cfg.uses_breaker and self.days_available and day:
            breaker = state.ensure_breaker(cfg.max_consecutive_losses, cfg.max_loss_percent)
            if breaker.tripped(day):
                state.breaker_skips += 1
                return Intent(action=SKIP, reason=BREAKER, short=short, skipped=True)

        stop_pct = float(cfg.stop_loss_percent)
        if cfg.enabled and cfg.sizing_mode == "volatility_target":
            vol = realized_volatility_percent(list(self._closed_closes), cfg.volatility_period)
            if vol:
                stop_pct = vol
        weight = (
            target_weight(cfg.risk_limit_percent, stop_pct, cfg.max_exposure_percent)
            if cfg.enabled
            else 1.0
        )
        if not state.weight_seen:
            state.first_weight = weight
            state.weight_seen = True

        entry_px = bar.open * (1.0 - cfg.cost) if short else bar.open * (1.0 + cfg.cost)
        stop_lvl, take_lvl = self.levels(entry_px, short)
        state.position = Position(
            entry_index=bar.index,
            entry_price=entry_px,
            raw_entry_price=bar.open,
            short=short,
            stop=stop_lvl,
            take=take_lvl,
            weight=weight,
            stop_pct=stop_pct,
        )
        return Intent(
            action=OPEN, reason=SIGNAL, short=short, expected_price=entry_px,
            stop=stop_lvl, take=take_lvl, weight=weight, stop_pct=stop_pct,
        )

    def _exit_on_signal(self, state: StrategyState, bar: Bar, act: str) -> Optional[Intent]:
        """Close on an opposing signal, or nothing."""
        pos = state.position
        assert pos is not None
        closing = (act == "BUY" and pos.short) or (act == "SELL" and not pos.short)
        if not closing:
            return None
        return self._close_at(state, bar, bar.open, SIGNAL)

    def _close_at(
        self, state: StrategyState, bar: Bar, price: float, reason: str, *, level=None
    ) -> Intent:
        pos = state.position
        assert pos is not None
        cfg = self.config
        exit_px = price * (1.0 + cfg.cost) if pos.short else price * (1.0 - cfg.cost)
        return Intent(
            action=CLOSE, reason=reason, short=pos.short, expected_price=exit_px,
            level=level, weight=pos.weight, stop_pct=pos.stop_pct,
        )

    def _level_hit(self, pos: Position, bar: Bar):
        """``(level, reason)`` when this bar touches the stop or the take."""
        if (pos.stop is None and pos.take is None) or bar.high is None or bar.low is None:
            return None
        if pos.short:
            if pos.stop is not None and bar.high >= pos.stop:
                return pos.stop, STOP
            if pos.take is not None and bar.low <= pos.take:
                return pos.take, TAKE
        else:
            if pos.stop is not None and bar.low <= pos.stop:
                return pos.stop, STOP
            if pos.take is not None and bar.high >= pos.take:
                return pos.take, TAKE
        return None

    # -- levels ------------------------------------------------------------
    def levels(self, entry_px: float, short: bool):
        """``(stop, take)`` price levels for an entry at ``entry_px``."""
        cfg = self.config
        stop_level = take_level = None
        if cfg.enabled and cfg.stop_loss_percent > 0:
            d = stop_distance(entry_px, cfg.stop_loss_percent)
            stop_level = entry_px + d if short else entry_px - d
        if cfg.enabled and cfg.take_profit_percent > 0:
            d = stop_distance(entry_px, cfg.take_profit_percent)
            take_level = entry_px - d if short else entry_px + d
        return stop_level, take_level

    # -- the batch replay --------------------------------------------------
    def run(
        self,
        signals: Sequence[str],
        bars: Sequence[Bar],
        *,
        days: Optional[Sequence[str]] = None,
    ) -> "Ledger":
        """Replay ``signals`` over ``bars`` — the driver the backtest uses.

        Bar ``k`` carries the fill for the signal on bar ``k-1``. The bars supply every
        price the ledger needs, so returns come from the same series the decisions were
        made on.
        """
        n = len(bars)
        if n == 0:
            return Ledger(n=0, opens=[], closes=[])
        self.days_available = bool(days)
        self._closed_closes = []
        opens = [b.open for b in bars]
        closes = [b.close for b in bars]
        ledger = Ledger(n=n, opens=opens, closes=closes)
        day_at = day_lookup(days)
        state = StrategyState()
        if self.config.uses_breaker and self.days_available:
            state.ensure_breaker(self.config.max_consecutive_losses, self.config.max_loss_percent)

        for k in range(1, n):
            # The bar that is STARTING: everything before it has closed, so that is
            # what the sizing may look at — never bar k's own close.
            self.observe_close(closes[k - 1])
            bar = bars[k]
            state.bar_index = k
            act = str(signals[k - 1]) if k - 1 < len(signals) else "HOLD"
            for intent in self.step(state, bar, act):
                self.settle(ledger, state, bar, intent, day_at=day_at)

        # Still open at the end of the data -> force-close at the final close.
        if state.position is not None:
            pos = state.position
            cfg = self.config
            raw_exit = float(closes[n - 1])
            exit_px = raw_exit * (1.0 + cfg.cost) if pos.short else raw_exit * (1.0 - cfg.cost)
            ledger.book(
                entry_bar=pos.entry_index, last_bar=n - 2, exit_idx=n - 1,
                short=pos.short, entry_px=pos.entry_price, exit_px=exit_px,
                weight=pos.weight, reason=FORCED, stop_pct=pos.stop_pct,
                day_at=day_at, state=state,
            )
            state.position = None

        ledger.finalise(state)
        return ledger

    # -- the seam to a fill ------------------------------------------------
    def settle(
        self,
        ledger: Ledger,
        state: StrategyState,
        bar: Bar,
        intent: Intent,
        *,
        day_at=None,
        exit_price: Optional[float] = None,
    ) -> None:
        """Book an intent as a fill.

        ``exit_price`` lets a live driver hand over what the broker really got; the
        simulation leaves it None and the intent's expected price is used. The
        DECISIONS are identical either way — only the price of a fill can differ, which
        is the one thing a backtest cannot promise.
        """
        if intent.action == SKIP:
            ledger.record_skip(index=bar.index, short=intent.short, reason=intent.reason)
            return
        if intent.action != CLOSE:
            return
        pos = state.position
        if pos is None:
            return
        # A signal exit fills at the NEXT bar's open, so the bar before it is the last
        # one that carries a return from this leg. A stop/take fills INSIDE its bar.
        last_bar = bar.index - 1 if intent.reason == SIGNAL else bar.index
        price = float(exit_price if exit_price is not None else intent.expected_price)
        ledger.book(
            entry_bar=pos.entry_index, last_bar=last_bar, exit_idx=bar.index,
            short=pos.short, entry_px=pos.entry_price, exit_px=price,
            weight=pos.weight, reason=intent.reason, stop_pct=pos.stop_pct,
            day_at=day_at, state=state, raw_entry_px=pos.raw_entry_price,
        )
        state.position = None


class Ledger:
    """Fills in, returns and trade rows out. No strategy decisions here."""

    def __init__(self, n: int, opens: Sequence[float], closes: Sequence[float]):
        self.n = n
        self.opens = list(opens)
        self.closes = list(closes)
        self.returns: List[float] = [0.0] * max(n - 1, 0)
        self.active: List[bool] = [False] * max(n - 1, 0)
        self.legs: List[dict] = []
        self.weight: float = 1.0
        self._state: Optional[StrategyState] = None

    # -- booking -----------------------------------------------------------
    def book(
        self,
        *,
        entry_bar: int,
        last_bar: int,
        exit_idx: int,
        short: bool,
        entry_px: float,
        exit_px: float,
        weight: float,
        reason: str,
        stop_pct: float,
        day_at=None,
        state: Optional[StrategyState] = None,
        raw_entry_px: Optional[float] = None,
    ) -> None:
        """Write the leg's bar returns and append its trade row.

        ``last_bar`` is the final bar carrying a return; ``exit_idx`` is the bar whose
        price ended the trade, which is what the trade log shows. A live run has no
        price array — the returns are a backtest construct — so the loop below simply
        writes nothing for it and the trade row carries the fill prices.
        """
        for k in range(entry_bar, last_bar + 1):
            if k < 0 or k >= len(self.returns):
                break
            den = entry_px if k == entry_bar else self.opens[k]
            num = exit_px if k == last_bar else self.opens[k + 1]
            bar_ret = (den / num - 1.0) if short else (num / den - 1.0)
            self.returns[k] = weight * bar_ret
            self.active[k] = True

        price_ret = (entry_px / exit_px - 1.0) if short else (exit_px / entry_px - 1.0)
        equity_ret = price_ret * weight
        self.legs.append(
            {
                "entry_idx": entry_bar,
                "exit_idx": exit_idx,
                "direction": "short" if short else "long",
                "entry_price": entry_px,
                "exit_price": exit_px,
                "raw_entry_price": (
                    raw_entry_px
                    if raw_entry_px is not None
                    else (self.opens[entry_bar] if entry_bar < len(self.opens) else entry_px)
                ),
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
        if weight != 1.0 and self.weight == 1.0:
            self.weight = weight
        if state is not None and state.breaker is not None and day_at is not None:
            state.breaker.record_trade(equity_ret * 100.0, day=day_at(exit_idx))

    def record_skip(self, *, index: int, short: bool, reason: str) -> None:
        """An entry the risk layer refused. Kept in the log: a veto is a decision."""
        self.legs.append(
            {
                "entry_idx": index, "exit_idx": index,
                "direction": "short" if short else "long",
                "skipped": True, "reason": reason,
                "weight": 0.0, "bars": 0, "ret": 0.0,
            }
        )

    def finalise(self, state: StrategyState) -> None:
        self._state = state

    # -- results -----------------------------------------------------------
    @property
    def trades(self) -> List[dict]:
        """Only the legs that actually traded."""
        return [leg for leg in self.legs if not leg.get("skipped")]

    @property
    def in_position_bars(self) -> int:
        return sum(1 for a in self.active if a)

    def stats(self, *, applied: bool = True) -> Dict[str, object]:
        """The counters the report has always carried."""
        counts = {STOP: 0, TAKE: 0, SIGNAL: 0, FORCED: 0}
        skips = 0
        for leg in self.legs:
            if leg.get("skipped"):
                skips += 1
                continue
            reason = leg.get("reason")
            if reason in counts:
                counts[reason] += 1
        breaker = self._state.breaker if self._state is not None else None
        return {
            "applied": bool(applied),
            "weight": self.weight,
            "stop_exits": counts[STOP],
            "take_exits": counts[TAKE],
            "signal_exits": counts[SIGNAL],
            "forced_exits": counts[FORCED],
            "breaker_skips": skips,
            "breaker_trips": int(getattr(getattr(breaker, "state", None), "trips", 0) or 0),
            "breaker": breaker.snapshot() if breaker is not None else None,
            "in_position_bars": self.in_position_bars,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def day_key(time_value: Any) -> Optional[str]:
    """``YYYY-MM-DD`` of a bar, or None when it cannot be told."""
    if time_value is None:
        return None
    text = str(time_value)
    return text[:10] if len(text) >= 10 else None


def day_lookup(days: Optional[Sequence[str]]):
    """``day_at(idx)`` from the per-bar day keys, or None when there are none."""
    if not days:
        return None

    def day_at(idx: int) -> Optional[str]:
        return str(days[min(max(idx, 0), len(days) - 1)])[:10]

    return day_at
