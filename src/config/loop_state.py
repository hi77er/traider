"""Who is running the loop, and when it says it will next do something.

``data/loop.lock`` is the answer to "is a bot running this account?", and it is read by
**two processes**:

* the loop, before it starts, to find out whether it may (see
  ``src/scheduler/lease.py``, which also claims and renews it), and
* the dashboard, which says whether anything is running at all — because a quiet market and
  a dead loop look identical otherwise, and those two want opposite responses from whoever
  is looking. (It used to recite the holder, the next wake and the age of the last tick as
  well; the log page's own loop panel carries those from the loop's records, in one place.)

That second reader is why this module exists rather than the rules living in the loop. The
dashboard must not import ``src.scheduler`` at all: the whole point of the two-process split
is that the dashboard survives a broken loop, and a code dependency on the loop package is
how a "read the lease" helper becomes a "start the loop" import six months later. The rules
for judging a holder live here, below both of them, and ``src/scheduler/lease.py`` builds
the claiming and renewing on top.

What lives here is only READING. The write side — claim, renew, release — is in the loop,
because only the holder may touch it.

**How a holder is judged**, and the order is not interchangeable:

1. **Nothing there, or unreadable** — nobody. A corrupt lock must not be able to stop a bot
   for ever; it is logged loudly and taken over, because the alternative is manual cleanup
   at 09:30, which is exactly what this design refuses to require.
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

Alive is not the same as WORKING, and only one of the two is this file's job to enforce. A
holder that declared a wake and is past its grace with the process still there keeps its
lease — nothing here will take a claim from a running process — but it is not a healthy
loop, and :func:`is_stalled` is what says so, so that everything reporting on the loop can
say it too instead of relaying a pid as health.

The record's shape is the loop's to define (``acquire`` writes it); this module reads it:

    pid · host · started · heartbeat · next_wake · expires_at · strategy

``next_wake`` is what makes it useful rather than decorative: the loop sleeps for an hour at
a time on purpose, so a timestamp from the last tick cannot distinguish "asleep until 14:30"
from "died at 14:05". The holder declares the moment it has committed to waking, and
``expires_at`` is that moment plus a grace period.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import state_files

logger = logging.getLogger(__name__)

__all__ = [
    "LEASE_FILENAME",
    "LEASE_GRACE_SECONDS",
    "describe",
    "holder",
    "is_expired",
    "is_stalled",
    "late_by_seconds",
    "lease_path",
    "parse_stamp",
    "read",
]

#: In the data root itself, where a lease can be found before anything else is read — runtime
#: state, never in git. The root-anchored ``/data/`` ignore rule covers it.
LEASE_FILENAME = "loop.lock"

#: How long past its declared wake the holder may go before another host may take over.
#: Generous on purpose: it has to cover a provider sync that runs long, and the cost of
#: being wrong in this direction is a refused start rather than a double order.
LEASE_GRACE_SECONDS = 300.0


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def now() -> datetime:
    """An aware UTC moment. One clock for the whole file, so nothing has to guess."""
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    """One ISO form for every timestamp in the file, so a reader never has to guess."""
    return moment.astimezone(timezone.utc).isoformat()


def parse_stamp(text: Any) -> Optional[datetime]:
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
        # and it is the form this project writes.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def grace_end(from_moment: datetime) -> datetime:
    """When a claim made at ``from_moment`` stops being believable."""
    return from_moment + timedelta(seconds=LEASE_GRACE_SECONDS)


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
    """Is ``pid`` RUNNING on this machine? ``None`` when the question cannot be asked.

    ``os.kill(pid, 0)`` sends signal 0, which is the POSIX way of asking "does this process
    exist and may I signal it" without disturbing it.

    Existing is not the same as running, and the difference is a whole class of confusion:
    a process that has exited but whose parent never called ``wait()`` keeps its pid as a
    ZOMBIE, and signal 0 answers for a corpse exactly as it does for a live process. The
    dashboard is the parent of every loop it starts (``src.web.services.loop_control``) and
    never waits on one, so a loop that dies without releasing its lease — ``kill -9``, the
    OOM killer — leaves a claim that reads as honoured. The panel then says "running" with a
    countdown to a boundary nothing will wake for, and arming refuses to start a replacement
    because it believes one is up. Both were seen, one of them for the rest of the day.

    ``waitpid`` with ``WNOHANG`` is the cheap way to tell them apart, and it REAPS the corpse
    while it is there — which is why the check is worth doing even though it has a side
    effect. It only answers for our own children, which is exactly the case that can leave one:
    a zombie belonging to somebody else is reaped by that somebody, or by init.
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
    try:
        reaped, _status = os.waitpid(number, os.WNOHANG)
    except ChildProcessError:
        # Not our child, so it is not ours to reap either — and a process somebody else owns
        # that has exited will be reaped by its owner. Alive.
        return True
    except OSError:  # pragma: no cover - defensive: an unusual platform, not a state
        return True
    # ``0`` means "still running". Anything else is the pid of a child that has exited, which
    # ``WNOHANG`` has just reaped.
    return reaped == 0


def this_host() -> str:
    """The name this machine answers to, as recorded in a claim. Never raises."""
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - a hostname lookup that fails is not fatal
        return ""


def is_expired(record: Dict[str, Any], at: Optional[datetime] = None) -> bool:
    """Has the holder's declared wake passed its grace?

    A record with no readable ``expires_at`` counts as EXPIRED. The file is written by this
    project, so an unparseable one means something else made it — and a lock nobody can
    interpret must not be able to hold the loop hostage.
    """
    moment = at or now()
    expires = parse_stamp((record or {}).get("expires_at"))
    if expires is None:
        return True
    return moment >= expires


def holder(settings, at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The live holder of the lease, or ``None`` when a loop may take it.

    The order of the checks is the whole logic and it is not interchangeable — see the
    module docstring for why the pid outranks the timestamps on this host and why only the
    timestamps can speak for another one.
    """
    record = read(settings)
    if record is None:
        return None

    moment = at or now()
    mine_host = this_host()
    holder_host = str(record.get("host") or "")

    if holder_host and mine_host and holder_host != mine_host:
        return None if is_expired(record, moment) else record

    alive = _process_is_alive(record.get("pid"))
    if alive is True:
        return record
    if alive is False:
        # A dead pid on this host is a crashed loop, and it needs no grace period.
        logger.warning(
            "Taking over %s: %s is no longer running", lease_path(settings), describe(record)
        )
        return None
    return None if is_expired(record, moment) else record


def late_by_seconds(record: Dict[str, Any], at: Optional[datetime] = None) -> Optional[float]:
    """How long past its declared wake the holder is. ``None`` when it declared none.

    Zero before the wake: this is the age of a COMMITMENT rather than of a process, and it is
    the number to show when a loop that is plainly alive has plainly stopped doing anything.
    """
    wake = parse_stamp((record or {}).get("next_wake"))
    if wake is None:
        return None
    return max(0.0, ((at or now()) - wake).total_seconds())


def is_stalled(record: Dict[str, Any], at: Optional[datetime] = None) -> bool:
    """Is this LIVE holder no longer doing anything? — the question a pid cannot answer.

    :func:`holder` treats a live pid as the holder whatever the timestamps say, and that is
    right: on one machine a pid outranks a clock, and taking a claim from a process that is
    still running is how two loops come to place two sets of orders. But "alive" was also
    being read as "working", and on 2026-09-25 that hid a wedged loop for a whole session:
    the process woke for its bar, blocked on one open socket, and held the lease — so the
    dashboard said ``running``, the countdown counted down to a boundary nothing would reach,
    and no bar was traded. A live claim past the grace on the wake it declared is NOT a
    healthy loop, and everything that reports on one should say so.
    """
    if parse_stamp((record or {}).get("next_wake")) is None:
        # Nothing committed to yet: a loop that is starting, not one that is stuck.
        return False
    return is_expired(record, at)


def describe(record: Optional[Dict[str, Any]]) -> str:
    """One line naming the holder, for a log line, a refusal message or a dashboard."""
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
