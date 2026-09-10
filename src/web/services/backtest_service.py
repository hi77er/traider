"""Backtest business logic for the Web Portal.

Runs the pure ``src.backtest.engine.run_backtest`` against the ACTIVE
strategy's current effective settings (rules, config, instrument, bar size) in
a background thread, mirroring the backfill/rebuild job pattern, so the UI can
poll status and render the latest result.

The result of each run is stored per strategy by ``src.backtest.store``:
  * the FULL run (every equity point, every trade, plus the ``inputs``
    provenance block) is written to ``runs/<run_id>.json``, and
  * the bounded, UI-shaped view the panel reads is refreshed as ``latest.json``.
The latest view is also cached in memory (``_RESULTS``, keyed by strategy name)
so polling does not re-read the file. Only successful runs are written, so a bad
run (wrong model, empty data) never clobbers the last good result.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

from src.backtest import store as bt_store
from src.backtest.engine import run_backtest
from src.backtest.store import jsonable
from src.config.effective import active_strategy_name, get_effective_settings

logger = logging.getLogger(__name__)

# In-process backtest job state (single-worker bot).
_JOB_LOCK = threading.Lock()
_JOB: Dict[str, object] = {"running": False, "last_error": None, "last_run": None}
# Last completed engine result per strategy name (mirror of the JSON on disk,
# so ``payload()`` doesn't re-read the file on every UI poll).
_RESULTS: Dict[str, dict] = {}


# Bounded sizes for the panel's copy of a run. The full run stays on disk, so
# this view is deliberately lossy: enough to draw the sparkline and place the
# markers, NOT enough to report from.
MAX_CURVE_POINTS = 600
MAX_TRADE_ROWS = 200
# Shape version of the persisted payload (bumped when keys move/change meaning).
SCHEMA_VERSION = 2


def _thin(points: list, limit: int) -> list:
    """Evenly spaced subsample of a series, always keeping the last point."""
    if len(points) <= limit:
        return points
    step = math.ceil(len(points) / limit)
    thinned = points[::step]
    if thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned


def _ui_view(result: dict) -> dict:
    """Trimmed copy of a run for the panel.

    ``equity_curve`` and ``benchmark_curve`` are reduced to at most
    ``MAX_CURVE_POINTS`` evenly spaced points (the last one is always kept, so
    the final value stays exact) and ``trades`` to the most recent
    ``MAX_TRADE_ROWS``. A ``view`` block records the totals and points at the
    full run file, which is what reports read."""
    view = dict(result)

    curve = list(result.get("equity_curve") or [])
    shown = _thin(curve, MAX_CURVE_POINTS)
    view["equity_curve"] = shown
    view["benchmark_curve"] = _thin(list(result.get("benchmark_curve") or []), MAX_CURVE_POINTS)

    trades = list(result.get("trades") or [])
    view["trades"] = trades[-MAX_TRADE_ROWS:]
    run_id = result.get("run_id")
    view["view"] = {
        "curve_points": len(shown),
        "curve_points_total": len(curve),
        "trade_rows": len(view["trades"]),
        "trade_rows_total": len(trades),
        "truncated": len(shown) != len(curve) or len(view["trades"]) != len(trades),
        # The complete, untruncated record (reports read this one).
        "full_run": f"runs/{run_id}.json" if run_id else None,
    }
    return view


def _persist_run(settings, name: str, run: dict) -> None:
    """Persist a successful run: full detail under ``runs/`` + ``latest.json``.

    A persistence failure must never break the run, so everything is guarded."""
    try:
        paths = bt_store.save_run(settings, name, run, _ui_view(run))
        logger.info(
            "Saved backtest run %s for %s -> %s", run.get("run_id"), name, paths["run"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist backtest run for %s", name)


def forget_run(settings, name: str, run_id: str) -> None:
    """Make the panel forget a run that was deleted from disk.

    Called by the report page after a deletion, so a deleted report cannot keep
    showing in the Backtest panel. When the panel was pinned to that run,
    ``latest.json`` is repointed at the newest remaining run (rebuilt as the
    trimmed UI view); when no run is left the file is removed and the panel falls
    back to its how-it-works guide. Never raises — the deletion itself is what
    matters, and a stale panel must not fail the request.
    """
    try:
        cached = _RESULTS.get(name) or {}
        if str(cached.get("run_id") or "") == run_id:
            _RESULTS.pop(name, None)

        pinned_id = str((bt_store.load_latest(settings, name) or {}).get("run_id") or "")
        if pinned_id and pinned_id != run_id:
            return  # the panel is showing a different run — leave it alone

        runs = bt_store.list_runs(settings, name)
        latest = bt_store.latest_path(settings, name)
        if not runs:
            latest.unlink(missing_ok=True)
            return
        run = bt_store.load_run(settings, name, str(runs[0].get("run_id") or ""))
        if run:
            bt_store.write_json_atomic(latest, _ui_view(run))
    except Exception:  # noqa: BLE001 - a panel refresh must never break a delete
        logger.exception("Could not refresh the panel's latest view for %s", name)


def _load_latest(settings, name: str) -> Optional[dict]:
    """The strategy's last persisted panel view (None when absent/corrupt)."""
    try:
        return bt_store.load_latest(settings, name)
    except Exception:  # noqa: BLE001
        logger.warning("Could not load backtest result for %s", name, exc_info=True)
        return None


def _snapshot_locked() -> dict:
    """Read job state - caller must already hold ``_JOB_LOCK``."""
    return {
        "running": bool(_JOB["running"]),
        "last_error": _JOB["last_error"],
        "last_run": _JOB["last_run"],
    }


def payload() -> dict:
    """Job status + the last backtest result for the CURRENT active strategy.

    The result is served from the in-memory per-strategy cache or, on a fresh
    process / after a reload, from ``data/backtest_results/<strategy>/latest.json``
    (falling back to the legacy flat file). ``result`` is None when the current
    strategy has no recorded run yet (the panel then shows the guide).

    ``run_count`` is how many complete runs are stored for the strategy — the
    panel offers the full-report button only when there is at least one."""
    with _JOB_LOCK:
        status = _snapshot_locked()
    result = None
    run_count = 0
    name = active_strategy_name()
    if name:
        settings = get_effective_settings()
        result = _RESULTS.get(name)
        if result is None:
            result = _load_latest(settings, name)
            if result is not None:
                _RESULTS[name] = result
        # Reads the (small) run index, not the run files themselves.
        run_count = len(bt_store.list_runs(settings, name))
    return {"status": status, "result": result, "run_count": run_count}


def start_backtest() -> dict:
    """Kick off a backtest of the ACTIVE strategy (one at a time)."""
    settings = get_effective_settings()
    name = active_strategy_name()
    with _JOB_LOCK:
        if _JOB["running"]:
            return {
                "started": False,
                "reason": "a backtest is already running",
                "status": _snapshot_locked(),
            }
        _JOB["running"] = True
        _JOB["last_error"] = None
        _JOB["last_run"] = None

    logger.info(
        "Starting backtest for strategy %r (%s %s, model=%s)",
        name,
        settings.instrument,
        settings.historical_bar_size,
        settings.model_type,
    )
    threading.Thread(target=_run, args=(settings, name), daemon=True).start()
    with _JOB_LOCK:
        return {"started": True, "status": _snapshot_locked()}


def _run(settings, name: Optional[str]) -> None:
    """Run the engine, cache the outcome and persist it for the strategy."""
    try:
        result = run_backtest(settings)
        with _JOB_LOCK:
            _JOB["last_error"] = result.get("error")  # ok=False carries a reason
        logger.info(
            "Backtest finished for %s: ok=%s error=%s",
            settings.instrument,
            result.get("ok"),
            result.get("error"),
        )
    except Exception as exc:  # noqa: BLE001 - engine crash shouldn't kill the bot
        logger.exception("Backtest crashed for %s", settings.instrument)
        result = {
            "ok": False,
            "error": f"Backtest crashed: {exc}",
            "symbol": getattr(settings, "instrument", None),
            "bar_size": getattr(settings, "historical_bar_size", None),
            "model_type": getattr(settings, "model_type", None),
            "metrics": None,
            "gate": None,
            "equity_curve": [],
            "benchmark_curve": [],
            "trades": [],
            "notes": [],
        }
        with _JOB_LOCK:
            _JOB["last_error"] = str(exc)

    # Cache in memory; only successful runs overwrite the persisted files so a
    # bad run (wrong model, empty data) can't clobber the last good result.
    # Happens BEFORE the running flag clears so any poll that observes
    # "not running" already sees a complete result.
    if name:
        # Strip numpy/pandas scalars so the dict is both JSON-serializable for
        # the files AND strictly serializable by the FastAPI ``dict`` response.
        result = jsonable(result)
        result["schema_version"] = SCHEMA_VERSION
        result["run_id"] = bt_store.new_run_id(result.get("inputs") or {})
        result["strategy"] = result.get("strategy") or name
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        # The panel gets a bounded view; the full run goes to ``runs/``.
        _RESULTS[name] = _ui_view(result)
        if result.get("ok") is True:
            _persist_run(settings, name, result)

    with _JOB_LOCK:
        _JOB["running"] = False
        _JOB["last_run"] = datetime.now(timezone.utc).isoformat()
