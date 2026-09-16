"""Where results live, and the three helpers every writer needs.

Two things in TRAIDER end up as files under the account's data folder: backtest
runs (``data/backtest_results/``) and, from Phase 3, live results
(``data/live_results/``). Both want the same four things — a filesystem-safe
strategy name, a JSON encoder that survives numpy and pandas, an atomic write, and
the root those trees hang off.

They live here rather than in either owner because the alternative is the
dependency this module exists to prevent: ``src/execution`` must not import
``src/backtest`` (the trading process must not need the backtester), and a second
copy of ``jsonable`` is how two writers start disagreeing about what a timestamp
looks like on disk.

This module therefore imports **nothing from the project**, and
``tests/test_architecture.py`` keeps it that way. It is deliberately below
``src/config``'s own layers: settings, state files and results are three different
things, and only the first of those is configuration.

Nothing here decides anything — each function is a pure transformation of its
arguments into a path or a string.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict

__all__ = ["jsonable", "output_root", "slug", "write_json_atomic"]

# Typographic punctuation -> plain ASCII, applied before the accent fold so
# "Alpha – AAPL" becomes "Alpha-AAPL" rather than "AlphaAAPL".
_PUNCT_MAP = str.maketrans(
    {
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2014": "-", "\u2015": "-", "\u2212": "-",
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u00a0": " ", "\u2026": "...", "\u00b7": "-",
    }
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DASH_RUN = re.compile(r"-{2,}")


def slug(name: str) -> str:
    """Filesystem- and URL-safe ASCII form of a strategy name.

    ``slug("Delta – NVDA - 1h") == "Delta-NVDA-1h"``. Never empty (falls back to
    ``"strategy"``), so it is always safe to use as a path segment — which matters
    because a strategy name is operator-supplied text that becomes a directory.
    """
    text = str(name or "").translate(_PUNCT_MAP)
    folded = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    cleaned = _DASH_RUN.sub("-", _UNSAFE.sub("-", folded)).strip("-._")
    return cleaned or "strategy"


def jsonable(obj: Any) -> Any:
    """Recursively coerce numpy/pandas scalars & timestamps to JSON types.

    Without this, ``json.dumps`` raises on the very values a trading record is made
    of — a numpy float from an indicator, a pandas ``Timestamp`` from a bar index —
    and the failure lands at write time, after the decision has been made.
    """
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if hasattr(obj, "item"):  # numpy scalar
        try:
            return jsonable(obj.item())
        except (TypeError, ValueError):
            pass
    if hasattr(obj, "isoformat"):  # pandas/numpy timestamps & datetimes
        return obj.isoformat()
    return str(obj)


def output_root(settings, *, folder: str, setting: str) -> Path:
    """The root of one output tree under the account's data folder.

    ``setting`` names the explicit ``Settings`` override (``BACKTEST_DIR``), and
    ``folder`` is the subfolder of ``DATA_DIR`` used when it is empty — so the
    account configures ONE path and each tree is a fixed subfolder of it, rather
    than two directories that can drift apart.

    The ``historical_data_dir`` fallback keeps a plain settings object working by
    placing results beside the dataset, which is how the layout worked before the
    account layer existed.
    """
    base = getattr(settings, setting, None)
    if base:
        return Path(str(base))
    hist = getattr(settings, "historical_data_dir", None)
    if hist:
        return Path(str(hist)).resolve().parent / folder
    return Path("data") / folder


def write_json_atomic(path: Path, payload: Any) -> Path:
    """Write ``payload`` as pretty JSON via a temp file + atomic replace.

    Every writer here is a state file or a result record, and both are read by a
    DIFFERENT process (the dashboard reads what the loop writes). A torn write is
    therefore not a lost file, it is a reader seeing half a position — so the
    replace is atomic and the encoder is ``jsonable``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(jsonable(payload), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
