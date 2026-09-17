"""What the live loop wrote — one tree per strategy, one line per event.

    data/live_results/<strategy-slug>/
        latest.json             the panel's view: the LAST tick, whatever it did
        index.json              one record per trading day
        ticks/<date>.jsonl      append-only: bar key, signal, intents, refusals
        orders.jsonl            every submit: env, client_order_id, status, fill
        trades.jsonl            closed round trips (the ledger)
        state-<env>.json        the driver's memory, per environment

Four decisions this layout encodes, each of which was a choice:

**The broker is the source of truth; these files answer "why".** What is held, which orders
are working and what filled comes from Alpaca. What these files add is the reason — which
bar, which signal, which refusal — and nothing here should ever be read as the account's
state. A deleted log therefore costs the explanation and not the screen.

**Append-only ``.jsonl`` for the logs.** A rewritten array loses its tail on a crash, which
is precisely when the tail is wanted. A reader must skip a line it cannot parse: the loop may
be mid-append, so a partial trailing line is NORMAL, not corruption.

**``latest.json`` is written on EVERY tick, including the ones that do nothing.** It carries
the heartbeat, and a quiet day — no new bar, market closed, or trading off — must still move
it. Otherwise "the loop is alive with nothing to do" and "the loop is dead" look identical
from the dashboard, and those two want opposite responses.

**Days are the MARKET's days.** ``ticks/<date>.jsonl`` and the index are keyed by the date in
the configured market timezone, not UTC, so one trading session is one file rather than two
halves either side of midnight. The same boundary is what the deferred daily-loss limits will
reset on.

Nothing in here decides anything, and nothing in here is imported by the web layer: the
dashboard READS this tree, the loop writes it. ``tests/test_architecture.py`` pins that.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.config import artifacts, state_files

logger = logging.getLogger(__name__)

__all__ = [
    "LATEST",
    "STATE_PREFIX",
    "append_line",
    "append_order",
    "append_tick",
    "append_trade",
    "index_path",
    "latest_path",
    "legacy_state_files",
    "live_root",
    "load_index",
    "load_latest",
    "order_record",
    "orders_path",
    "read_lines",
    "retire_legacy_state",
    "save_latest",
    "state_path",
    "strategy_dir",
    "tick_log_path",
    "tick_record",
    "ticks_dir",
    "trade_record",
    "trading_day",
    "trades_path",
    "upsert_day",
]

LATEST = "latest.json"
STATE_PREFIX = "state-"

# The pre-(strategy, env) state file, written beside the dataset as
# ``strategy_state_<name>.json``. Recognised only so it can be moved aside — never read.
_LEGACY_GLOB = "strategy_state_*.json"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def live_root(settings) -> Path:
    """``<data root>/live_results`` — the account's live output tree."""
    return artifacts.output_root(settings, folder=artifacts.LIVE_FOLDER, setting="live_dir")


def strategy_dir(settings, name: str) -> Path:
    """Directory holding everything one strategy's loop produced."""
    return artifacts.live_strategy_dir(settings, name)


def latest_path(settings, name: str) -> Path:
    return strategy_dir(settings, name) / LATEST


def index_path(settings, name: str) -> Path:
    return strategy_dir(settings, name) / "index.json"


def ticks_dir(settings, name: str) -> Path:
    return strategy_dir(settings, name) / "ticks"


def tick_log_path(settings, name: str, when: Any = None) -> Path:
    return ticks_dir(settings, name) / f"{trading_day(settings, when)}.jsonl"


def orders_path(settings, name: str) -> Path:
    return strategy_dir(settings, name) / "orders.jsonl"


def trades_path(settings, name: str) -> Path:
    return strategy_dir(settings, name) / "trades.jsonl"


def state_path(settings, name: str, env: str) -> Path:
    """The driver's state file — see ``artifacts.live_state_path`` for why it is keyed."""
    return artifacts.live_state_path(settings, name, env)


def load_state(settings, name: str, env: str) -> Dict[str, Any]:
    """What the driver believes: the position it holds, and the bar it last decided.

    Read-only, and tolerant like every other reader here — a missing or torn file yields an
    empty dict, which the caller reads as "nothing is held". The dashboard is the reader
    this exists for: "is the open position protected?" is answered by comparing the levels
    the machine RECORDED on the position against the exits the broker is actually resting,
    and a position nobody can read is not a position.
    """
    return state_files.read_json(state_path(settings, name, env), default={})


def trading_day(settings, when: Any = None) -> str:
    """``YYYY-MM-DD`` in the MARKET's timezone — one session, one file."""
    tz_name = str(getattr(settings, "market_timezone", "") or "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - a bad timezone must not stop the log
        logger.warning("Unknown MARKET_TIMEZONE %r — dating the log in UTC", tz_name)
        tz = timezone.utc
    moment = when or datetime.now(timezone.utc)
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date().isoformat()


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------
def tick_record(
    *,
    strategy: str,
    env: str,
    action: str,
    settings=None,
    at: Any = None,
    reason: str = "",
    bar: Any = None,
    signal: Any = None,
    intents: Optional[List[Any]] = None,
    adopted: Optional[Dict[str, Any]] = None,
    position: Any = None,
    order_ids: Optional[List[str]] = None,
    trades: Optional[List[Any]] = None,
    open_count: Optional[int] = None,
    protected: Optional[bool] = None,
) -> Dict[str, Any]:
    """One tick, in the shape the panel and the logs both read.

    Built here rather than assembled at the call site so the shape lives in one place: a
    key the panel reads and the loop forgets to write is otherwise a blank field forever.
    ``action`` is the loop's verdict — ``decided``, ``noop``, ``refused``, ``off`` — and it
    is always present, because "what did the tick do" is the first question asked of it.
    """
    moment = at or datetime.now(timezone.utc)
    record: Dict[str, Any] = {
        "at": moment.isoformat() if hasattr(moment, "isoformat") else str(moment),
        "day": trading_day(settings, moment) if settings is not None else str(moment)[:10],
        "strategy": strategy,
        "env": str(env or "").lower(),
        "action": action,
        "reason": reason,
        "bar": None if bar is None else str(bar),
        "signal": signal,
        "intents": list(intents or []),
        "adopted": adopted,
        "position": position,
        "order_ids": list(order_ids or []),
        # The trades this tick CLOSED. Carried in the record as well as appended to
        # trades.jsonl: the panel answers "what did the last tick do" from latest.json, and
        # a close it cannot see is a close the operator has to go digging for.
        "trades": list(trades or []),
    }
    if open_count is not None:
        record["open_count"] = int(open_count)
    if protected is not None:
        record["protected"] = bool(protected)
    return record


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def save_latest(settings, name: str, record: Dict[str, Any]) -> Path:
    """Rewrite the panel's view. Called on EVERY tick, including no-ops (the heartbeat)."""
    return artifacts.write_json_atomic(latest_path(settings, name), record)


def order_record(
    *,
    settings,
    strategy: str,
    env: str,
    at: Any,
    bar: Any = None,
    intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One submitted order, in the shape ``orders.jsonl`` holds.

    Built from the driver's own per-intent report rather than from the broker's payload,
    because the question this file answers is "what did the bot try to do and why" — the
    broker's copy answers what happened to it. Both are needed, and the
    ``client_order_id`` is what joins them.
    """
    intent = dict(intent or {})
    moment = at or datetime.now(timezone.utc)
    return {
        "at": moment.isoformat() if hasattr(moment, "isoformat") else str(moment),
        "day": trading_day(settings, moment) if settings is not None else str(moment)[:10],
        "strategy": strategy,
        "env": str(env or "").lower(),
        "bar": None if bar is None else str(bar),
        "intent": intent.get("intent"),
        "reason": intent.get("reason") or "",
        "status": intent.get("status") or "",
        "price": intent.get("price"),
        "expected": intent.get("expected"),
        "order_id": intent.get("order_id"),
        "client_order_id": intent.get("client_order_id"),
    }


def trade_record(
    *,
    settings,
    strategy: str,
    env: str,
    at: Any,
    bar: Any = None,
    leg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One closed round trip, in the shape ``trades.jsonl`` holds.

    The fields come from the engine's own leg — the same row a backtest stores — so a
    live trade and a replayed one are described identically and the two can be compared
    without translating between them. The wall-clock moment is added because a leg carries
    only the bar INDEX it ended on, and "when" is the first thing anyone asks of a trade.
    """
    leg = dict(leg or {})
    moment = at or datetime.now(timezone.utc)
    return {
        "at": moment.isoformat() if hasattr(moment, "isoformat") else str(moment),
        "day": trading_day(settings, moment) if settings is not None else str(moment)[:10],
        "strategy": strategy,
        "env": str(env or "").lower(),
        "bar": None if bar is None else str(bar),
        "entry_idx": leg.get("entry_idx"),
        "exit_idx": leg.get("exit_idx"),
        "direction": leg.get("direction"),
        "entry_price": leg.get("entry_price"),
        "exit_price": leg.get("exit_price"),
        "ret": leg.get("ret"),
        "equity_ret": leg.get("equity_ret"),
        "weight": leg.get("weight"),
        "bars": leg.get("bars"),
        "reason": leg.get("reason"),
        "stop_percent": leg.get("stop_percent"),
        "skipped": bool(leg.get("skipped")),
    }


def append_line(path: Path, record: Dict[str, Any]) -> Path:
    """Append one line. A crash can leave the last line short, which readers tolerate.

    One ``json.dumps`` per line, and never a trailing comma or a wrapping array: a line is
    the unit a reader can survive losing, which is the whole reason for the format.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifacts.jsonable(record), ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return path


def append_tick(settings, name: str, record: Dict[str, Any], when: Any = None) -> Path:
    """Append a tick to its day's log. No-ops are not appended: the log is events."""
    return append_line(tick_log_path(settings, name, when), record)


def append_order(settings, name: str, record: Dict[str, Any]) -> Path:
    return append_line(orders_path(settings, name), record)


def append_trade(settings, name: str, record: Dict[str, Any]) -> Path:
    return append_line(trades_path(settings, name), record)


def upsert_day(settings, name: str, day: str, **fields: Any) -> Path:
    """Merge ``fields`` into one day's index entry, creating it if it is new.

    The index exists so listing the days a strategy ran never has to read the tick logs,
    which grow without bound. It is rewritten rather than appended because it is small and
    because a duplicate day entry would be a bug that shows up as a doubled session.
    """
    records = load_index(settings, name)
    for entry in records:
        if str(entry.get("day")) == str(day):
            entry.update(fields)
            break
    else:
        records.append({"day": str(day), **fields})
    records.sort(key=lambda e: str(e.get("day")))
    return artifacts.write_json_atomic(index_path(settings, name), records)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def read_lines(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Parsed lines, oldest first, skipping anything unreadable.

    Tolerating a bad line is the point, not laziness: the loop may be appending while the
    dashboard reads, so the last line can legitimately be half-written. Refusing to read the
    file at all would turn a normal race into an empty screen.
    """
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            logger.debug("Skipping an unreadable line in %s", path)
    if limit is not None and limit > 0:
        return out[-int(limit):]
    return out


def load_latest(settings, name: str) -> Optional[Dict[str, Any]]:
    """The last tick written, or ``None`` when this strategy has never run.

    ``None`` rather than an empty record, so a caller can tell "it has never run" from "it
    ran and did nothing" — the dashboard shows those differently, and the second one is
    normal.
    """
    path = latest_path(settings, name)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring an unreadable latest tick at %s: %s", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def load_index(settings, name: str) -> List[Dict[str, Any]]:
    """Every day this strategy has run. Never raises: an empty list is a valid answer."""
    path = index_path(settings, name)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring an unreadable live index at %s: %s", path, exc)
        return []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    # A hand-edited object is still worth reading rather than throwing away the day list.
    if isinstance(raw, dict) and raw:
        return [raw]
    return []


def read_ticks(settings, name: str, when: Any = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return read_lines(tick_log_path(settings, name, when), limit=limit)


def read_orders(settings, name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return read_lines(orders_path(settings, name), limit=limit)


def read_trades(settings, name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return read_lines(trades_path(settings, name), limit=limit)


# ---------------------------------------------------------------------------
# the pre-(strategy, env) state file
# ---------------------------------------------------------------------------
def legacy_state_files(settings) -> List[Path]:
    """The old ``strategy_state_<name>.json`` files, wherever they still sit.

    They live beside the dataset, because that is where ``state_files.state_path`` puts
    everything. They are keyed by instrument alone, so a file here cannot be attributed to
    a strategy or an account — and adopting it would mean guessing. Recognised only so it
    can be moved out of the way; nothing reads one.
    """
    hist = getattr(settings, "historical_data_dir", None)
    if not hist:
        return []
    where = Path(str(hist)).resolve().parent
    try:
        return sorted(where.glob(_LEGACY_GLOB))
    except OSError:  # noqa: BLE001 - a missing directory is not a problem here
        return []


def retire_legacy_state(settings) -> List[Path]:
    """Move any legacy state file aside so nothing can mistake it for current.

    Never deleted: it may be the only record of a position someone is holding, and the
    right response to that is to look at it, not to lose it. Never ADOPTED either — see
    ``legacy_state_files``. Called once on startup by the loop, not by the driver: reading
    and writing state is the driver's job, and housekeeping is not.
    """
    found = legacy_state_files(settings)
    if not found:
        return []
    target = live_root(settings) / "_legacy"
    moved: List[Path] = []
    for path in found:
        try:
            target.mkdir(parents=True, exist_ok=True)
            destination = target / path.name
            path.replace(destination)
            moved.append(destination)
            logger.warning(
                "Moved the pre-(strategy, env) state file %s to %s — it is NOT adopted: it "
                "cannot be attributed to a strategy or an account. Delete it once you have "
                "confirmed nothing is held.",
                path, destination,
            )
        except OSError as exc:
            logger.error("Could not move %s aside: %s", path, exc)
    return moved
