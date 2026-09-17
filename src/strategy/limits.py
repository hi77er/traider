"""The day's loss limits: when to stop OPENING, until the exchange day turns over.

``MAX_LOSS_PERCENT`` and ``MAX_CONSECUTIVE_LOSSES`` are not trading rules — they choose no
direction, no size and no level — so they are not in the engine, and a backtest does not
apply them. They are a policy about the DAY, applied by the execution loop, and they are a
pure function of facts the caller gathers: this module reads nothing, owns nothing, and
writes nothing.

Four decisions are baked in here, and each one had an alternative that was worse:

* **The day is the exchange's day, and both limits clear when it turns over.** That reset
  is not a convenience, it is what makes a halt survivable: a halted bot takes no trades,
  so a streak that only a win could break can never be broken, and the bot would be locked
  out for good with nothing on the dashboard to clear it. The caller owns the calendar (it
  owns the clock) and passes only the current day's trades.
* **A loss is money, not price.** The tally reads ``equity_ret`` — the leg's return times
  the weight it was sized at — so a 1% adverse move on a quarter-sized position costs a
  quarter of a percent. ``ret`` is the price move on its own and ignores the size, which is
  exactly what a loss limit must not do.
* **A skipped entry is neither a loss nor a win.** It does not count toward a streak and it
  does not reset one: counting it would halt over trades that never happened, resetting on
  it would clear a halt that is still earned. A leg whose return is unknown is treated the
  same way, because unknown is not a win.
* **``MAX_LOSS_PERCENT`` is measured on the ACCOUNT, not on the realised trades**, so it
  sees a drawdown that is still open, not just one that has been booked. It refuses
  ENTRIES only: an open position keeps its stop, its take and its signal exit, because
  halting the exits would turn a loss limit into a way of holding a loser for ever.

What this cannot do is change the past. A day that breaches the limit with a position open
keeps that position until its own exit arrives.

At a DAILY bar size there is one bar per day, so a tally that only ever sees one bar can
never reach a limit: both settings are inert there, and the dashboard says so rather than
implying a promise it cannot keep.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence

from src.strategy.config import StrategyConfig

__all__ = ["day_halt_reason", "day_loss_percent", "losing_streak"]


def day_loss_percent(equity: Optional[float], last_equity: Optional[float]) -> Optional[float]:
    """The account's move today, in percent of the equity the day OPENED with.

    ``last_equity`` is the previous session's close, which is the day's opening balance —
    the only denominator that makes "down 2% today" mean what a reader assumes. ``None``
    when either number is missing and when it is zero: a percentage of nothing is not 0%,
    and reporting it as one would say "today is fine" without measuring anything.
    """
    if equity is None or last_equity is None or float(last_equity) == 0.0:
        return None
    return (float(equity) - float(last_equity)) / float(last_equity) * 100.0


def losing_streak(trades: Sequence[Mapping[str, Any]]) -> int:
    """Consecutive losing TRADES at the end of ``trades`` (oldest first, newest last).

    Walks the day's rows and counts back from the most recent one. A row that was skipped
    and a row with no measurable return are both passed over without breaking the run —
    see the module docstring for why neither is allowed to clear a halt.
    """
    streak = 0
    for row in trades:
        if row.get("skipped"):
            continue
        ret = row.get("equity_ret")
        if ret is None:
            continue
        streak = streak + 1 if float(ret) < 0.0 else 0
    return streak


def day_halt_reason(
    config: StrategyConfig,
    *,
    trades: Sequence[Mapping[str, Any]] = (),
    equity: Optional[float] = None,
    last_equity: Optional[float] = None,
    account_reason: str = "",
) -> Optional[str]:
    """Why no new entry may be opened today, or ``None`` when the limits allow one.

    ``trades`` are the trades of the CURRENT exchange day — filtered by the caller, which
    is the only code that knows the calendar. ``equity``/``last_equity`` come from the
    account being traded, and are only read when ``MAX_LOSS_PERCENT`` is set; a caller that
    has not configured that setting needs no account at all.

    Returns a sentence, not a bool: the loop writes it into the tick record, and "new
    entries are paused" without a reason is the kind of message that gets a limit switched
    off to make it stop.
    """
    reasons: List[str] = []

    loss_limit = config.max_loss_percent
    if loss_limit is not None:
        pct = day_loss_percent(equity, last_equity)
        if pct is None:
            # FAIL CLOSED. The limit is configured, so the day's loss is a number that
            # matters, and an account we cannot read is not evidence that it is zero.
            why = account_reason or "the account's equity could not be read"
            return (
                f"{why}, and MAX_LOSS_PERCENT is set — refusing new entries rather than "
                "assuming the day is unbroken"
            )
        # ``pct < 0`` as well as the threshold, so a configured 0 means "any loss at all"
        # rather than "a day that ended exactly flat".
        if pct < 0.0 and abs(pct) >= abs(float(loss_limit)):
            reasons.append(
                f"the account is down {abs(pct):.2f}% today (MAX_LOSS_PERCENT={abs(float(loss_limit)):g})"
            )

    streak_limit = config.max_consecutive_losses
    if streak_limit is not None:
        streak = losing_streak(trades)
        if streak >= int(streak_limit):
            reasons.append(
                f"{streak} losing trade{'s' if streak != 1 else ''} in a row today "
                f"(MAX_CONSECUTIVE_LOSSES={int(streak_limit)})"
            )

    if not reasons:
        return None
    return (
        "the day's loss limit is in force — "
        + "; ".join(reasons)
        + "; no new entries until the next exchange day"
    )
