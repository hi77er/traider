"""Storage layout for backtest runs and the reports derived from them.

All backtest output lives under ONE root — the sibling of the dataset dir
(``data/historical`` -> ``data/backtest_results``) — so there is no second
``backtest/reports`` tree to keep in sync::

    data/backtest_results/
        <strategy-slug>/
            latest.json            # trimmed UI view the panel reads
            runs/<run_id>.json     # full, untruncated, self-describing run
            reports/<run_id>.json  # derived report bundle for that run
            reports/<run_id>/…     # optional exported artefacts (charts, csv)

Why a run *and* a ``latest.json``:

* ``runs/`` keeps the complete series (every equity point, every trade) plus the
  inputs that produced them, so a report or a parameter sweep has full fidelity
  and a result stays reproducible.
* ``latest.json`` is the bounded, UI-shaped view so the panel does not have to
  download a 60k-point curve on every poll.

``run_id`` is a sortable UTC stamp plus a short hash of the run inputs, e.g.
``20260910T112900Z-3f9ac1b2``: runs sort chronologically, older ones sit next to
newer ones, and re-running the same inputs is recognisable at a glance.

Strategy names are slugified to a filesystem/URL-safe ASCII form
(``slug("Delta – NVDA - 1h") == "Delta-NVDA-1h"``). Legacy flat files
(``<root>/<name>.json``, written before this layout existed) are still read, so
old results survive the upgrade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# The four generic helpers live in ``src/config/artifacts.py`` and are re-exported
# here, because they are not backtest ideas: ``src/execution/store.py`` needs the same
# notion of a safe name, a JSON encoder that survives numpy, an atomic write and a root
# under the account's data folder — and it must not learn them by importing THIS package
# (the trading process must not depend on the backtester).
from src.config.artifacts import jsonable, output_root, slug, write_json_atomic

__all__ = [
    "slug",
    "jsonable",
    "results_root",
    "strategy_dir",
    "latest_path",
    "runs_dir",
    "run_path",
    "reports_dir",
    "index_path",
    "inputs_hash",
    "new_run_id",
    "write_json_atomic",
    "save_run",
    "delete_run",
    "list_runs",
    "load_run",
    "load_latest",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def results_root(settings) -> Path:
    """Root of all backtest output — the account's configured backtest folder.

    ``BACKTEST_DIR`` (derived from the account's data folder, see
    ``Settings._derive_data_dirs``) is authoritative. The fallbacks keep a plain
    ``historical_data_dir``-only settings object working: results sit beside the
    dataset, which is how the layout worked before the account layer existed.
    """
    return output_root(settings, folder="backtest_results", setting="backtest_dir")


def strategy_dir(settings, name: str) -> Path:
    """Directory holding everything for one strategy."""
    return results_root(settings) / slug(name)


def latest_path(settings, name: str) -> Path:
    """The trimmed UI view the panel reads."""
    return strategy_dir(settings, name) / "latest.json"


def runs_dir(settings, name: str) -> Path:
    """Directory of full, self-describing runs (one file per execution)."""
    return strategy_dir(settings, name) / "runs"


def run_path(settings, name: str, run_id: str) -> Path:
    """File for a single full run."""
    return runs_dir(settings, name) / f"{slug(run_id)}.json"


def reports_dir(settings, name: str, run_id: Optional[str] = None) -> Path:
    """Where derived report artefacts for a run are written.

    With no ``run_id`` this is the report root for the strategy."""
    base = strategy_dir(settings, name) / "reports"
    return base / slug(run_id) if run_id else base


def index_path(settings, name: str) -> Path:
    """Menu index for the strategy: one small record per run.

    Kept so listing runs never has to read the (large, full-fidelity) run files
    just to render a menu."""
    return strategy_dir(settings, name) / "index.json"


def _legacy_path(settings, name: str) -> Path:
    """The pre-layout location: ``<root>/<name>.json``.

    Reproduces the original sanitiser (only ``/`` and ``\\`` were replaced) so
    files written by older versions are still found."""
    legacy = re.sub(r"[\\/]", "_", str(name or "")).strip() or "strategy"
    return results_root(settings) / f"{legacy}.json"


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------
def inputs_hash(inputs: dict) -> str:
    """Short content hash of a run's inputs (8 hex chars).

    Covers the whole snapshot (effective settings, rules, window, costs), so any
    change — including the dataset growing — yields a different hash. Because
    the engine is deterministic, two runs with the same hash ARE the same
    experiment: the newer one replaces the older instead of cluttering the
    report menu with an identical entry."""
    blob = json.dumps(jsonable(inputs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def new_run_id(inputs: dict, when: Optional[datetime] = None) -> str:
    """Sortable UTC stamp + ``inputs_hash``, e.g. ``20260910T114304Z-68a1eb29``.

    The stamp prefix makes run ids (and therefore file names and the report
    menu) sort chronologically with the newest first; the hash identifies the
    tested configuration."""
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{inputs_hash(inputs)}"


def _read_json(path: Path):
    """Parsed JSON at ``path``, or None when missing/corrupt."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt file must not break the UI
        pass
    return None


def _summary(run: dict) -> dict:
    """The small record the report menu needs (never the full series)."""
    metrics = run.get("metrics") or {}
    inputs = run.get("inputs") or {}

    def num(key):
        value = metrics.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    return {
        "run_id": run.get("run_id"),
        "generated_at": run.get("generated_at"),
        "gate_pass": bool((run.get("gate") or {}).get("pass")),
        "symbol": run.get("symbol"),
        "bar_size": run.get("bar_size"),
        "model_type": run.get("model_type"),
        "rows": run.get("rows"),
        "start": run.get("start"),
        "end": run.get("end"),
        "rules_hash": inputs.get("rules_hash"),
        "allow_short": inputs.get("allow_short"),
        "num_rules": len(inputs.get("rules") or []),
        "sharpe": num("sharpe"),
        "total_return_pct": num("total_return_pct"),
        "max_drawdown_pct": num("max_drawdown_pct"),
        "win_rate_pct": num("win_rate_pct"),
        "num_trades": metrics.get("num_trades"),
    }


def _load_index(settings, name: str) -> List[dict]:
    data = _read_json(index_path(settings, name))
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _drop_duplicate_runs(settings, name: str, digest: str) -> None:
    """Delete earlier runs of the SAME inputs, whatever their timestamp.

    Keeps one file per tested configuration so the report menu never lists the
    same experiment twice (the engine is deterministic, so the results match)."""
    directory = runs_dir(settings, name)
    if not directory.is_dir():
        return
    suffix = f"-{digest}.json"
    for path in directory.glob("*.json"):
        if path.name.endswith(suffix):
            try:
                path.unlink()
            except OSError:
                logger.warning("Could not remove superseded run %s", path)


def save_run(settings, name: str, run: dict, latest: Optional[dict] = None) -> dict:
    """Persist a completed run, refresh ``latest.json`` and the run index.

    ``run`` is the full engine result (no caps or trims); ``latest`` is the
    bounded panel view (defaults to ``run`` itself, e.g. for a caller that has no
    trimming step). A previous run of the same inputs is superseded. Returns the
    paths written."""
    inputs = run.get("inputs") or {}
    run_id = str(run.get("run_id") or new_run_id(inputs))
    digest = run_id.rsplit("-", 1)[-1] if "-" in run_id else inputs_hash(inputs)

    _drop_duplicate_runs(settings, name, digest)
    run_file = write_json_atomic(run_path(settings, name, run_id), run)

    # Refresh the index: drop this run's record AND any record for a superseded
    # run of the same inputs, so the index cannot accumulate stale entries whose
    # file has been removed (which would otherwise grow forever).
    suffix = f"-{digest}"
    entries = [
        entry
        for entry in _load_index(settings, name)
        if str(entry.get("run_id") or "") != run_id
        and not str(entry.get("run_id") or "").endswith(suffix)
    ]
    entries.append(_summary({**run, "run_id": run_id}))
    write_json_atomic(index_path(settings, name), entries)

    latest_file = write_json_atomic(
        latest_path(settings, name), latest if latest is not None else run
    )
    return {"run": run_file, "latest": latest_file, "run_id": run_id}


def _remove_report_artefacts(settings, name: str, run_id: str) -> List[str]:
    """Delete a run's derived report output (a file, a directory, or neither).

    The report generator (plan task 21) owns this location, so it may not exist
    yet — a missing artefact is not an error."""
    target = reports_dir(settings, name, run_id)
    removed: List[str] = []
    for path in (target, target.with_name(target.name + ".json")):
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path))
            elif path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError:
            logger.warning("Could not remove report artefact %s", path)
    return removed


def delete_run(settings, name: str, run_id: str) -> dict:
    """Remove ONE stored run: its file, its index record, its report output.

    ``latest.json`` is deliberately left alone — the panel's pinned view is the
    caller's business (``backtest_service.forget_run`` repoints it), because only
    the web layer knows how to build the trimmed UI view.

    Returns ``{"deleted": bool, "run_id": str, "removed": [paths]}``; a run that
    is not on disk reports ``deleted: False`` instead of raising."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return {"deleted": False, "run_id": None, "removed": []}

    removed: List[str] = []
    run_file = run_path(settings, name, run_id)
    if run_file.exists():
        try:
            run_file.unlink()
            removed.append(str(run_file))
        except OSError:
            logger.warning("Could not remove run file %s", run_file)
    removed.extend(_remove_report_artefacts(settings, name, run_id))

    # Drop the menu record too, or the index would keep advertising a run whose
    # file no longer exists (list_runs tolerates that, but it must not grow).
    before = _load_index(settings, name)
    after = [e for e in before if str(e.get("run_id") or "") != run_id]
    if len(after) != len(before):
        write_json_atomic(index_path(settings, name), after)

    deleted = bool(removed) or len(after) != len(before)
    if deleted:
        logger.info("Deleted backtest run %s for %s (%s)", run_id, name, removed or "index only")
    return {"deleted": deleted, "run_id": run_id, "removed": removed}


def list_runs(settings, name: str) -> List[dict]:
    """Every stored run for the strategy, newest first (menu records only).

    The index is the fast path. Extra run files that are not in it (e.g. copied
    in by hand) are summarised on the fly so they still appear."""
    directory = runs_dir(settings, name)
    entries: List[dict] = []
    seen = set()

    for entry in _load_index(settings, name):
        run_id = str(entry.get("run_id") or "")
        if not run_id or run_id in seen:
            continue
        if not run_path(settings, name, run_id).exists():
            continue  # deleted behind our back
        seen.add(run_id)
        entries.append(entry)

    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            run_id = path.stem
            if run_id in seen:
                continue
            data = _read_json(path)
            if isinstance(data, dict):
                seen.add(run_id)
                entries.append(_summary({**data, "run_id": run_id}))

    # run ids start with a UTC stamp, so descending lexical order is newest-first
    entries.sort(key=lambda e: str(e.get("run_id") or ""), reverse=True)
    return entries


def load_run(settings, name: str, run_id: str) -> Optional[dict]:
    """One full run by id (None when unknown)."""
    if not run_id:
        return None
    data = _read_json(run_path(settings, name, run_id))
    return data if isinstance(data, dict) else None


def load_latest(settings, name: str) -> Optional[dict]:
    """The strategy's last persisted panel view, or ``None``.

    Falls back to the legacy flat file so results written before this layout
    still show up in the panel."""
    for path in (latest_path(settings, name), _legacy_path(settings, name)):
        data = _read_json(path)
        if isinstance(data, dict):
            return data
    return None
