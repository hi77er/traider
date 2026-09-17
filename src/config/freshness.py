"""Is the running process still the code that is on disk?

Python loads a module once. Edit `credentials.py` or `trading_service.py`, forget to
restart uvicorn, and the process keeps serving the PREVIOUS gate while the dashboard,
the popup and the tests all look current. That is not hypothetical — it is how a live
switch once armed a bot on credentials the broker had already revoked, with the fix
sitting on disk the whole time and refusing correctly under every test.

So the modules that decide whether trading may start are watched by mtime: the
process records them as it imports, compares them whenever the switch asks for its
state, and says so when they have moved on without it. The check is deliberately
narrow — it covers the gate, not the whole tree — because that is the part where
"the code I am running is not the code I wrote" has consequences beyond a stale
button label.

This lives in ``src/config``, below both layers, for the same reason
``trading_state`` does: it has two readers. The dashboard asks whenever it builds
the trading payload; the **execution loop** asks before a tick, because a loop whose
source has moved on is running a strategy nobody is looking at. The watched paths
are an argument, so a second reader adds its own set without disturbing the first —
and the default set may name a file in ``src/web`` because those are *paths*, not
imports. This module must keep importing nothing from the project.

This is a development affordance, not a security boundary: it tells the operator to
restart. It cannot make an old process run new code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

# The gate, in the order it would be missed: the credential check itself and the
# service that consults it before writing the ON state.
WATCHED: tuple = (
    "src/execution/credentials.py",
    "src/web/services/trading_service.py",
)

# src/config/freshness.py -> src/config -> src -> repo root
ROOT = Path(__file__).resolve().parents[2]


def _mtime(path: Path) -> Optional[float]:
    """Modification time, or None when the file is not there (never raises: this
    runs on every trading payload and must not be able to break it)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def snapshot(paths=WATCHED, root: Path = ROOT) -> Dict[str, Optional[float]]:
    """The mtime of every watched file right now."""
    return {rel: _mtime(Path(root) / rel) for rel in paths}


# Taken as this module is imported: the moment the process read the gate.
LOADED: Dict[str, Optional[float]] = snapshot()


def info(
    paths=WATCHED,
    root: Path = ROOT,
    loaded: Optional[Dict[str, Optional[float]]] = None,
) -> dict:
    """Whether the gate on disk is newer than the gate this process loaded.

    ``changed`` names the files that moved, so the message can be specific rather
    than a vague "something changed". A file that disappeared or appeared counts as
    changed too.
    """
    recorded = LOADED if loaded is None else loaded
    now = snapshot(paths, root)
    changed: List[str] = []
    for rel, current in now.items():
        was = recorded.get(rel)
        if current is None or was is None or current > was:
            changed.append(rel)
    return {
        "stale": bool(changed),
        "changed": sorted(changed),
        "watched": list(paths),
        "message": (
            "The server is running an older build of the trading gate than the files on disk ("
            + ", ".join(sorted(changed))
            + " changed since it started). Restart it before trusting the switch."
        )
        if changed
        else "",
    }
