"""Small JSON state files that live beside the data, never in git.

Two things in this project are *runtime state* rather than configuration:

* whether trading is ON (``data/trading/trading.json``), and
* which Alpaca credential pairs have been verified (``data/credential_checks.json``).

Both must sit outside ``settings/*.json`` on purpose — the configuration files are
exactly what the trading lock freezes. Both are also written while a request is in
flight, so a half-finished write must never be readable: the read/write pair lives
here rather than being copied into each owner, because the failure modes (unreadable
file, torn write, permissions) are the same for both and only one copy can be
reviewed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def state_path(settings, filename: str, folder: str = "") -> Path:
    """``<data root>/[<folder>/]<filename>`` — beside ``historical/`` and ``backtest_results/``.

    Runtime state gets a folder of its own under the data root — ``auth/``, ``trading/``,
    ``account/``, ``strategies/automation/`` — so the root reads as a list of folders rather than a
    pile of loose files whose owners have to be remembered. ``write_json`` creates whatever is
    missing, so a caller names the folder and nothing else.
    """
    root = Path(settings.historical_data_dir).resolve().parent
    return root / folder / filename if folder else root / filename


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    """The document at ``path``, or a copy of ``default``. **Never raises**: an
    unreadable file must not be mistaken for meaningful content."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
        logger.warning("Ignoring %s: expected a JSON object", path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable state at %s: %s", path, exc)
    return dict(default)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Atomic, owner-only write (``mkstemp`` creates the file 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
