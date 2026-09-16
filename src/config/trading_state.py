"""The trading switch — its *state*, owned by nobody's layer.

``data/trading.json`` is the single fact "is the bot allowed to trade, and in
which environment". It is read by **two processes**:

* the dashboard (``src/web``), which renders the switch and refuses
  reconfiguration while it is on, and
* the execution loop, which reads it **every tick** — that is what makes turning
  trading off take effect at the next bar without touching the process.

That second reader is the reason this module exists. The switch used to live in
``src/web/services/trading_service.py``, which meant ``src.execution`` had to
import ``src.web`` to answer "am I allowed to trade?" — an arrow pointing the
wrong way, dragging FastAPI and every route module into the trading process. The
state moved here so the arrow points ``execution -> config <- web``.

What lives here is only the file-backed state: read, write, and the two
questions anyone needs to ask of it. The *policy* — the credential re-check, the
live-money confirmation, the configuration lock — stays in
``src/web/services/trading_service.py``, where the request is being made. This
module must keep importing nothing but ``src.config.state_files``.

The state is runtime, not configuration: it does not live in the strategy store
(which the lock itself would otherwise freeze), it lives beside the datasets and
is gitignored with them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from src.config import state_files

STATE_FILENAME = "trading.json"

# The shape of "never written yet": OFF. A missing file must mean OFF, never
# anything else — the default is the safe answer.
OFF: Dict[str, Any] = {"on": False, "since": None}


def now_iso() -> str:
    """Timestamps in state files are UTC, second precision, always with offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path(settings) -> Path:
    """``<data root>/trading.json`` — beside ``historical/`` and ``backtest_results/``."""
    return state_files.state_path(settings, STATE_FILENAME)


def get_state(settings) -> Dict[str, Any]:
    """Current state, defaulting to OFF. Never raises: an unreadable file must
    not be mistaken for "on"."""
    state = state_files.read_json(state_path(settings), OFF)
    state["on"] = bool(state.get("on"))
    return state


def write_state(settings, state: Dict[str, Any]) -> None:
    """Persist the switch. Atomic and owner-only (``mkstemp`` gives 0600)."""
    state_files.write_json(state_path(settings), state)


def is_trading_on(settings) -> bool:
    """Read the switch from disk — cheap, and never cached by callers.

    An execution loop that cached this would keep trading after the operator
    pressed stop, which is the one thing the switch must never do.
    """
    return bool(get_state(settings).get("on"))
