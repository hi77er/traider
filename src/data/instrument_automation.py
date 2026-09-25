"""The Top-10 list: which instruments the automation may switch to, and the cache it is read from.

The ranking answers "the best performing small caps that also have the highest trading volume"
with two ranks rather than one number. Every candidate gets a rank by day change and a rank by
DOLLAR volume (price x shares: a $2 stock trading 10M shares is not the same market as a $40 one
trading 10M), and the list is ordered by their sum — so a name has to be genuinely strong on both
sides to lead, and the panel can show which side each one earned its place on. Any single-number
score would have to invent an exchange rate between percent and dollars.

The list is CACHED because it is a screener call, and a loop ticks every bar: the tick reads the
file and refreshes it only when it has aged past the criteria's own limit. The panel refreshes it
on demand. Nothing here talks to the broker or to OpenBB — this is the Yahoo screener path only
(``src.data.screener``), the same one the Market page's panels use.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.config import automation as automation_mod
from src.config import session as session_mod
from src.data import screener as screener_data

logger = logging.getLogger(__name__)

__all__ = ["FETCH_ROWS", "cache_and_screen", "criteria_text", "decide", "rank_rows", "screen",
           "spec_from"]

#: Yahoo answers at most 250 rows per request, and the ranking is done over what it returns —
#: so this much of the small-cap tape is the universe the Top-10 is chosen from.
FETCH_ROWS = 250

#: What the screener is asked for, in one request: small caps, above the price floor, from the
#: primary US tape, best day change first. The VOLUME side of the ranking is applied locally
#: (see ``rank_rows``) because Yahoo can sort by one field only.
SORT_FIELD = "percentchange"


def spec_from(enter: Dict[str, Any]) -> screener_data.ScreenSpec:
    """The screener query the entering criteria describe."""
    return screener_data.ScreenSpec(
        key="instrument_automation",
        label="Instrument automation",
        description="Small caps, ranked for the instrument automation's Top-10 list.",
        sort_field=SORT_FIELD,
        sort_asc=False,
        market_cap_min=int(enter.get("market_cap_min") or 0) or None,
        market_cap_max=int(enter.get("market_cap_max") or 0) or None,
        min_price=float(enter.get("min_price") or 0),
        min_volume=int(enter.get("min_volume") or 0),
        us_only=True,
    )


def criteria_text(enter: Dict[str, Any]) -> str:
    """The screen, in the words the panel and the tick's notes use."""
    return spec_from(enter).criteria()


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN, which is how a screener reports "not reported"
        return None
    return number


def _ranked(rows: List[dict], key: Callable[[dict], Optional[float]]) -> Dict[int, int]:
    """``index -> position`` for one measure, best first. A missing value ranks last."""
    ordered = sorted(
        range(len(rows)),
        key=lambda i: (key(rows[i]) is None, -(key(rows[i]) or 0.0)),
    )
    return {index: position + 1 for position, index in enumerate(ordered)}


def rank_rows(frame, size: int) -> List[dict]:
    """The Top ``size`` rows of a screened frame, ranked for performance AND for volume."""
    if frame is None or len(frame) == 0:
        return []
    rows = [dict(row) for row in frame.to_dict("records") if row.get("symbol")]
    for row in rows:
        price = _number(row.get("price"))
        volume = _number(row.get("volume"))
        row["dollar_volume"] = None if price is None or volume is None else price * volume

    by_change = _ranked(rows, lambda row: _number(row.get("change_percent")))
    by_volume = _ranked(rows, lambda row: _number(row.get("dollar_volume")))

    listed = []
    for index, row in enumerate(rows):
        rank_change = by_change.get(index, len(rows) + 1)
        rank_volume = by_volume.get(index, len(rows) + 1)
        listed.append({
            "symbol": str(row.get("symbol") or "").upper(),
            "name": row.get("name") or "",
            "price": _number(row.get("price")),
            "change_percent": _number(row.get("change_percent")),
            "volume": _number(row.get("volume")),
            "dollar_volume": row.get("dollar_volume"),
            "market_cap": _number(row.get("market_cap")),
            "avg_volume_3m": _number(row.get("avg_volume_3m")),
            "sector": row.get("sector") or "",
            "rank_change": rank_change,
            "rank_volume": rank_volume,
            "score": rank_change + rank_volume,
        })
    listed.sort(key=lambda row: (row["score"], row["rank_change"], -(row["change_percent"] or 0.0)))
    for position, row in enumerate(listed[: max(0, int(size))], start=1):
        row["rank"] = position
    return listed[: max(0, int(size))]


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _row_text(row: Dict[str, Any]) -> str:
    """One candidate, in the words the panel and the tick's log line both use."""
    return (f"{row.get('symbol')} (rank {row.get('rank')}, "
            f"{_fmt(row.get('change_percent'))}% on ${_fmt(row.get('dollar_volume'), 0)} traded)")


def decide(
    automation: automation_mod.Automation, current: str, rows: List[dict]
) -> Dict[str, Any]:
    """The switch criteria applied to one list — pure: no files, no broker, no clock.

    Lives here, beside the ranking it reads, because BOTH processes need it and only one of them
    may import the loop: the tick decides with it, and the panel shows what the tick would decide
    (``src.web`` may not reach ``src.scheduler``). Returns ``{switch, target, reason, skip}``.
    """
    ranked = {_symbol_of(row): row for row in rows}
    leader = rows[0]
    hero = _symbol_of(leader)
    current = str(current or "").strip().upper()
    if not current or not hero:
        return {"switch": False, "skip": "the current instrument or the list has no symbol"}
    if current == hero and automation.mode in ("not_in_list", "not_first"):
        return {"switch": False, "skip": f"{current} is already rank 1"}

    entry = ranked.get(current)
    if automation.mode == "not_first":
        return {"switch": True, "target": hero,
                "reason": f"{hero} now leads the list ({_row_text(leader)}) and {current} does not"}
    if automation.mode == "margin":
        if entry is None:
            return {"switch": True, "target": hero,
                    "reason": f"{current} is not on the list at all — {_row_text(leader)} leads it"}
        gap = int(entry.get("score") or 0) - int(leader.get("score") or 0)
        if gap < automation.margin:
            return {"switch": False,
                    "skip": f"{hero} leads {current} by {gap} points, under the margin of "
                            f"{automation.margin}"}
        return {"switch": True, "target": hero,
                "reason": f"{hero} leads {current} by {gap} points of the blended rank "
                          f"({_row_text(leader)})"}
    # not_in_list — the default: a name that holds its place is never churned out of it.
    if entry is not None:
        return {"switch": False,
                "skip": f"{current} is still in the list at rank {entry.get('rank')}"}
    return {"switch": True, "target": hero,
            "reason": f"{current} has dropped out of the top {len(rows)} — {_row_text(leader)}"}


def _symbol_of(row: Any) -> str:
    return str((row or {}).get("symbol") or "").strip().upper()


def screen(enter: Dict[str, Any], *, size: Optional[int] = None,
           fetch: Optional[int] = None) -> List[dict]:
    """One screener request, ranked. Raises ``ScreenerError`` when the provider refuses."""
    spec = spec_from(enter)
    frame = screener_data.run_screen(spec, size=int(fetch or FETCH_ROWS))
    return rank_rows(frame, int(size or enter.get("size") or 10))


def cache_and_screen(
    settings,
    strategy: str,
    automation: Optional[automation_mod.Automation] = None,
    *,
    force: bool = False,
    at: Optional[datetime] = None,
    screen_call: Optional[Callable[[Dict[str, Any]], List[dict]]] = None,
) -> Dict[str, Any]:
    """Screen when the cached list is missing or has aged, and cache what came back.

    Returns ``{refreshed, rows, at, text, error, reason}`` and never raises: a provider that
    refuses is reported to whoever asked (the panel shows it; the tick records it as the reason
    it could not judge the criteria) and the previous list is left where it is.
    """
    moment = at or datetime.now(timezone.utc)
    criteria = automation or automation_mod.read(settings, strategy)
    # WHICH session this screening is for, stamped into the document. The screener's numbers are
    # the last completed regular session's, so the list has to say which session it answers for —
    # that stamp is what stops a pre-market list from being judged once the bell has rung.
    current = session_mod.stamp(settings, moment)
    cached = automation_mod.read_list(settings, strategy)

    if not force:
        stale = automation_mod.list_is_stale(cached, criteria, moment=moment, session=current)
        if stale is None:
            return {"refreshed": False, "rows": cached.get("rows") or [], "at": cached.get("at"),
                    "text": cached.get("text") or criteria_text(criteria.enter),
                    "session": cached.get("session"), "error": None,
                    "reason": "the cached list is still current"}

    try:
        rows = (screen_call or (lambda enter: screen(enter, size=criteria.size)))(criteria.enter)
    except Exception as exc:  # noqa: BLE001 - a screener outage is not a failed tick
        logger.warning("Could not screen the instrument list for %s: %s", strategy, exc)
        return {"refreshed": False, "rows": (cached or {}).get("rows") or [],
                "at": (cached or {}).get("at"), "text": criteria_text(criteria.enter),
                "session": (cached or {}).get("session"),
                "error": str(exc), "reason": "the screener could not be read"}

    document = {
        "at": moment.isoformat(),
        "session": current,
        "strategy": strategy,
        "criteria": dict(criteria.enter),
        "text": criteria_text(criteria.enter),
        "rows": rows,
        "size": criteria.size,
    }
    automation_mod.write_list(settings, strategy, document)
    logger.info("Screened %d instruments for %s (%s, %s)", len(rows), strategy,
                document["text"], current)
    return {"refreshed": True, "rows": rows, "at": document["at"], "text": document["text"],
            "session": current, "error": None, "reason": ""}
