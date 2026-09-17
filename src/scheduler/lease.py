"""The lease: what makes "exactly one trading process" true rather than intended.

Two loops mean double orders, and the failure is quiet — both processes look healthy, the
dashboard shows a working bot, and the account simply ends up with twice the position that
was sized for. Nothing crashes and nothing logs an error, so the only evidence is the
account. This file is what stops that before it starts.

The lease is one small JSON file beside the other runtime state
(``<data root>/loop.lock``, the same directory as ``trading.json``). It holds who is
running the loop and, more usefully, **when that loop has said it will next do something**:
``next_wake`` is the bar boundary the holder is sleeping until, and ``expires_at`` is that
moment plus a grace period. A holder therefore declares its own liveness in advance rather
than being judged by a heartbeat that has to be refreshed on a timer — the loop sleeps for
an hour at a time on purpose, and a heartbeat that needs waking to prove it is alive would
fight the very scheduling this design is built around.

**How a holder is judged** (and the order matters):

1. **Nothing there, or unreadable** — nobody. A corrupt lock must not be able to stop a
   bot for ever; it is logged loudly and taken over, because the alternative is manual
   cleanup at 09:30, which is exactly what this design refuses to require.
2. **Same host and the pid is alive** — the holder, whatever the timestamps say. The pid is
   the stronger signal here: a laptop that suspends overnight leaves a process that is
   alive but has not ticked, and taking its lease would start the second loop this file
   exists to prevent. Pid reuse errs the same safe way — a recycled pid looks alive, so the
   lease is respected until it expires.
3. **Same host and the pid is gone** — nobody. This is the crash case, and it is detected
   immediately rather than after an expiry: a supervisor restarting the loop should not
   have to wait out a grace period that was sized for a slow provider.
4. **Another host** (a container restarted elsewhere, a second machine, a test) — the pid
   means nothing, so only ``expires_at`` can decide: held until then, nobody after.

The consequence worth stating plainly: **on one machine a crashed loop is taken over at
once, and a live one is never taken over at all.** Expiry exists for the cases where the
pid cannot answer.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import state_files

logger = logging.getLogger(__name__)

__all__ = [
    "LEASE_FILENAME",
    "LEASE_GRACE_SECONDS",
    "Lease",
    "LeaseHeld",
    "acquire",
    "describe",
    "holder",
    "is_expired",
    "lease_path",
    "read",
    "refresh",
    "release",
]

#: Beside ``trading.json`` and ``credential_checks.json`` — runtime state, never in git.
#: The root-anchored ``/data/`` ignore rule covers it.
LEASE_FILENAME = "loop.lock"

#: How long past its declared wake the holder may go before another host may take over.
#: Generous on purpose: it has to cover a provider sync that runs long, and the cost of
#: being wrong in this direction is a refused start rather than a double order.
LEASE_GRACE_SECONDS = 300.0


class LeaseHeld(RuntimeError):
    """Another live loop holds the lease. Carries the holder's record.

    Raised rather than returned because this is the one condition that must stop a start
    completely: a caller may not ignore it, and there is nothing sensible to do instead.
    """

    def __init__(self, record: Dict[str, Any]):
        self.record = dict(record or {})
        super().__init__(
            f"the loop is already running — {describe(self.record)}. Refusing to start a "
            "second one: two loops place two sets of orders."
        )


@dataclass(frozen=True)
class Lease:
    """This process's claim on the loop, and the file that records it."""

    path: Path
    pid: int
    host: str
    started: str
    strategy: str = ""
    record: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.record)


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """One ISO form for every timestamp in the file, so a reader never has to guess."""
    return moment.astimezone(timezone.utc).isoformat()


def _parse(text: Any) -> Optional[datetime]:
    """A timestamp from the file, or ``None`` when it is missing or unreadable."""
    if not text:
        return None
    if isinstance(text, datetime):
        moment = text
    else:
        try:
            moment = datetime.fromisoformat(str(text))
        except (TypeError, ValueError):
            return None
    if moment.tzinfo is None:
        # A naive stamp from a hand-edited or older file: UTC is the only defensible guess,
        # and it is the form this module writes.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------
def lease_path(settings) -> Path:
    """``<data root>/loop.lock`` — see ``state_files.state_path`` for the root."""
    return state_files.state_path(settings, LEASE_FILENAME)


def read(settings) -> Optional[Dict[str, Any]]:
    """The record in the file, or ``None`` when there is nothing usable there.

    "Unusable" deliberately includes a file that exists but cannot be parsed: a reader has
    no way to tell that from an absent one, and treating it as a live holder would need the
    manual cleanup this design exists to avoid.
    """
    path = lease_path(settings)
    if not path.exists():
        return None
    record = state_files.read_json(path, default={})
    if not record:
        logger.warning("Ignoring an empty %s — starting as if no loop were running", path)
        return None
    return record


def _process_is_alive(pid: Any) -> Optional[bool]:
    """Is ``pid`` running on THIS machine? ``None`` when the question cannot be asked.

    ``os.kill(pid, 0)`` sends signal 0, which is the POSIX way of asking "does this process
    exist and may I signal it" without disturbing it.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it is simply not ours to signal. That is still a live process.
        return True
    except OSError:
        return None
    return True


def _this_host() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - a hostname lookup that fails is not fatal
        return ""


def is_expired(record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Has the holder's declared wake passed its grace?

    A record with no readable ``expires_at`` counts as EXPIRED. The file is written by this
    module, so an unparseable one means something else made it — and a lock nobody can
    interpret must not be able to hold the loop hostage.
    """
    moment = now or _now()
    expires = _parse((record or {}).get("expires_at"))
    if expires is None:
        return True
    return moment >= expires


def holder(settings, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The live holder of the lease, or ``None`` when this process may take it.

    The order of the checks is the whole logic and it is not interchangeable — see the
    module docstring for why the pid outranks the timestamps on this host and only the
    timestamps can speak for another host.
    """
    record = read(settings)
    if record is None:
        return None

    moment = now or _now()
    mine_host = _this_host()
    holder_host = str(record.get("host") or "")

    if holder_host and mine_host and holder_host != mine_host:
        return None if is_expired(record, moment) else record

    alive = _process_is_alive(record.get("pid"))
    if alive is True:
        return record
    if alive is False:
        # A dead pid on this host is a crashed loop, and it needs no grace period.
        logger.warning(
            "Taking over %s: %s is no longer running",
            lease_path(settings), describe(record),
        )
        return None
    return None if is_expired(record, moment) else record


def describe(record: Optional[Dict[str, Any]]) -> str:
    """One line naming the holder, for a log line or a refusal message."""
    if not record:
        return "no loop"
    who = f"pid {record.get('pid', '?')}"
    host = record.get("host")
    if host:
        who = f"{who} on {host}"
    strategy = record.get("strategy")
    if strategy:
        who = f"{who} (strategy {strategy!r})"
    started = record.get("started")
    if started:
        who = f"{who}, started {started}"
    return who


# ---------------------------------------------------------------------------
# claiming it
# ---------------------------------------------------------------------------
def _write(path: Path, record: Dict[str, Any]) -> None:
    state_files.write_json(path, record)


def acquire(
    settings,
    *,
    strategy: str = "",
    now: Optional[datetime] = None,
) -> Lease:
    """Claim the loop for this process, or raise :class:`LeaseHeld`.

    The record is written before it is returned, so a second process starting a moment
    later sees it — the check and the claim are not atomic, and pretending otherwise would
    be worse than being honest about it: the window is a few milliseconds wide, it is
    closed by the lease being written FIRST, and the alternative (an OS file lock) does not
    survive a container restart the way a stale-timeout file does.
    """
    moment = now or _now()
    blocker = holder(settings, moment)
    if blocker is not None:
        raise LeaseHeld(blocker)

    pid = os.getpid()
    host = _this_host()
    path = lease_path(settings)
    record: Dict[str, Any] = {
        "pid": pid,
        "host": host,
        "started": _stamp(moment),
        "heartbeat": _stamp(moment),
        # Until the first tick says otherwise, the lease is good for exactly one grace
        # period. The first refresh widens it to the boundary the loop is actually
        # sleeping until, which is what a later reader needs to judge it.
        "next_wake": None,
        "expires_at": _stamp(moment + timedelta(seconds=LEASE_GRACE_SECONDS)),
        "strategy": str(strategy or ""),
    }
    _write(path, record)
    logger.info("Acquired the loop lease at %s as %s", path, describe(record))
    return Lease(
        path=path, pid=pid, host=host, started=record["started"],
        strategy=record["strategy"], record=record,
    )


def refresh(
    lease: Lease,
    *,
    next_wake: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Say that this process is alive and when it will next do something.

    Called after every tick, before the sleep. ``next_wake`` is what makes the record
    useful rather than decorative: it is the moment the loop has COMMITTED to waking, so a
    reader can tell "sleeping until 14:30" from "died at 14:05" without a timer.
    """
    moment = now or _now()
    record = dict(lease.record)
    record["pid"] = lease.pid
    record["host"] = lease.host
    record["heartbeat"] = _stamp(moment)
    record["next_wake"] = _stamp(next_wake) if next_wake else None
    record["expires_at"] = _stamp(
        (next_wake or moment) + timedelta(seconds=LEASE_GRACE_SECONDS)
    )
    _write(lease.path, record)
    return record


def release(lease: Lease) -> None:
    """Give the lease up, so the next start does not have to notice this one died.

    Only ever removes OUR record: a lease already taken over (a stale timeout while this
    process was wedged, say) belongs to whoever holds it now, and deleting it would hand the
    loop to nobody while that process is still trading.
    """
    try:
        record = state_files.read_json(lease.path, default={})
    except OSError:  # pragma: no cover - read_json does not raise, but be safe on a delete
        record = {}
    if record and int(record.get("pid") or 0) != lease.pid:
        logger.warning(
            "Not releasing %s: it now belongs to %s", lease.path, describe(record)
        )
        return
    try:
        lease.path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - a read-only volume, a permissions change
        logger.warning("Could not remove %s: %s", lease.path, exc)
    logger.info("Released the loop lease at %s", lease.path)
