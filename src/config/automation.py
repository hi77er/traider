"""The instrument automation's criteria, and the files they live in.

Automation REPLACES the strategy's instrument with the best of a screener's Top-10, so its
settings are runtime state rather than frozen configuration: turning it off has to work while a
loop is trading, and everything under the trading lock (``strategies/store.json``) may not be
written then. It gets its own files for that reason, not for convenience.

Two of them, each with one writer, in ``data/strategies/automation/`` — the strategies folder,
because that is what they are about, and a folder of their own inside it because they are not the
frozen store beside them:

``automation-<strategy>.json``       the criteria — written by the panel.
``automation-list-<strategy>.json``  the cached Top-10 — written by whoever refreshed it: the
                                     panel's ↻, or the loop when the list has aged out.

Split for the same reason ``trading.json`` and ``loop.lock`` are apart: two processes write
here, and neither may be able to lose the other's work to a read-modify-write.

The criteria themselves are TABLES rather than typed fields, because they are meant to grow
("criteria added on the fly"): a row added to ``ENTER_FIELDS``/``SWITCH_FIELDS`` shows up in the
panel, in the screener query and in the prose the tick logs, without a second and third edit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import state_files
from src.config.artifacts import slug

logger = logging.getLogger(__name__)

__all__ = [
    "Automation", "ENTER_FIELDS", "SWITCH_FIELDS", "SWITCH_MODES",
    "describe", "from_payload", "list_path", "read", "read_list", "to_payload",
    "write", "write_list", "locate",
]

#: Under the strategies folder, in a folder of their own. One string for both files, so the criteria
#: and the list they screen can never end up in different places.
FOLDER = "strategies/automation"

#: The universe the list is picked from. Small caps by the conventional band, priced above a
#: dollar floor, and traded enough to be enterable at all — the entering criteria in the panel.
ENTER_FIELDS: tuple = (
    {"name": "market_cap_min", "label": "Market cap floor", "kind": "int", "unit": "USD",
     "default": 300_000_000, "help": "the bottom of the small-cap band"},
    {"name": "market_cap_max", "label": "Market cap ceiling", "kind": "int", "unit": "USD",
     "default": 2_000_000_000, "help": "and its top"},
    {"name": "min_price", "label": "Minimum price", "kind": "float", "unit": "USD",
     "default": 3.0, "help": "keeps penny stocks out"},
    {"name": "min_volume", "label": "Minimum day volume", "kind": "int", "unit": "shares",
     "default": 1_000_000, "help": "a name nobody trades could not be entered anyway"},
    {"name": "size", "label": "List size", "kind": "int", "unit": "instruments",
     "default": 10, "help": "how many of the ranked names are kept"},
)

#: When the tick may replace the instrument. All three are ranks on the SAME ranked list, so
#: they differ only in how much change they tolerate before acting.
SWITCH_MODES: tuple = (
    {"value": "not_in_list", "label": "The current instrument is no longer in the list",
     "help": "the list is the whole criterion — a name that holds its place is never churned"},
    {"value": "not_first", "label": "The current instrument is no longer the leader",
     "help": "follows the ranking exactly, so a changing leader changes the instrument"},
    {"value": "margin", "label": "Another instrument is ahead by the margin",
     "help": "the leader must beat the current one by this many places to be worth a switch"},
)

SWITCH_FIELDS: tuple = (
    {"name": "mode", "label": "Switch when", "kind": "choice", "options": SWITCH_MODES,
     "default": "not_in_list", "help": "what has to be true of the current instrument"},
    {"name": "margin", "label": "Margin", "kind": "int", "unit": "places", "default": 5,
     "help": "read only by the margin rule above"},
    {"name": "only_when_flat", "label": "Only while nothing is held", "kind": "bool",
     "default": True,
     "help": "a switch mid-position would leave it to the next instrument's rules"},
    {"name": "once_per_day", "label": "At most one switch a day", "kind": "bool",
     "default": True, "help": "read from the day's own tick log"},
    {"name": "max_age_minutes", "label": "List maximum age", "kind": "int", "unit": "minutes",
     "default": 60, "help": "older than this the tick refreshes the list before judging it"},
)

#: The fields the criteria are FOR. Added to below as the tables are read.
ENTER_NAMES = tuple(field_["name"] for field_ in ENTER_FIELDS)


def _coerce(kind: str, raw: Any, default: Any) -> Any:
    """One submitted or stored value, in the type its field declares. Junk falls back."""
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        return text in ("1", "true", "yes", "on")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        if kind == "int":
            return int(float(str(raw).strip()))
        if kind == "float":
            return float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return str(raw).strip()


def _defaults(fields: tuple) -> Dict[str, Any]:
    return {field_["name"]: field_["default"] for field_ in fields}


def _normalise(values: Optional[Dict[str, Any]], fields: tuple) -> Dict[str, Any]:
    """Stored or submitted values against a field table: known keys only, coerced, filled in."""
    out = _defaults(fields)
    for field_ in fields:
        name = field_["name"]
        if values is None or name not in values:
            continue
        out[name] = _coerce(field_["kind"], values.get(name), field_["default"])
    return out


@dataclass(frozen=True)
class Automation:
    """The criteria as the loop reads them: a switch and two sets of named values."""

    on: bool = False
    enter: Dict[str, Any] = field(default_factory=lambda: _defaults(ENTER_FIELDS))
    switch: Dict[str, Any] = field(default_factory=lambda: _defaults(SWITCH_FIELDS))

    # -- the values the gate asks for by name, so a typo is a KeyError and not a silent default
    @property
    def size(self) -> int:
        return int(self.enter.get("size") or 10)

    @property
    def mode(self) -> str:
        mode = str(self.switch.get("mode") or "not_in_list")
        return mode if mode in {m["value"] for m in SWITCH_MODES} else "not_in_list"

    @property
    def margin(self) -> int:
        return int(self.switch.get("margin") or 0)

    @property
    def only_when_flat(self) -> bool:
        return bool(self.switch.get("only_when_flat"))

    @property
    def once_per_day(self) -> bool:
        return bool(self.switch.get("once_per_day"))

    @property
    def max_age_minutes(self) -> int:
        return int(self.switch.get("max_age_minutes") or 0)

    def as_dict(self) -> Dict[str, Any]:
        return {"on": bool(self.on), "enter": dict(self.enter), "switch": dict(self.switch)}


def from_payload(payload: Optional[Dict[str, Any]]) -> Automation:
    """An ``Automation`` from a submitted body or a stored document. Never raises."""
    payload = payload or {}
    return Automation(
        on=_coerce("bool", payload.get("on"), False),
        enter=_normalise(payload.get("enter"), ENTER_FIELDS),
        switch=_normalise(payload.get("switch"), SWITCH_FIELDS),
    )


def to_payload(automation: Automation) -> Dict[str, Any]:
    """The document shape, plus the field tables the panel renders its inputs from."""
    return {
        "on": bool(automation.on),
        "enter": dict(automation.enter),
        "switch": dict(automation.switch),
        "enter_fields": [dict(f) for f in ENTER_FIELDS],
        "switch_fields": [dict(f) for f in SWITCH_FIELDS],
        "switch_modes": [dict(m) for m in SWITCH_MODES],
    }


def describe(automation: Automation) -> List[str]:
    """The criteria in one line each — what the panel shows and the tick's reason quotes."""
    lines = []
    for field_ in ENTER_FIELDS:
        lines.append(f"{field_['label'].lower()}: {automation.enter.get(field_['name'])}")
    for field_ in SWITCH_FIELDS:
        lines.append(f"{field_['label'].lower()}: {automation.switch.get(field_['name'])}")
    return lines


# ---------------------------------------------------------------------------
# the files
# ---------------------------------------------------------------------------
def locate(settings, strategy: str) -> Path:
    """``<data root>/strategies/automation/automation-<strategy>.json`` — the criteria."""
    return state_files.state_path(
        settings, f"automation-{slug(strategy or 'strategy')}.json", FOLDER
    )


def list_path(settings, strategy: str) -> Path:
    """``<data root>/strategies/automation/automation-list-<strategy>.json`` — the cached Top-10."""
    return state_files.state_path(
        settings, f"automation-list-{slug(strategy or 'strategy')}.json", FOLDER
    )


def read(settings, strategy: str) -> Automation:
    """The stored criteria for one strategy, or the defaults (automation OFF)."""
    document = state_files.read_json(locate(settings, strategy), {})
    return from_payload(document)


def write(settings, strategy: str, automation: Automation) -> Path:
    """Store the criteria. The PANEL is the only writer of this file."""
    path = locate(settings, strategy)
    state_files.write_json(path, {**automation.as_dict(), "strategy": strategy})
    logger.info("Instrument automation %s for %s: %s", "ON" if automation.on else "OFF",
                strategy, path)
    return path


def read_list(settings, strategy: str) -> Optional[Dict[str, Any]]:
    """The cached list, or ``None`` when nothing has been screened yet."""
    document = state_files.read_json(list_path(settings, strategy), {})
    if not document or not document.get("rows"):
        return None
    return document


def write_list(settings, strategy: str, document: Dict[str, Any]) -> Path:
    """Cache a screened list (the panel's ↻, or the loop's own refresh when it has aged)."""
    path = list_path(settings, strategy)
    state_files.write_json(path, document)
    return path


def list_is_stale(
    cached: Optional[Dict[str, Any]], automation: Automation, *, moment=None,
    session: Optional[str],
) -> Optional[str]:
    """Why the cached list cannot be judged on, or ``None`` when it can.

    A list screened against DIFFERENT criteria is not a slightly old list, it is an answer to
    another question — so a criteria change invalidates it outright rather than waiting out the
    age. The age is the honest bound: a screener's volumes move, and a decision made on this
    morning's list at four in the afternoon is a decision about the morning.

    ``session`` is WHO IS ASKING, and the rule that needs it is the one the screener makes
    unavoidable: its numbers are the last COMPLETED regular session's (measured: a pre-market
    screen returns the previous close's change %), so a list belongs to one session and cannot be
    judged in another. Before the bell the day's own ranking does not exist yet, and after it the
    ranking a switch would act on has closed. A session of ``None`` means the caller cannot tell —
    an install with no exchange timezone, or a test that is not about this — and then only the
    age and the criteria are judged, because inventing "another session" from an unknown is how a
    list gets re-screened by a caller that never knew the time.
    """
    if not cached:
        return "no list has been screened yet"
    if dict(cached.get("criteria") or {}) != dict(automation.enter):
        return "the entering criteria have changed since this list was screened"
    if session:
        screened_in = str(cached.get("session") or "")
        if not screened_in:
            return ("the cached list does not say which session it was screened in, and only a "
                    "list screened inside the session it is judged in can be acted on")
        if screened_in != str(session):
            return (f"the cached list was screened in another session ({screened_in}, now "
                    f"{session}) — the screener's numbers are the last completed session's")
    stamp = cached.get("at")
    if not stamp:
        return "the cached list carries no timestamp"
    try:
        from datetime import datetime, timezone

        when = datetime.fromisoformat(str(stamp))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "the cached list's timestamp cannot be read"
    now = moment or _now()
    age = (now - when).total_seconds() / 60.0
    if automation.max_age_minutes and age > automation.max_age_minutes:
        return f"the cached list is {age:.0f} minutes old (the limit is {automation.max_age_minutes})"
    return None


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
