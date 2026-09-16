"""THE TRADING LOOP — the process that owns the bar clock and every order.

    .venv/bin/python -m src.main        # this: the loop
    .venv/bin/python -m src.web.app     # the other one: the dashboard

**This is not the dashboard.** TRAIDER runs as two processes that share files and
nothing else; see ``src/process_info`` for which of them owns what, and README
"Two processes" for why. Put briefly: the dashboard is the window you debug a
broken loop through, so the two must not be able to fail each other.

What this process is for (once Phase 6 is built — it is NOT built yet):

1. sleep until the next bar boundary (not a fixed interval);
2. read the switch: ``src/config/trading_state.is_trading_on`` — every tick, never
   cached, so turning trading OFF takes effect at the next boundary;
3. gate on the broker clock and the configured trading window;
4. reconcile against Alpaca, decide on the newest closed bar, submit through the
   shared strategy machine, and record what happened.

The loop is the only writer of trading state and the only caller of the executor;
the dashboard only reads. Two loops would mean double orders, which is what the
lease file is for.
"""

from __future__ import annotations

import sys
from typing import Optional

from src.process_info import LOOP, announce, asgi_app_loaded, is_main_entry

#: Refused to start: the two processes were collapsed into one. See
#: ``src/process_info``.
EXIT_WRONG_HOST = 2

#: Correctly hosted, but the loop itself has not been written yet.
EXIT_NOT_IMPLEMENTED = 3


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


def main() -> int:
    announce(LOOP)

    refusal = check_host()
    if refusal:
        print(f"[{LOOP}] REFUSING TO START — {refusal}", file=sys.stderr, flush=True)
        return EXIT_WRONG_HOST

    # Phase 6 of TRAIDER_PLAN.md. Deliberately loud rather than a silent no-op: a
    # process that appears to be running while placing no orders is the same failure
    # mode the trading switch refuses, and for the same reason — it would look like
    # it works.
    print(
        f"[{LOOP}] the execution loop is not implemented yet (TRAIDER_PLAN.md, Phase 6).\n"
        f"[{LOOP}] nothing will be traded; this process cannot place an order.",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
