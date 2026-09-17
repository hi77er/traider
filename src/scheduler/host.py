"""Hosting the loop in this process: claim the lease, report where it stands, then tick.

``src/main.py`` is the entry point you type; this is what it does, kept apart so the
behaviour is testable without a process, an argv list or a real clock. The split is the same
one the rest of the project uses for its entry points: the host decides nothing about
trading, and everything in here is about the process rather than about the strategy.

Two responsibilities, and they are the two things that have to happen before the first tick:

**Take the lease.** Two loops mean double orders and a quiet failure, so the claim comes
before anything else — before the startup report, and before a tick. See
``src/scheduler/lease.py`` for how a holder is judged and why a crashed one needs no manual
cleanup.

**Say what it found.** A loop that has been down for a week comes back to a position it did
not know it had. The startup report reads the broker once and says so, in the log, before any
decision is made. It is a REPORT and not a gate on purpose: the tick's own reconcile is what
refuses to trade on a disagreement, and refusing to *start* would leave the operator with a
process that will not run and no way to see what is wrong. A position is never adopted here
either — adoption needs the bar that closed, which is exactly what the first tick has and
the startup does not.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from src.config.trading_state import armed_strategy, is_trading_on
from src.execution import store
from src.scheduler import lease as lease_mod
from src.scheduler import orchestrator
from src.scheduler.orchestrator import build_driver

logger = logging.getLogger(__name__)

__all__ = ["prepare", "serve"]


def prepare(settings) -> List[str]:
    """What the loop found when it started. Never raises, and never trades.

    Returns lines for the operator rather than logging them, so the caller decides where
    they go — a daemon's stdout is usually the one place nobody looks, and Phase 8 gives the
    loop a log destination for exactly that reason.
    """
    lines: List[str] = []
    lines.extend(_retire_legacy_state(settings))

    if not is_trading_on(settings):
        # The common case, and worth saying out loud: with the switch OFF nothing below
        # would mean anything, and a startup that needed credentials to start would refuse
        # to run on the many days trading is simply off.
        lines.append("trading is OFF — no order can be placed, and there is nothing to reconcile")
        return lines

    name = armed_strategy(settings)
    lines.append(f"trading is ON for {name!r} — checking against the broker")
    try:
        driver = build_driver(settings, name=name)
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop the process
        lines.append(f"could not build the strategy machine ({exc})")
        return lines

    try:
        mismatch = driver.reconcile()
    except Exception as exc:  # noqa: BLE001 - an unreachable broker is reported, not fatal
        lines.append(
            f"could not ask the broker what is held ({exc}) — the first tick will try again"
        )
        return lines

    if mismatch:
        # Loud, because this is the state that wedges the loop: the tick will refuse every
        # bar until a human reconciles it, and a silent refusal looks like a quiet market.
        logger.error("Startup reconcile: %s", mismatch)
        lines.append(f"the broker and the local state disagree — {mismatch}")
    else:
        lines.append("local state and the broker agree")
    return lines


def _retire_legacy_state(settings) -> List[str]:
    """Move any pre-Phase-3 state file out of the way, once, on startup."""
    try:
        moved = store.retire_legacy_state(settings)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never stop the loop
        logger.warning("Could not retire legacy state: %s", exc)
        return [f"could not retire legacy state ({exc})"]
    if not moved:
        return []
    where = moved[0].parent
    return [
        f"moved {len(moved)} legacy state file(s) to {where} — they are kept, never adopted"
    ]


def serve(
    settings,
    *,
    once: bool = False,
    resolve: Optional[Callable[[], Any]] = None,
    report: Optional[Callable[[List[str]], None]] = None,
    **run_kwargs,
) -> List[dict]:
    """Claim the lease, say what was found, then run the loop until it is stopped.

    The order is the point: the claim comes first, so two loops can never both get as far as
    the report, let alone a tick. The report comes before the first tick, so an operator
    reading the log sees what the loop inherited before they see what it decided.

    Returns the records it produced: one for ``once=True``, otherwise however many ticks ran
    before something stopped it. An exception on the way out propagates — the caller's exit
    code should say the process failed — but the lease is released first, so the next start
    does not have to notice that this one died.

    ``report`` receives the startup lines (a caller decides where they go: a daemon's stdout
    is the one place nobody looks, which is why Phase 8 gives the loop a log destination).
    ``run_kwargs`` goes straight to ``orchestrator.run`` — ``sleep``, ``clock``, ``driver`` —
    which is how a test drives a whole session with no clock and no waiting. Do not pass
    ``ticks``: ``once`` is how a bounded run is asked for, and two ways to say one thing is
    how the two end up disagreeing.
    """
    claim = lease_mod.acquire(settings, strategy=armed_strategy(settings))
    try:
        if report is not None:
            report(prepare(settings))
        return orchestrator.run(
            settings,
            ticks=1 if once else None,
            lease=claim,
            resolve=resolve,
            **run_kwargs,
        )
    finally:
        lease_mod.release(claim)
