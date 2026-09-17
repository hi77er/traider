"""Hosting the loop: the lease comes first, the report comes second, and nothing trades.

``src/main.py`` is thin on purpose — argv, the host guard, exit codes — so the behaviour
worth testing lives here, in ``src/scheduler/host.py``: claiming the lease, saying what the
loop inherited, and giving the lease back however the run ends.

The run itself is not re-tested here. These tests use the real ``orchestrator.run`` for the
one case that is safe and cheap (trading OFF stops at the first gate), and replace it with
something that explodes when what is being tested is the ORDER — claim, then report, then
tick — because that order is the only thing that makes "exactly one loop" true.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from src.config.settings import Settings
from src.config.trading_state import write_state
from src.execution import store
from src.scheduler import host, lease as lease_mod, orchestrator


def _settings(tmp_path, **kwargs) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        live_dir=str(tmp_path / "data" / "live_results"),
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
        trading_start_hour="09:30",
        trading_end_hour="16:00",
    )
    values.update(kwargs)
    return Settings(**values)


def _hold_the_lease(settings, *, pid: int = 1) -> None:
    """Put a lease on disk held by a process that is alive and is not this one.

    pid 1 is used rather than a fake: ``os.kill(1, 0)`` raises PermissionError for an
    unprivileged caller, which the lease reads as "it exists, it is simply not mine" — the
    real behaviour, not a monkeypatched one.
    """
    path = lease_mod.lease_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": lease_mod._this_host(),
                "started": "2026-09-17T07:00:00+00:00",
                "heartbeat": "2026-09-17T07:00:00+00:00",
                "next_wake": "2026-09-17T08:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "strategy": "Epsilon",
            }
        ),
        encoding="utf-8",
    )


class _Driver:
    """A driver that answers reconcile with whatever the test wants to see."""

    def __init__(self, mismatch=None, error=None):
        self.mismatch = mismatch
        self.error = error
        self.asked = 0

    def reconcile(self):
        self.asked += 1
        if self.error is not None:
            raise self.error
        return self.mismatch


# ---------------------------------------------------------------------------
# the lease comes first
# ---------------------------------------------------------------------------
def test_a_second_loop_cannot_start_however_safe_its_tick_would_be(tmp_path):
    """The refusal is about the PROCESS, not about what this tick might have done.

    Even with trading OFF — where this tick would have placed nothing — a second loop must
    not start: the first one may be a moment away from arming, and "it would have been
    harmless this time" is not a property a lease can rely on.
    """
    settings = _settings(tmp_path)
    _hold_the_lease(settings)

    with pytest.raises(lease_mod.LeaseHeld):
        host.serve(settings, once=True)


def test_the_report_never_runs_when_the_lease_is_held(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _hold_the_lease(settings)
    reported = []

    with pytest.raises(lease_mod.LeaseHeld):
        host.serve(settings, once=True, report=reported.append)

    assert reported == [], "nothing inherited is reported by a process that is not starting"


def test_the_lease_is_given_back_when_the_run_blows_up(tmp_path, monkeypatch):
    """Otherwise the next start would have to notice this one died, and would refuse."""
    settings = _settings(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("the provider went away mid-tick")

    monkeypatch.setattr(host.orchestrator, "run", explode)

    with pytest.raises(RuntimeError):
        host.serve(settings, once=True)

    assert lease_mod.read(settings) is None
    assert not lease_mod.lease_path(settings).exists()


def test_a_run_leaves_the_lease_free_for_the_next_one(tmp_path):
    settings = _settings(tmp_path)

    records = host.serve(settings, once=True)

    assert [r["action"] for r in records] == ["off"], "trading is OFF in this fixture"
    assert lease_mod.read(settings) is None


def test_the_lease_is_held_for_the_whole_run_but_not_after_it(tmp_path, monkeypatch):
    """Seen from inside the loop: the claim is on disk while the tick runs."""
    settings = _settings(tmp_path)
    seen = {}

    real_run = orchestrator.run

    def watch(*args, **kwargs):
        seen["held"] = lease_mod.read(settings)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(host.orchestrator, "run", watch)
    host.serve(settings, once=True)

    assert seen["held"] is not None, "the loop ran without a claim on disk"
    assert seen["held"]["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
def test_the_report_says_trading_is_off_and_never_builds_a_driver(tmp_path, monkeypatch):
    """The common case must not need credentials to start a process."""
    settings = _settings(tmp_path)

    def no_driver(*_args, **_kwargs):
        raise AssertionError("a driver was built with trading OFF")

    monkeypatch.setattr(host, "build_driver", no_driver)

    lines = host.prepare(settings)

    assert len(lines) == 1
    assert "trading is OFF" in lines[0]


def test_the_report_retires_legacy_state(tmp_path):
    """Phase 3 built the move; Phase 5 is when a running loop actually performs it.

    The file sits directly in the DATA directory, not in ``live_results/``: that is where
    ``state_files.state_path`` used to put it, and the whole reason it has to be retired is
    that its location encodes the keying it had (instrument alone, no strategy, no env).
    """
    settings = _settings(tmp_path)
    legacy = tmp_path / "data" / "strategy_state_AMZN.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")

    lines = host.prepare(settings)

    assert any("legacy" in line for line in lines), lines
    assert not legacy.exists()
    moved = tmp_path / "data" / "live_results" / "_legacy" / legacy.name
    assert moved.exists(), "preserved, never deleted"
    assert any("_legacy" in line for line in lines), "the report says where it went"


def test_the_report_names_the_armed_strategy(tmp_path, monkeypatch):
    """The strategy comes from the switch's stamp, which is what the first tick will use."""
    settings = _settings(tmp_path)
    write_state(settings, {"on": True, "strategy": "Beta", "env": "paper"})
    asked = {}

    def fake_build(settings, *, name=None, **_kwargs):
        asked["name"] = name
        return _Driver()

    monkeypatch.setattr(host, "build_driver", fake_build)

    lines = host.prepare(settings)

    assert asked["name"] == "Beta"
    assert any("Beta" in line for line in lines)
    assert any("agree" in line for line in lines)


def test_a_disagreement_is_reported_and_does_not_stop_the_loop(tmp_path, monkeypatch):
    """Refusing to START would leave an operator with a process that will not run.

    The tick's own reconcile is what refuses to trade on it, and it says so every bar.
    """
    settings = _settings(tmp_path)
    write_state(settings, {"on": True, "strategy": "Beta", "env": "paper"})
    monkeypatch.setattr(
        host, "build_driver",
        lambda settings, **kwargs: _Driver(mismatch="the broker holds 4 shares we know nothing about"),
    )

    lines = host.prepare(settings)

    assert any("disagree" in line for line in lines), lines
    assert any("4 shares" in line for line in lines), lines


def test_an_unreachable_broker_is_reported_rather_than_fatal(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    write_state(settings, {"on": True, "strategy": "Beta", "env": "paper"})
    monkeypatch.setattr(
        host, "build_driver",
        lambda settings, **kwargs: _Driver(error=RuntimeError("no route to the broker")),
    )

    lines = host.prepare(settings)

    assert any("could not ask the broker" in line for line in lines), lines
    assert any("first tick will try again" in line for line in lines), lines


def test_housekeeping_never_stops_the_loop(tmp_path, monkeypatch):
    """A failed move must be a line in the log, not a process that will not run."""
    settings = _settings(tmp_path)

    def broken(_settings):
        raise OSError("read-only volume")

    monkeypatch.setattr(host.store, "retire_legacy_state", broken)

    lines = host.prepare(settings)

    assert any("could not retire legacy state" in line for line in lines), lines
