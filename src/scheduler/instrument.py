"""The instrument automation gate: may this tick change WHAT the strategy trades?

The decision is a comparison between the strategy's instrument and the screener list the criteria
picked (``src.data.instrument_automation``), taken at the top of the tick — before the dataset is
synced and before anything is decided, because everything below trades the instrument that comes
out of here.

**A switch ENDS the tick.** Every step after this one was computed for the instrument the tick
found, so trading it after changing the instrument would trade the wrong symbol for one bar. The
next boundary reads the store again (``orchestrator.run``'s ``resolve``) and trades the new one.

**History comes first, and the switch is abandoned without it.** A strategy pointed at an
instrument that has no dataset refuses every bar from then on ("the dataset has no bars up to the
last closed bar — backfill first"), so a switch that could not fetch history would trade a working
instrument for a stuck one. That is the one failure mode this feature can introduce on its own.

**Nothing here raises.** A screener outage, a broker that cannot be read, a store that cannot be
written: each is a reason the instrument is left alone, reported in the tick's notes. Automation
that can stop trading by failing is worse than no automation.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.config import automation as automation_mod
from src.config import session as session_mod
from src.data import instrument_automation as list_mod
from src.data.historical import fetch_candles
from src.execution import positions, store
from src.model import rules as rules_mod
logger = logging.getLogger(__name__)

__all__ = ["INSTRUMENT_KEY", "apply", "evaluate", "last_switch_today"]

#: The key the strategy's instrument is stored under (``rules_service`` reads the same one).
INSTRUMENT_KEY = "INSTRUMENT"

#: How far back a day's tick log is scanned for a switch. A day has one row per bar, so this
#: covers any bar size this project trades and stops a pathological log from being read whole.
LOG_SCAN = 600


def _instrument(settings) -> str:
    return str(getattr(settings, "instrument", "") or "").strip().upper()


def last_switch_today(settings, strategy: str, at) -> Optional[Dict[str, Any]]:
    """Today's switch, if there was one.

    Read from the day's own tick log rather than from a counter: the loop is the only writer of
    that log, so the once-a-day guard cannot disagree with what the log says happened.
    """
    try:
        rows = store.read_ticks(settings, strategy, when=at, limit=LOG_SCAN)
    except Exception:  # noqa: BLE001 - an unreadable log is not a reason to switch twice
        logger.exception("Could not read today's ticks for the switch guard")
        return None
    for row in reversed(rows):
        if str(row.get("action")) == "switched":
            return row
    return None


def _held_anything(settings, env: str) -> Optional[str]:
    """Why a switch would be unsafe with what is open, or ``None`` when nothing is.

    The account being TRADED, not both: the strategy is pointed at one env, and a position the
    other account holds belongs to a run this switch cannot touch. An account that cannot be
    read refuses the switch — unknown is not flat.
    """
    try:
        state = positions.snapshot(settings, env)
    except Exception as exc:  # noqa: BLE001 - a broker outage refuses the switch, nothing else
        logger.warning("Could not read the %s account for the switch guard: %s", env, exc)
        return f"the {env} account could not be read ({exc})"
    if not state.known:
        return f"the {env} account could not be read, so what it holds is unknown"
    if state.payloads:
        return f"{env} holds {positions.describe([state])}"
    return None


def evaluate(
    settings,
    strategy: str,
    at: Optional[datetime] = None,
    *,
    refresh: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """What this tick should do about the instrument. Never raises.

    Returns ``{active, switch, target, reason, skip, notes, list_at, session}``. ``active`` is
    "automation is ON" and ``switch`` is the verdict — both false when it is off, so the caller can
    tell an OFF automation from an ON one that decided against a switch. ``session`` is the session
    the list judged here belongs to, which is how a switch reports the session it acted on.
    """
    answer: Dict[str, Any] = {
        "active": False, "switch": False, "target": None, "reason": "", "skip": "", "notes": [],
    }
    try:
        automation = automation_mod.read(settings, strategy)
        if not automation.on:
            return answer
        answer["active"] = True

        # ONLY INSIDE THE SESSION, and this is the gate the screener's own data forces. The list is
        # the last COMPLETED regular session's numbers — measured against the provider: a
        # pre-market screen returns the previous close's change %, its volume and its
        # ``regularMarketTime``, with the pre-market move nowhere in them. Judging it before the
        # bell therefore spends the day's one switch on yesterday's ranking, and judging it after
        # the close spends it on a session that has ended. Screening is skipped for the same
        # reason and one more: nothing can be judged, so a provider call out of hours buys
        # nothing. The first tick inside the window screens a new list and judges THAT.
        if not session_mod.is_open_at(settings, at):
            answer["skip"] = (
                f"outside the {session_mod.describe(settings)} session, "
                "where the list is screened and judged"
            )
            return answer

        if automation.once_per_day and last_switch_today(settings, strategy, at):
            answer["skip"] = "one switch a day is the limit and today's has been made"
            return answer

        screened = (refresh or list_mod.cache_and_screen)(
            settings, strategy, automation, at=at
        )
        rows = screened.get("rows") or []
        answer["list_at"] = screened.get("at")
        answer["session"] = screened.get("session")
        if screened.get("error"):
            answer["notes"].append(f"the screener could not be read ({screened['error']})")
        if not rows:
            answer["skip"] = "no screened list to judge the criteria against"
            return answer
        if screened.get("text"):
            answer["notes"].append(f"the list: {screened['text']}")

        current = _instrument(settings)
        verdict = list_mod.decide(automation, current, rows)
        answer.update({k: verdict.get(k) for k in ("switch", "target", "reason") if k in verdict})
        answer["skip"] = verdict.get("skip") or ""
        if not verdict.get("switch"):
            return answer

        # The expensive, stateful checks last: they are only worth asking when a switch is
        # actually pending, and they are the ones that can REFUSE one.
        if automation.only_when_flat:
            env = str(getattr(settings, "execution_env", "paper") or "paper").lower()
            held = _held_anything(settings, env)
            if held:
                answer.update({"switch": False, "target": None,
                               "skip": f"waiting until nothing is held — {held}"})
        return answer
    except Exception as exc:  # noqa: BLE001 - automation must not be able to stop the loop
        logger.exception("The instrument automation could not be evaluated")
        answer["skip"] = f"the automation could not be evaluated ({exc})"
        return answer


def apply(
    settings,
    strategy: str,
    target: str,
    *,
    backfill: Optional[Callable[[Any, str], int]] = None,
) -> Dict[str, Any]:
    """Make the switch real: history for the new instrument, then the strategy's instrument.

    Returns ``{ok, from, to, rows, reason}``. ``ok`` false means the instrument is UNCHANGED and
    the reason is worth a line in the tick's notes — history that could not be fetched leaves
    the strategy on the instrument it can actually trade.
    """
    symbol = str(target or "").strip().upper()
    if not symbol:
        return {"ok": False, "reason": "no instrument was named"}
    from_symbol = _instrument(settings)

    try:
        rows = (backfill or _backfill)(settings, symbol)
    except Exception as exc:  # noqa: BLE001 - a provider outage abandons the switch
        logger.warning("Could not fetch history for %s: %s", symbol, exc)
        return {"ok": False, "reason": f"its history could not be fetched ({exc})"}

    try:
        rules_mod.set_strategy_config(settings, strategy, {INSTRUMENT_KEY: symbol})
    except Exception as exc:  # noqa: BLE001 - a locked or unreadable store abandons the switch
        logger.exception("Could not write the instrument into %s", strategy)
        return {"ok": False, "reason": f"the strategy could not be written ({exc})"}

    logger.info("Instrument automation switched %s: %s -> %s", strategy, from_symbol, symbol)
    return {"ok": True, "from": from_symbol, "to": symbol, "rows": int(rows or 0), "reason": ""}


def _backfill(settings, symbol: str) -> int:
    """The configured history for one instrument, merged into its dataset."""
    frame = fetch_candles(settings, symbol=symbol, persist=True)
    return int(len(frame)) if frame is not None else 0
