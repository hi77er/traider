"""Report data for the Web Portal's standalone backtest report page.

Three payloads:

* ``runs_payload(strategy)`` — the run menu: every stored run of the strategy,
  newest first (see ``src.backtest.store.list_runs``).
* ``report_payload(strategy, run_id)`` — the full report for ONE run (the newest
  when ``run_id`` is omitted), assembled by ``src.backtest.report.build_report``.
* ``delete_report(strategy, run_id)`` — remove ONE stored run from disk.

Nothing here starts a backtest — the page always reads what is already on disk,
so opening a report can never kick off work behind the user's back.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.backtest import store as bt_store
from src.backtest.report import build_report
from src.config.effective import active_strategy_name, get_effective_settings
from src.web.services import backtest_service

logger = logging.getLogger(__name__)


def _resolve_strategy(strategy: Optional[str]) -> Optional[str]:
    """An explicit ``?strategy=`` wins; otherwise fall back to the active one.

    The page is usually opened from the backtest panel, which passes the
    strategy explicitly so the report keeps referring to the strategy it was
    opened for even if the dashboard is switched to another one afterwards."""
    return (strategy or "").strip() or active_strategy_name()


def _empty(strategy: Optional[str], message: str, runs: Optional[list] = None) -> dict:
    return {
        "strategy": strategy,
        "run_id": None,
        "runs": runs or [],
        "report": None,
        "error": message,
    }


def runs_payload(strategy: Optional[str] = None) -> dict:
    """The run menu for one strategy."""
    name = _resolve_strategy(strategy)
    if not name:
        return _empty(None, "No strategy is active — create one in the dashboard first.")
    runs = bt_store.list_runs(get_effective_settings(), name)
    return {"strategy": name, "run_id": None, "runs": runs, "report": None, "error": None}


def report_payload(strategy: Optional[str] = None, run_id: Optional[str] = None) -> dict:
    """The full report for one run (newest by default) plus the run menu."""
    name = _resolve_strategy(strategy)
    if not name:
        return _empty(None, "No strategy is active — create one in the dashboard first.")

    settings = get_effective_settings()
    runs = bt_store.list_runs(settings, name)
    if not runs:
        return _empty(
            name,
            "No backtest runs recorded for this strategy yet — run a backtest to "
            "generate a report.",
        )

    wanted = (run_id or "").strip()
    stale = None
    chosen = None
    if wanted:
        chosen = next((r for r in runs if str(r.get("run_id")) == wanted), None)
        if chosen is None:
            # The run is gone (superseded by a re-run of the same inputs, or
            # deleted). A bookmarked/reloaded URL should still show something.
            stale = f"Run {wanted} is no longer stored — showing the latest run instead."
    if chosen is None:
        chosen = runs[0]  # newest first

    chosen_id = str(chosen.get("run_id"))
    run = bt_store.load_run(settings, name, chosen_id)
    if run is None:
        return _empty(name, "That run could not be read from disk.", runs)

    try:
        report = build_report(run)
    except Exception as exc:  # noqa: BLE001 - a malformed run must not 500 the page
        logger.exception("Could not build report for %s/%s", name, chosen_id)
        return _empty(name, f"Could not build the report: {exc}", runs)

    return {
        "strategy": name,
        "run_id": chosen_id,
        "runs": runs,
        "report": report,
        "error": stale,
    }


def delete_report(strategy: Optional[str] = None, run_id: Optional[str] = None) -> dict:
    """Delete ONE stored run of a strategy from disk.

    Removes the full run file, its menu-index record and any report output the
    generator produced; the panel's ``latest.json`` is repointed at the newest
    remaining run (or removed) so the deletion is not undone by the Backtest
    panel still showing it.

    Responds with ``{ok, deleted, removed, strategy, runs, message}`` — the page
    re-fetches whatever is left rather than receiving a whole report back.
    """
    name = _resolve_strategy(strategy)
    wanted = (run_id or "").strip()

    def fail(message: str) -> dict:
        runs = bt_store.list_runs(get_effective_settings(), name) if name else []
        return {
            "ok": False,
            "deleted": None,
            "removed": [],
            "strategy": name,
            "runs": runs,
            "message": message,
        }

    if not name:
        return fail("No strategy is active — nothing to delete.")
    if not wanted:
        return fail("No run was selected — nothing to delete.")

    settings = get_effective_settings()
    if bt_store.load_run(settings, name, wanted) is None:
        return fail(f"Run {wanted} is not on disk — it may already have been deleted.")

    result = bt_store.delete_run(settings, name, wanted)
    if not result.get("deleted"):
        return fail(f"Run {wanted} could not be deleted.")

    # The panel must not keep displaying what was just removed.
    backtest_service.forget_run(settings, name, wanted)

    runs = bt_store.list_runs(settings, name)
    logger.info("Deleted report %s for %s (%d run(s) left)", wanted, name, len(runs))
    return {
        "ok": True,
        "deleted": wanted,
        "removed": result.get("removed") or [],
        "strategy": name,
        "runs": runs,
        "message": (
            None
            if runs
            else "That was the last stored report for this strategy — run a "
            "backtest to create a new one."
        ),
    }
