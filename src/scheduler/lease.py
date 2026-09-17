"""Claiming the loop: what makes "exactly one trading process" true rather than intended.

Two loops mean double orders, and the failure is quiet — both processes look healthy, the
dashboard shows a working bot, and the account simply ends up with twice the position that
was sized for. Nothing crashes and nothing logs an error, so the only evidence is the
account. This file is what stops that before it starts.

**A lease, not a lock.** A lock is held until someone releases it, so a crashed holder holds
it for ever. A lease is time-bounded: the holder declares when it will next do something and
renews that, and it expires on its own if the holder stops. That is the whole reason for the
word — a crashed loop must not need a human at 09:30.

**The holder declares the wake rather than sending a heartbeat.** ``next_wake`` is the bar
boundary the loop is sleeping until, and ``expires_at`` is that moment plus a grace period,
so a reader can tell "asleep until 14:30" from "died at 14:05" without a timer. A heartbeat
would need waking up to prove the loop is alive, which fights the very scheduling this design
is built around — the loop sleeps for an hour at a time on purpose.

This module is the WRITE side: claim, renew, release. Reading — the path, the record's shape,
and the rules for judging whether a holder is alive — is in ``src/config/loop_state.py``,
below both processes, because the DASHBOARD has to read those and must not import this
package at all (see ``tests/test_architecture.py``). The read API is re-exported here so a
caller that holds a lease does not have to know about the split.

Not an OS lock (``fcntl.flock``) on purpose: an OS lock does not survive a container restart
and the dashboard cannot read it, while a stale-timeout file can be inspected with ``cat``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import state_files
from src.config.loop_state import (  # noqa: F401 - re-exported for callers holding a lease
    LEASE_FILENAME,
    LEASE_GRACE_SECONDS,
    describe,
    grace_end,
    holder,
    is_expired,
    lease_path,
    now,
    parse_stamp,
    read,
    stamp,
    this_host,
)

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
# claiming it
# ---------------------------------------------------------------------------
def _write(path: Path, record: Dict[str, Any]) -> None:
    state_files.write_json(path, record)


def acquire(
    settings,
    *,
    strategy: str = "",
    at: Optional[datetime] = None,
) -> Lease:
    """Claim the loop for this process, or raise :class:`LeaseHeld`.

    The record is written before it is returned, so a second process starting a moment
    later sees it — the check and the claim are not atomic, and pretending otherwise would
    be worse than being honest about it: the window is a few milliseconds wide, it is
    closed by the lease being written FIRST, and the alternative (an OS file lock) does not
    survive a container restart the way a stale-timeout file does.
    """
    moment = at or now()
    blocker = holder(settings, moment)
    if blocker is not None:
        raise LeaseHeld(blocker)

    pid = os.getpid()
    host = this_host()
    path = lease_path(settings)
    record: Dict[str, Any] = {
        "pid": pid,
        "host": host,
        "started": stamp(moment),
        "heartbeat": stamp(moment),
        # Until the first tick says otherwise, the lease is good for exactly one grace
        # period. The first refresh widens it to the boundary the loop is actually
        # sleeping until, which is what a later reader needs to judge it.
        "next_wake": None,
        "expires_at": stamp(grace_end(moment)),
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
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Say that this process is alive and when it will next do something.

    Called after every tick, before the sleep. ``next_wake`` is what makes the record
    useful rather than decorative: it is the moment the loop has COMMITTED to waking, so a
    reader can tell "sleeping until 14:30" from "died at 14:05" without a timer.
    """
    moment = at or now()
    record = dict(lease.record)
    record["pid"] = lease.pid
    record["host"] = lease.host
    record["heartbeat"] = stamp(moment)
    record["next_wake"] = stamp(next_wake) if next_wake else None
    record["expires_at"] = stamp(grace_end(next_wake or moment))
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
