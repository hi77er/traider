"""THE TRADING LOOP — the process that owns the bar clock and every order.

    .venv/bin/python -m src.main            # the loop, until you stop it
    .venv/bin/python -m src.main --once     # one tick, then exit

**This is not the dashboard.** TRAIDER runs as two processes that share files and
nothing else; see ``src/process_info`` for which of them owns what, and README
"Two processes" for why. Put briefly: the dashboard is the window you debug a
broken loop through, so the two must not be able to fail each other.

What this process does:

1. refuse to run at all if the dashboard was loaded into it (``check_host`` below);
2. take the lease, so a second loop cannot start and place a second set of orders;
3. report what it found — legacy state retired, and the broker's own view of the position;
4. sleep to each bar boundary, and on each one read the switch, gate on the exchange
   clock, sync the dataset, reconcile, decide on the newest CLOSED bar, and record what
   happened.

The work itself is in ``src/scheduler/orchestrator.py`` (the tick) and
``src/scheduler/host.py`` (starting it), so this file is argv, the host guard and the exit
codes and nothing else.

**Exit codes** exist so a supervisor can tell "this loop is running" from "this loop cannot
run here", and they are the only thing cron or a container healthcheck can act on:

* ``0`` — the process ran. With ``--once`` that means a tick happened; the VERDICT
  (decided, noop, refused, closed, off) is in the record it wrote, not in this number. A
  closed market is not a process failure, and an exit code that said otherwise would alarm
  every night and every weekend.
* ``2`` — wrong host: this module was imported rather than run, or the dashboard is loaded
  inside this process.
* ``4`` — a LIVE loop already holds the lease. Nothing is wrong with this process;
  something else is already trading, and two loops mean double orders.
* ``1`` — an unexpected failure, reported on stderr.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, List, Optional

from src.process_info import LOOP, announce, asgi_app_loaded, is_main_entry

#: Refused to start: the two processes were collapsed into one. See
#: ``src/process_info``.
EXIT_WRONG_HOST = 2

#: Refused to start: a live loop already owns this account.
EXIT_ALREADY_RUNNING = 4


def check_host() -> Optional[str]:
    """Why this process must not run the loop here, or ``None`` if it may.

    The rule is one line of code and the whole reason the two processes are
    separate: the loop must not be the process that serves HTTP. If the dashboard
    is already loaded here the two have been collapsed — and then a crashed route
    or a dev ``--reload`` restart would kill trading, while a wedged loop would
    take down the very UI you would use to notice it.

    This enforces an invariant that would otherwise live only in the README, and a
    convention that is not checked is a convention that will be broken by whichever
    change is in a hurry.
    """
    if not is_main_entry(__name__):
        return (
            "the trading loop was imported rather than run: this module is a host, not a "
            "library. Something is trying to own the loop in-process — put it in its own "
            "process instead (python -m src.main)."
        )
    if asgi_app_loaded():
        return (
            "the dashboard (src.web.app) is loaded in this process, so the loop would share a "
            "process with the web server. Run the loop separately: python -m src.main."
        )
    return None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """The loop's whole command line. One flag, because there is one thing to ask for."""
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="The TRAIDER trading loop: wakes on bar boundaries and places orders.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "run exactly one tick and exit. The exit code says whether the tick RAN, not "
            "what it decided — read the record it wrote for that."
        ),
    )
    return parser.parse_args(argv)


def _report(lines) -> None:
    for line in lines:
        print(f"[{LOOP}] {line}", flush=True)


def _describe(record: Any) -> str:
    """One line for the operator, from the record the tick left behind."""
    if not isinstance(record, dict):
        return "no record was written"
    parts = [f"action={record.get('action') or '?'}"]
    if record.get("bar"):
        parts.append(f"bar={record['bar']}")
    if record.get("reason"):
        parts.append(str(record["reason"]))
    return " · ".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    # The host guard runs BEFORE argparse and before any config or data file is read: a
    # process that must not host the loop has no business touching the account on its way
    # out. It is also the one check that has to survive a test importing this module.
    refusal = check_host()
    if refusal:
        announce(LOOP)
        print(f"[{LOOP}] REFUSING TO START — {refusal}", file=sys.stderr, flush=True)
        return EXIT_WRONG_HOST

    options = parse_args(argv)
    announce(LOOP)

    # Imported here rather than at module scope so that ``check_host``, ``parse_args`` and
    # the exit codes stay importable — and testable — without pulling the whole trading
    # stack (and, with it, a provider client) into the process that only wanted to refuse.
    from src.config import effective
    from src.scheduler import host, lease

    settings = effective.get_effective_settings()
    try:
        records = host.serve(
            settings,
            once=options.once,
            resolve=effective.get_effective_settings,
            report=_report,
        )
    except lease.LeaseHeld as held:
        print(f"[{LOOP}] REFUSING TO START — {held}", file=sys.stderr, flush=True)
        return EXIT_ALREADY_RUNNING

    if options.once:
        _report([_describe(records[0] if records else None)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
