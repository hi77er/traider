"""The Instrument Automation panel's data.

Reading, mostly: the criteria, the cached Top-10, when it was screened, and — the point of the
panel — **what the tick would decide right now**. The preview is deliberately the SHARED decision
(``src.data.instrument_automation.decide``, the same call the tick makes) rather than a second
implementation in the panel: a screen that says "this would switch" while the loop says otherwise
is worse than no screen at all.

Two writes, and neither is a trading decision: the criteria (the toggle and the filters — they
have to be editable while a loop is trading, which is the whole reason they are not part of the
frozen strategy config) and a manual list refresh.

No broker call happens here. The panel's page is the Strategy lab, which asks the account nothing;
the "only while nothing is held" guard is part of the tick's judgement, and the panel says which
criteria are in force rather than re-deriving them from an account it does not read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import automation as automation_mod
from src.config import session as session_mod
from src.config.effective import active_strategy_name
from src.data import instrument_automation as list_mod

logger = logging.getLogger(__name__)

__all__ = ["payload", "refresh", "save"]

#: How many rows the panel shows. The list is the criteria's own ``size``; this only bounds what a
#: hand-edited file could put on the page.
MAX_ROWS = 50


def _strategy(settings) -> str:
    return str(active_strategy_name() or getattr(settings, "instrument", "strategy"))


def _preview(settings, strategy: str, automation: automation_mod.Automation,
             cached: Optional[Dict[str, Any]], in_session: bool) -> Dict[str, Any]:
    """What the tick would decide with the list on the panel — no screener, no broker.

    Including the session gate, because the tick has one and a preview that ignored it would
    promise a switch the loop would not make: outside the session the list is neither screened
    nor judged, whatever it says.
    """
    rows = (cached or {}).get("rows") or []
    current = str(getattr(settings, "instrument", "") or "").strip().upper()
    if not automation.on:
        return {"active": False, "switch": False, "skip": "instrument automation is off"}
    if not in_session:
        return {"active": True, "switch": False,
                "skip": f"outside the {session_mod.describe(settings)} session, "
                        "where the list is screened and judged"}
    if not rows:
        return {"active": True, "switch": False, "skip": "no list has been screened yet"}
    verdict = list_mod.decide(automation, current, rows)
    return {
        "active": True,
        "switch": bool(verdict.get("switch")),
        "target": verdict.get("target"),
        "reason": verdict.get("reason") or "",
        "skip": verdict.get("skip") or "",
        "guards": {"only_when_flat": automation.only_when_flat,
                   "once_per_day": automation.once_per_day},
    }


def payload(settings) -> Dict[str, Any]:
    """Everything the panel renders: the criteria, the list, and the tick's likely verdict."""
    strategy = _strategy(settings)
    automation = automation_mod.read(settings, strategy)
    cached = automation_mod.read_list(settings, strategy)
    moment = datetime.now(timezone.utc)
    # The panel judges the list from the SAME session the tick would, out of the same clock-free
    # rule (``src.config.session``): a badge that said "fresh" for a list the loop would re-screen
    # before acting on it would be the panel disagreeing with the thing it exists to show.
    current = session_mod.stamp(settings, moment)
    in_session = session_mod.is_open_at(settings, moment)
    stale = automation_mod.list_is_stale(cached, automation, moment=moment, session=current)

    return {
        "strategy": strategy,
        "instrument": str(getattr(settings, "instrument", "") or "").upper(),
        "on": bool(automation.on),
        "criteria": automation_mod.to_payload(automation),
        "preview": _preview(settings, strategy, automation, cached, in_session),
        "session": current,
        "in_session": in_session,
        "list": {
            "at": (cached or {}).get("at"),
            "age_seconds": _age_seconds((cached or {}).get("at"), moment),
            "text": (cached or {}).get("text") or list_mod.criteria_text(automation.enter),
            "rows": _rows(cached),
            # Which session the list is an answer FOR, and why it cannot be judged if that is not
            # this one. Both go to the panel so the reader sees what the loop sees.
            "session": (cached or {}).get("session"),
            # ...and whether it was screened INSIDE the session, which is the reader's answer to a
            # question the age cannot answer: a fresh list screened before the bell is the previous
            # close's numbers, and it looks exactly like a fresh one screened after it.
            "screened_in_session": session_mod.is_in_session((cached or {}).get("session")),
            "stale": stale,
            "file": str(automation_mod.list_path(settings, strategy)),
        },
        "file": str(automation_mod.locate(settings, strategy)),
        "max_rows": MAX_ROWS,
    }


def _rows(cached: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cached rows, trimmed to what a panel may render and with the numbers made safe."""
    out: List[Dict[str, Any]] = []
    for row in ((cached or {}).get("rows") or [])[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        out.append({
            "rank": row.get("rank"),
            "symbol": row.get("symbol"),
            "name": row.get("name") or "",
            "price": row.get("price"),
            "change_percent": row.get("change_percent"),
            "volume": row.get("volume"),
            "dollar_volume": row.get("dollar_volume"),
            "market_cap": row.get("market_cap"),
            "sector": row.get("sector") or "",
            "rank_change": row.get("rank_change"),
            "rank_volume": row.get("rank_volume"),
            "score": row.get("score"),
        })
    return out


def _age_seconds(stamp: Any, moment: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - when).total_seconds())


def _validated(automation: automation_mod.Automation) -> List[str]:
    """Why these criteria cannot be used, in the words of the fields they came from.

    Small on purpose, and only the things that make the screen meaningless rather than merely
    aggressive: a list of nothing, a band that cannot contain anything, a negative age.
    """
    errors: List[str] = []
    enter = automation.enter
    if int(enter.get("size") or 0) < 1:
        errors.append("List size: at least one instrument")
    if int(enter.get("size") or 0) > MAX_ROWS:
        errors.append(f"List size: at most {MAX_ROWS} instruments")
    low, high = int(enter.get("market_cap_min") or 0), int(enter.get("market_cap_max") or 0)
    if low and high and low >= high:
        errors.append("Market cap floor must be below the ceiling")
    if float(enter.get("min_price") or 0) < 0:
        errors.append("Minimum price cannot be negative")
    if int(enter.get("min_volume") or 0) < 0:
        errors.append("Minimum day volume cannot be negative")
    if int(automation.switch.get("margin") or 0) < 0:
        errors.append("Margin cannot be negative")
    if int(automation.switch.get("max_age_minutes") or 0) < 0:
        errors.append("List maximum age cannot be negative")
    return errors


def save(settings, body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Store the criteria for the active strategy. Never raises.

    Not gated on trading being OFF, unlike every other write in this panel's family: turning the
    automation OFF is how you stop it, and an automation that could only be disarmed while the
    thing it drives is stopped would be worse than useless.
    """
    strategy = _strategy(settings)
    automation = automation_mod.from_payload(body)
    errors = _validated(automation)
    if errors:
        return {"ok": False, "message": "The criteria were not saved", "errors": errors}

    previous = automation_mod.read(settings, strategy)
    automation_mod.write(settings, strategy, automation)
    if dict(previous.enter) != dict(automation.enter):
        # A list screened against other criteria is an answer to another question, so it is
        # dropped rather than judged: the next read (panel or tick) screens a new one.
        state_files_forget(settings, strategy)
    logger.info("Instrument automation saved for %s (on=%s)", strategy, automation.on)
    return {"ok": True, "message": "Saved", "errors": [], "payload": payload(settings)}


def state_files_forget(settings, strategy: str) -> None:
    """Throw away a list the new criteria have invalidated (see ``save``)."""
    path = automation_mod.list_path(settings, strategy)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - a read-only volume
        logger.warning("Could not drop the stale list at %s: %s", path, exc)


def refresh(settings, *, force: bool = True) -> Dict[str, Any]:
    """Screen a new list now, for the panel's ↻. Never raises."""
    strategy = _strategy(settings)
    automation = automation_mod.read(settings, strategy)
    outcome = list_mod.cache_and_screen(settings, strategy, automation, force=force)
    return {
        "ok": not outcome.get("error"),
        "message": outcome.get("error") or outcome.get("reason") or "Screened",
        "refreshed": bool(outcome.get("refreshed")),
        "rows": len(outcome.get("rows") or []),
        "payload": payload(settings),
    }
