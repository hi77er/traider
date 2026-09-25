"""A call that is not allowed to park the caller for ever.

The data stack is a black box with its own sockets and its own retries: OpenBB builds the
request, yfinance decides how many times to re-ask, and curl_cffi owns the connection. A
read that stalls inside all of that has nothing above it to cut it off — and on 2026-09-25
one did: the loop woke for its bar, blocked on a single open socket, and sat there for the
rest of the session while its pid kept the dashboard saying "running" and no bar was traded.

So the deadline lives out here, around the call. Python cannot interrupt a thread that is
blocked in a socket read, so the call runs on its own daemon thread and the CALLER stops
waiting when the time is up; the abandoned thread ends by itself if the socket ever answers.
That makes this a guarantee about the caller — the tick carries on and the next bar is still
traded — not about the socket, which is why the clients also set explicit timeouts on the
sessions they hand the provider. Two nets, because the one that froze was not caught by the
first.

The seconds are passed in rather than read from a constant here, so a test can make them
small; each caller names the value it uses.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Type

logger = logging.getLogger(__name__)

__all__ = ["bounded"]


def bounded(
    call: Callable[[], Any],
    seconds: float,
    *,
    what: str,
    error: Type[Exception] = TimeoutError,
) -> Any:
    """Call ``call``, giving up on it after ``seconds`` and raising ``error``.

    Whatever ``call`` raised is re-raised here, on the caller's own thread, so a caller's
    ``except`` clauses behave exactly as they would without the deadline.
    """
    done = threading.Event()
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["raised"] = exc
        finally:
            done.set()

    # A raw daemon thread rather than a pool: an abandoned worker must never be able to hold
    # the process open at shutdown.
    threading.Thread(target=_run, name=f"bounded:{what}", daemon=True).start()
    if not done.wait(max(0.0, float(seconds))):
        logger.warning("%s did not answer within %.0fs — abandoning the wait", what, seconds)
        raise error(f"{what} did not answer within {seconds:.0f}s")
    if "raised" in box:
        raise box["raised"]
    return box.get("value")
