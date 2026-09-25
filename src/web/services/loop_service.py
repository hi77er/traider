"""Is the loop alive? What did it last do? — the dashboard's read of it.

Three screens need these answers (the header pill, the Trading panel and the trading log), and
they must not get them from the loop: the dashboard has to survive a broken one. Everything
here is therefore read from files the loop writes — the lease, and ``latest.json`` — plus the
one rule that is genuinely shared, ``armed_strategy``.

**The distinction this module exists to draw** is between a quiet market and a dead loop, and
between a loop that stopped and one that died:

``never``
    No claim and no tick has ever been recorded. Nothing has run here yet.
``stopped``
    It ran before and nothing holds the lease now — the ordinary result of a clean shutdown,
    which releases it.
``overdue``
    A claim is on disk and NO live process is honouring it. This is the crash case, and it is
    the one worth shouting about: the file says a loop meant to act, the process table says
    otherwise, and nothing will tick again until someone restarts it.
``running``
    A live holder. The heartbeat age is then just information.
``stalled``
    A live holder that has gone quiet: it declared a wake, and the grace past that wake has
    run out with the process still there. Alive but not working — a loop wedged inside a tick
    holds its lease exactly like a healthy one, and that is the one question a pid cannot
    answer, so it is answered from the wake the loop committed to.

An age on its own cannot tell those apart, which is why the state is computed rather than the
timestamp being handed over for each caller to interpret slightly differently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.config import loop_state
from src.config.trading_state import armed_strategy, is_trading_on
from src.execution import store
from src.model import rules as rules_mod

logger = logging.getLogger(__name__)

__all__ = ["NEVER", "OVERDUE", "PROTECTED", "RUNNING", "STALLED", "STOPPED",
           "UNPROTECTED", "ensure_running", "monitored_strategy", "protection", "status"]

#: How often a read of the loop's state may try to put a crashed loop back. The state is polled
#: every few seconds by an open page, and a start attempt per poll would be a fork storm.
ENSURE_MIN_INTERVAL_SECONDS = 30

#: When the last attempt was made, in epoch seconds. In-process on purpose: a throttle on an
#: operation a poll asks for, not state anything else has to agree with.
_last_ensure = 0.0

NEVER = "never"
STOPPED = "stopped"
OVERDUE = "overdue"
RUNNING = "running"
STALLED = "stalled"

#: The verdicts on an open position. ``naked`` is NOT a failure: a strategy configured with
#: no stop gets no bracket, deliberately, and warning about it would be crying wolf — which
#: is how a real warning gets ignored. ``unprotected`` is the loud one: a level was set and
#: nothing is resting at it.
PROTECTED = "protected"
UNPROTECTED = "unprotected"
NAKED = "naked"
NO_POSITION = "none"

#: How far a resting level may sit from the level the position was opened with and still be
#: the same level. Small on purpose: this is for float noise and broker rounding, not for
#: "close enough" — a stop that moved by half a percent is a stop in the wrong place.
LEVEL_TOLERANCE = 0.003

#: How far back to look for the last refusal. Today's log only: an operator asking "why is
#: nothing happening" means now, and the log page is where history belongs.
REFUSAL_SCAN = 200


def monitored_strategy(settings) -> str:
    """Which strategy's SESSIONS the monitor is showing.

    Trading ON: the ARMED one — the log belongs to the run, and the strategy picker is locked
    while it is on, so this can only differ if the store was edited outside the UI.

    Trading OFF: the ACTIVE strategy — the one the lab is showing, and the one a switch would arm
    next. The stamp in the switch is deliberately NOT used here. It records which strategy the
    LAST run belonged to, so reading it left the monitor showing the previous strategy's day —
    its ticks, its orders, its closed trades, its roadmap — after the operator had moved on to
    another strategy. Worse, it was showing it beside the NEW strategy's charts and account,
    which is a page that cannot be read at all: half of it belongs to something else.
    """
    if is_trading_on(settings):
        return armed_strategy(settings)
    try:
        store_model = rules_mod.load_store(settings)
    except Exception as exc:  # noqa: BLE001 - a broken store must not 500 the monitor
        logger.warning("Could not read the strategy store for the monitor: %s", exc)
        return armed_strategy(settings)
    name = store_model.active
    strategy = store_model.strategies.get(name) if name else None
    if strategy is None or strategy.deleted:
        return armed_strategy(settings)
    return str(name)


def status(settings, at: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the dashboard needs to say whether a bot is running, in one read."""
    moment = at or datetime.now(timezone.utc)
    name = monitored_strategy(settings)
    claim = loop_state.read(settings)
    live = loop_state.holder(settings, moment)
    latest = store.load_latest(settings, name)

    if live is not None:
        # Alive is not the same as working: this is the case the countdown used to hide.
        state = STALLED if loop_state.is_stalled(claim, moment) else RUNNING
    elif claim is not None:
        state = OVERDUE
    elif latest is not None:
        state = STOPPED
    else:
        state = NEVER

    last_at = loop_state.parse_stamp((latest or {}).get("at"))
    return {
        "state": state,
        "strategy": name,
        "env": str(getattr(settings, "execution_env", "paper") or "paper").lower(),
        # ``claim`` is what the file says; ``holder`` is only set when something is actually
        # honouring it. A dashboard that showed the claim as "running" would show a crashed
        # loop as healthy, which is the failure this whole file is about.
        "claim": claim,
        "holder": live,
        "holder_text": loop_state.describe(claim),
        "next_wake": (claim or {}).get("next_wake"),
        "expires_at": (claim or {}).get("expires_at"),
        "late_by_seconds": loop_state.late_by_seconds(claim, moment),
        "started": (claim or {}).get("started"),
        "has_run": latest is not None,
        "last_tick": latest,
        "last_tick_at": (latest or {}).get("at"),
        "last_tick_age_seconds": (
            None if last_at is None else max(0.0, (moment - last_at).total_seconds())
        ),
        "last_action": (latest or {}).get("action"),
        "last_reason": (latest or {}).get("reason"),
        "last_refusal": _last_refusal(settings, name),
        "checked_at": moment.isoformat(),
    }


def _last_refusal(settings, name: str) -> Optional[Dict[str, Any]]:
    """The most recent refusal in TODAY's log, or ``None``.

    Read from the tick log rather than from ``latest.json`` because the last tick may well
    have been a success: an operator looking for "why did nothing happen at 14:30" wants the
    last thing that went wrong, not the last thing that went right.
    """
    try:
        rows = store.read_ticks(settings, name, limit=REFUSAL_SCAN)
    except Exception as exc:  # noqa: BLE001 - a log we cannot read is not a reason to 500
        logger.warning("Could not read the tick log for %s: %s", name, exc)
        return None
    for row in reversed(rows):
        if str(row.get("action")) == "refused":
            return {"at": row.get("at"), "reason": row.get("reason"), "bar": row.get("bar")}
    return None


# ---------------------------------------------------------------------------
# is the open position protected?
# ---------------------------------------------------------------------------
def ensure_running(
    settings,
    *,
    at: Optional[datetime] = None,
    interval: float = ENSURE_MIN_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """Put the loop back when trading is ON and nothing is running it.

    ``overdue`` is this module's name for the crash case — a claim on disk that no live process is
    honouring — and until now nothing acted on it: "nothing will tick again until someone restarts
    it" was the entire answer, which is the wrong answer while an armed account waits for a bar.

    Restoring the RUNNER is not the same decision as arming. Trading was switched on by the
    operator, the loop reads that switch itself on every tick and every slice of its sleep, and a
    loop that comes back does nothing a surviving one would not have done. So this is a restart,
    never an arming — and it is throttled, because the caller is a poll.

    Returns ``{ensured, started, state, reason, message}``. ``ensured`` is False whenever nothing
    was needed; ``reason`` says which of the three ways that happened.
    """
    global _last_ensure

    moment = at or datetime.now(timezone.utc)
    view = status(settings, moment)
    state_now = view.get("state")

    if not is_trading_on(settings):
        return {"ensured": False, "started": False, "state": state_now,
                "reason": "trading is off", "message": ""}
    if state_now == RUNNING:
        return {"ensured": False, "started": False, "state": state_now,
                "reason": "the loop is already running", "message": ""}
    if state_now == STALLED:
        # Deliberately not a restart. The lease is held by a process that IS running, so a
        # second loop is exactly the double-order this project refuses to risk — and a restart
        # is not what the pid is evidence for either: it says something is there, not what it
        # is doing. So the one caller that reads this every few seconds says the truth instead.
        return {"ensured": False, "started": False, "state": state_now,
                "reason": "the loop is alive but past the wake it committed to",
                "message": "The loop is running but has not ticked since the boundary it "
                           "declared — it is stuck inside a tick, and it has to be restarted."}

    now_ts = moment.timestamp()
    if now_ts - _last_ensure < interval:
        return {"ensured": False, "started": False, "state": state_now,
                "reason": "asked again too soon", "message": ""}
    _last_ensure = now_ts

    # Imported here, not at module scope: the two are neighbours, and a local import keeps the
    # dependency one-way even if ``loop_control`` ever needs to read this module's state.
    from src.web.services import loop_control

    result = loop_control.start(settings)
    started = bool(result.get("started"))
    if started:
        logger.warning(
            "Trading is ON but no loop was running (state=%s) — started pid=%s",
            state_now, result.get("pid"),
        )
    else:
        logger.error("Trading is ON and no loop is running, and one could not be started: %s",
                     result.get("message") or result.get("reason") or "no reason given")
    return {
        "ensured": True,
        "started": started,
        "state": state_now,
        "reason": result.get("reason") or "",
        "message": result.get("message") or "",
    }


def protection(settings, *, env: Optional[str] = None, legs: Optional[list] = None) -> Dict[str, Any]:
    """Is what is held protected, by the levels the machine opened it with?

    One position, one question, and the answer is a comparison of two facts that are stored
    in different places on purpose: the stop and target recorded ON THE POSITION when it was
    opened, and the exit legs the broker is actually resting now. The broker's copy is what
    protects anything — a local level with no order behind it protects nothing — and a
    mismatch is exactly the silent failure worth surfacing: a stop cancelled, rejected on
    amendment, or left behind by a fill that moved away from its expectation.

    Three verdicts, and the third is deliberate rather than a gap:

    ``none``
        Nothing is held, so there is nothing to protect.
    ``naked``
        A position is held and NO level was configured. Not a failure — a strategy configured
        with no stop gets no bracket — and warning about it would make the real warning
        ignorable.
    ``protected`` / ``unprotected``
        Levels were set; every one of them has a leg resting at it, or some do not.

    Reads the position rather than the strategy's configuration, which is not a shortcut: the
    levels that matter are the ones this position was SIZED for, and a configuration edited
    since then must not be able to make an unprotected position look protected.

    ``legs`` is the broker's resting exits, already fetched by the caller — one broker read
    serves the whole panel, and this function stays a comparison rather than a network call.
    """
    name = armed_strategy(settings)
    account = str(env or getattr(settings, "execution_env", "paper") or "paper").lower()
    position = (store.load_state(settings, name, account) or {}).get("position")
    if not position:
        return {"state": NO_POSITION, "message": "nothing is held", "levels": {}, "uncovered": []}

    wanted = {"stop": position.get("stop"), "take": position.get("take")}
    if wanted["stop"] is None and wanted["take"] is None:
        return {
            "state": NAKED,
            "message": (
                "this position has no stop and no target configured — it will be held until "
                "the signal turns, which is what the strategy was asked for"
            ),
            "levels": {"stop": None, "take": None},
            "uncovered": [],
            "position": position,
        }

    by_kind: Dict[str, list] = {"stop": [], "take": []}
    for kind, level in _exit_levels(legs):
        by_kind[kind].append(level)

    uncovered: list = []
    levels: Dict[str, Any] = {}
    for kind in ("stop", "take"):
        level = wanted[kind]
        if level is None:
            continue
        resting = _match(by_kind[kind], float(level))
        levels[kind] = {
            "wanted": float(level),
            "resting": by_kind[kind],
            "ok": resting is not None,
        }
        if resting is None:
            uncovered.append(kind)

    verdict = {
        "levels": levels,
        "uncovered": uncovered,
        "position": position,
    }
    if not uncovered:
        verdict.update(
            state=PROTECTED, message="every configured exit is resting at the broker"
        )
        return verdict
    detail = ", ".join(f"no {kind} at {levels[kind]['wanted']:.2f}" for kind in uncovered)
    verdict.update(
        state=UNPROTECTED, message=f"the position is NOT protected — {detail}"
    )
    return verdict


def _exit_levels(legs: Optional[list]):
    """``(kind, level)`` for each leg that is an exit, skipping anything else."""
    from src.execution.alpaca_broker import exit_leg_level

    for leg in legs or []:
        classified = exit_leg_level(leg)
        if classified is not None:
            yield classified


def _match(levels: list, wanted: float) -> Optional[float]:
    """The resting level that IS ``wanted``, or ``None``. See ``LEVEL_TOLERANCE``."""
    if not wanted:
        return None
    for level in levels:
        if abs(float(level) - float(wanted)) <= abs(float(wanted)) * LEVEL_TOLERANCE:
            return level
    return None
