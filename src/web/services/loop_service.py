"""Is the loop alive? What did it last do? — the dashboard's read of it.

Three screens need these answers (the header pill, the Live panel and the trading log), and
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

An age on its own cannot tell those apart, which is why the state is computed rather than the
timestamp being handed over for each caller to interpret slightly differently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.config import loop_state
from src.config.trading_state import armed_strategy
from src.execution import store

logger = logging.getLogger(__name__)

__all__ = ["NEVER", "OVERDUE", "RUNNING", "STOPPED", "status"]

NEVER = "never"
STOPPED = "stopped"
OVERDUE = "overdue"
RUNNING = "running"

#: How far back to look for the last refusal. Today's log only: an operator asking "why is
#: nothing happening" means now, and the log page is where history belongs.
REFUSAL_SCAN = 200


def status(settings, at: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the dashboard needs to say whether a bot is running, in one read."""
    moment = at or datetime.now(timezone.utc)
    name = armed_strategy(settings)
    claim = loop_state.read(settings)
    live = loop_state.holder(settings, moment)
    latest = store.load_latest(settings, name)

    if live is not None:
        state = RUNNING
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
