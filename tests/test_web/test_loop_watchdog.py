"""The loop watchdog: an armed switch with no loop gets its runner back.

The gap this closes was a silent one. Trading ON means "a process is ticking this account", and
the arm is a FILE while the ticking is a PROCESS. When the process dies — a crash, an OOM kill, a
restart of the web app that took its child with it — the file still says ON, the lease still names
a holder, and ``loop_service`` even has a name for it (``overdue``: "nothing will tick again until
someone restarts it"). Nothing acted on that name. An armed account then waited for a bar that was
never going to be decided, while the dataset fell further behind, because fetching the missing
bars is a step OF a tick.

What is pinned here:

* the loop is put back when — and only when — trading is ON and no live process holds the lease;
* restoring the runner never touches what is armed: switching trading on is the operator's
  decision, and a watchdog that could arm an account would be a much worse bug than the one it
  fixes. Nothing is started while trading is off;
* a poll cannot storm it: the caller is a page reading the loop's state every twenty seconds, and
  a fork attempt per read would be a fork storm. Repeated asks are refused without starting
  anything, and the refusal says which of the ways it happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.config.settings import Settings
from src.config import trading_state
from src.web.services import loop_control, loop_service

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = "GPRO"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument=STRATEGY,
    )


@pytest.fixture()
def starts(monkeypatch) -> list:
    """Record every attempt to start the loop, instead of forking one in a test."""
    calls: list = []

    def start(settings, *args, **kwargs):
        calls.append(settings)
        return {"started": True, "running": True, "pid": 4242, "reason": "", "message": ""}

    monkeypatch.setattr(loop_control, "start", start)
    # The throttle is process state: a test that runs twice in a second would otherwise be
    # refused by the previous one's attempt.
    monkeypatch.setattr(loop_service, "_last_ensure", 0.0)
    return calls


def _arm(settings: Settings, on: bool = True) -> None:
    trading_state.write_state(settings, {
        "on": on, "env": "paper", "broker": "alpaca", "strategy": STRATEGY,
        "since": trading_state.now_iso() if on else None,
        "stopped_at": None if on else trading_state.now_iso(),
    })


def test_an_armed_switch_with_no_loop_gets_one_back(settings, starts):
    _arm(settings)

    result = loop_service.ensure_running(settings)

    assert result["ensured"] is True and result["started"] is True
    assert len(starts) == 1, "one loop, started once"
    assert result["state"] in (loop_service.STOPPED, loop_service.NEVER), result["state"]


def test_the_runner_is_restored_and_NOTHING_is_armed(settings, starts):
    """The switch is read, never written: a watchdog that could turn trading on would be a far
    worse failure than the one it exists to fix."""
    _arm(settings)
    before = trading_state.get_state(settings)

    loop_service.ensure_running(settings)

    assert trading_state.get_state(settings) == before
    assert trading_state.is_trading_on(settings) is True, "still armed, by the operator's own click"


def test_nothing_is_started_while_trading_is_off(settings, starts):
    _arm(settings, on=False)

    result = loop_service.ensure_running(settings)

    assert result["ensured"] is False and result["started"] is False
    assert result["reason"] == "trading is off"
    assert starts == [], "a stopped bot must stay stopped"


def test_a_running_loop_is_left_alone(settings, starts, monkeypatch):
    _arm(settings)
    monkeypatch.setattr(
        loop_service, "status",
        lambda settings, at=None: {"state": loop_service.RUNNING, "holder": {"pid": 1}},
    )

    result = loop_service.ensure_running(settings)

    assert result["ensured"] is False and result["reason"] == "the loop is already running"
    assert starts == []


def test_a_stalled_loop_is_reported_and_never_restarted(settings, starts, monkeypatch):
    """Alive is not the same as working, and the watchdog may not act on the difference.

    The lease belongs to a process that IS running, so a second loop is exactly the double order
    this project refuses to risk — and a restart is not what a pid is evidence for anyway: it
    says something is there, not what it is doing. So the state is reported instead, on every
    poll, in the loop's own words.
    """
    _arm(settings)
    monkeypatch.setattr(
        loop_service, "status",
        lambda settings, at=None: {"state": loop_service.STALLED, "late_by_seconds": 38 * 60,
                                   "holder": {"pid": 1}, "claim": {"pid": 1}},
    )

    result = loop_service.ensure_running(settings)

    assert result["ensured"] is False and result["started"] is False
    assert result["state"] == loop_service.STALLED
    assert "past the wake" in result["reason"]
    assert "stuck" in result["message"]
    assert starts == [], "a stalled loop still HOLDS the lease — a second one would double orders"

    again = loop_service.ensure_running(settings)
    assert again["reason"] == result["reason"], "the throttle must not silence the diagnosis"


def test_a_poll_cannot_storm_the_fork(settings, starts):
    """Called from a page that reads the loop's state every twenty seconds."""
    _arm(settings)

    first = loop_service.ensure_running(settings)
    second = loop_service.ensure_running(settings)

    assert first["started"] is True
    assert second["ensured"] is False and second["reason"] == "asked again too soon"
    assert len(starts) == 1, "one attempt, however many times the page asks"

    # ...and the throttle expires: a loop that keeps dying is retried, not given up on.
    later = loop_service.ensure_running(
        settings,
        at=datetime.now(timezone.utc)
        + timedelta(seconds=loop_service.ENSURE_MIN_INTERVAL_SECONDS + 1),
    )
    assert later["started"] is True
    assert len(starts) == 2


def test_a_start_that_fails_is_reported_rather_than_raised(settings, monkeypatch):
    """The operator needs to read why; a polling page must not be handed an exception."""
    _arm(settings)
    monkeypatch.setattr(loop_service, "_last_ensure", 0.0)
    monkeypatch.setattr(
        loop_control, "start",
        lambda settings: {"started": False, "running": False, "pid": None,
                          "reason": "no interpreter", "message": "no interpreter at .venv"},
    )

    result = loop_service.ensure_running(settings)

    assert result["ensured"] is True and result["started"] is False
    assert "no interpreter" in result["message"]


# ---------------------------------------------------------------------------
# the wiring: the route, and the page that asks for it
# ---------------------------------------------------------------------------
def test_the_route_is_a_POST_because_it_starts_a_process():
    src = (ROOT / "src" / "web" / "routes" / "live.py").read_text(encoding="utf-8")

    assert '@router.post("/loop/ensure")' in src
    assert "loop_service.ensure_running(settings)" in src
    # The READ stays a read: a GET that forks a process is a GET nobody can trust.
    assert '@router.get("/loop")' in src
    get_loop = src[src.index('@router.get("/loop")') : src.index('@router.post("/loop/ensure")')]
    assert "ensure_running" not in get_loop


def test_the_monitor_asks_for_the_loop_to_come_back_and_then_re_reads_it():
    """Order matters: the state is read again BEFORE rendering, or the page would show the
    pre-restart truth for another twenty seconds and the roadmap would look unchanged."""
    log_js = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")

    status = log_js[log_js.index("async function loadStatus()") :][:1600]
    assert "await ensureLoopRunning()" in status
    assert status.index("await ensureLoopRunning()") < status.index("renderLoopState()")
    assert 'api("/api/v1/loop", { method: "POST" })' not in status, "the READ is still a read"

    ensure = log_js[log_js.index("async function ensureLoopRunning()") :][:900]
    assert '"/api/v1/loop/ensure"' in ensure
    assert 'method: "POST"' in ensure
    assert 'loop.state === "running"' in ensure, "a healthy loop is not asked about"

    # And the line that says nothing is ticking, in the state that used to be silent.
    assert "Trading is armed, but no loop is running — nothing will tick." in log_js
