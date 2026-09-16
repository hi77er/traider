"""The two processes, named in one place.

TRAIDER runs as **two processes that share files and nothing else**:

| Role | Started by | Owns |
|------|-----------|------|
| ``LOOP`` | ``python -m src.main`` | the bar clock, the shared strategy machine, and every order |
| ``DASHBOARD`` | ``python -m src.web.app`` | HTTP: the UI, config, backtests, the reports |

They are deliberately separate (see README, "Two processes"):

* the dashboard is the window you debug a broken loop through, so it must survive
  a loop crash — and a loop crash must not take the UI down with it;
* the loop must survive a dashboard restart, which happens constantly in
  development;
* there is no in-memory state to share, because every setting and every result is
  already a file (``src/config/effective.py`` re-resolves on file mtime).

Nothing here imports anything from the project, so both entry points can use it
without either one gaining a dependency on the other. That is the point of
keeping it separate rather than putting it in ``src/config``.
"""

from __future__ import annotations

import os
import sys

LOOP = "loop"
DASHBOARD = "dashboard"

_DESCRIPTIONS = {
    LOOP: "the trading loop — owns the bar clock and every order",
    DASHBOARD: "the dashboard — serves HTTP and never trades",
}

_ASGI_APP_MODULE = "src.web.app"


def describe(role: str) -> str:
    """One line naming this process, for a startup banner or a log line."""
    return f"[{role}] pid={os.getpid()} — {_DESCRIPTIONS.get(role, role)}"


def announce(role: str) -> str:
    """Print the banner and return it, so a caller can also log it."""
    line = describe(role)
    print(line, flush=True)
    return line


def asgi_app_loaded() -> bool:
    """Is the dashboard's ASGI app loaded **in this process**?

    Both entry points must be able to answer this without importing the web
    package, so it is a ``sys.modules`` lookup and nothing more.

    This is how "the dashboard must never own the loop" stops being a convention
    and becomes something the code enforces: a loop that finds the dashboard
    loaded inside itself refuses to start, rather than trading from the same
    process that serves HTTP (where a crashed route or a ``--reload`` restart
    would kill trading, and a wedged loop would kill the UI you debug it with).
    """
    return _ASGI_APP_MODULE in sys.modules


def is_main_entry(module_name: str) -> bool:
    """True when ``module_name`` is the module the interpreter was started with.

    ``python -m src.main`` sets it to ``"__main__"``; importing ``src.main`` from
    something else does not. A host that can be *imported* is a library, and this
    is how a host tells the difference.
    """
    return module_name == "__main__"
