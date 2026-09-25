"""Starting the loop when the switch is turned on — and the other half of the same idea.

Turning trading ON starts the loop. Turning it off stops it, and that half needs no code here
at all: the loop re-reads the switch on every tick and in every slice of its sleep, and exits
when it is off (``src/scheduler/orchestrator.run``). The switch is the LIFECYCLE, so neither
direction needs a signal, a pid to get wrong, or a supervisor to be configured.

**The dashboard starts a PROCESS. It still never runs the loop.** ``src.main`` refuses to be
imported and refuses to run beside the web app (``check_host``), and the architecture test that
forbids a startup hook still holds — this is called from the route that arms the bot, not from
a lifespan. The two processes stay two, which is what keeps a dashboard restart harmless to
trading, and what keeps the window you debug a broken loop through from being the thing a
broken loop takes down with it.

**Detached, in its own session.** The loop has to outlive the request that started it and the
server that served it: ``start_new_session`` puts it in its own process group, so stopping the
dashboard — including a Ctrl-C in the terminal it was started from — does not stop trading.
That is the same reasoning that put the loop in its own process to begin with.

**A failed start is REPORTED, never fatal to the arming.** The operator's decision is the
switch, and it is written first. If the process cannot be started, "trading is ON with nothing
running it" is the truth, and the panel says so with this reason; undoing the arming would be
this module overruling a decision it does not own.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from src.config import state_files
from src.web.services import loop_service

logger = logging.getLogger(__name__)

__all__ = ["LOG_FILENAME", "running", "start"]

#: Where the loop's own output goes. It is started detached, so its stdout is nobody's
#: terminal — a daemon's output is the one place nobody looks, which is why it is a FILE beside
#: the state, named here rather than left to whatever the parent's stdout happened to be.
LOG_FILENAME = "loop.log"

#: The module the loop lives in. A STRING, not an import: importing it would collapse the two
#: processes, and ``src.main`` refuses to be imported for exactly that reason.
LOOP_MODULE = "src.main"

# src/web/services/loop_control.py -> src/web/services -> src/web -> src -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]


def log_path(settings) -> Path:
    """``<data root>/loop.log`` — in the data root, so a dashboard knows where to look."""
    return state_files.state_path(settings, LOG_FILENAME)


def running(settings) -> bool:
    """Is a loop honouring the lease right now? Never raises.

    ``loop_service`` answers this and nothing else tries to: it is the same judgement the
    status chip makes, so "already running, so do not start another" and what the panel shows
    can never disagree. A CLAIM alone is not enough — ``overdue`` means a process claimed the
    lease and then died, and that one should be replaced rather than left to hold the slot.
    """
    return loop_service.status(settings).get("state") == loop_service.RUNNING


def _spawn(command, **kwargs):
    """The one place in the codebase that creates a process. A test replaces THIS.

    A door, not a convenience: the suite must never start a loop (a second loop would take
    the lease and trade), and the alternative — patching ``subprocess.Popen`` — also breaks
    ``subprocess.run``, which other tests need. One named door is the same shape as
    ``positions.probe``: the thing that must not happen in a test is a call outward, so it
    gets its own name and the test closes it there.
    """
    return subprocess.Popen(command, **kwargs)  # noqa: S603 - our interpreter, our module


def start(settings) -> Dict[str, Any]:
    """Start the loop unless one is already running. Never raises.

    Returns ``{started, running, pid, reason, message}``. ``running`` is what the caller should
    report; ``reason`` is empty unless something went wrong, and is meant to be shown to the
    operator (it is the difference between "the loop is up" and "you are armed and alone").
    """
    if running(settings):
        return {
            "started": False,
            "running": True,
            "pid": None,
            "reason": "",
            "message": "the loop is already running",
        }

    command = [sys.executable, "-m", LOOP_MODULE]
    try:
        path = log_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Opened by the PARENT and closed here as soon as the child has the file descriptor:
        # the loop owns its own output, and a server that held the handle open would keep the
        # file locked for the life of the dashboard.
        handle = open(path, "a", encoding="utf-8")
    except OSError as exc:
        return _failed(f"its log could not be opened ({exc})")

    try:
        process = _spawn(
            command,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001 - a refused fork must not fail the arming
        logger.exception("Could not start the trading loop")
        return _failed(f"the process could not be started ({exc})")
    finally:
        handle.close()

    logger.warning("Started the trading loop (pid %s)", process.pid)
    return {
        "started": True,
        "running": True,
        "pid": process.pid,
        "reason": "",
        "message": f"the loop was started (pid {process.pid})",
    }


def _failed(reason: str) -> Dict[str, Any]:
    logger.error("The trading loop did not start: %s", reason)
    return {"started": False, "running": False, "pid": None, "reason": reason, "message": reason}
