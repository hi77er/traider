"""The dashboard's read of the loop: what state it is in, and the endpoints that serve it.

The interesting case is not "is there a lease file" but telling four situations apart that a
timestamp alone cannot: never ran, stopped cleanly, died (claim present, nobody honouring
it), and running. Getting that wrong shows a crashed loop as healthy, which is the failure
the whole design exists to prevent — so the state machine is tested rather than the file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.config import loop_state, state_files
from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.execution import store
from src.scheduler import lease as lease_mod
from src.web.app import app
from src.web.services import loop_service

client = TestClient(app)


def _settings(tmp_path, **kwargs) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        live_dir=str(tmp_path / "data" / "live_results"),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
    )
    values.update(kwargs)
    return Settings(**values)


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """A tmp account, with the switch stamped so no test touches the real strategy store."""
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)

    def build(**kw):
        settings = _settings(tmp_path, **kw)
        write_state(settings, {"on": True, "strategy": "Alpha", "env": "paper"})
        return settings

    return build


def _tick_at(settings, *, action="decided", reason="", seconds_ago=0, name="Alpha"):
    """Record a tick the way the loop does: the heartbeat AND a line in the day's log."""
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    record = store.tick_record(
        strategy=name, env="paper", action=action, reason=reason, settings=settings, at=at
    )
    store.save_latest(settings, name, record)
    store.append_tick(settings, name, record, when=at)
    return record


# ---------------------------------------------------------------------------
# the four states
# ---------------------------------------------------------------------------
def test_nothing_has_ever_run(tmp_path, armed):
    status = loop_service.status(armed())

    assert status["state"] == loop_service.NEVER
    assert status["has_run"] is False
    assert status["claim"] is None and status["holder"] is None
    assert status["last_tick_age_seconds"] is None
    assert status["last_action"] is None


def test_a_clean_shutdown_is_stopped_not_overdue(tmp_path, armed):
    """The lease is released on the way out, so "nothing is holding it" is expected here.

    Reporting an ordinary stop as a crash is how an operator learns to ignore the warning.
    """
    settings = armed()
    _tick_at(settings, seconds_ago=120)

    status = loop_service.status(settings)

    assert status["state"] == loop_service.STOPPED
    assert status["has_run"] is True
    assert status["last_tick_age_seconds"] == pytest.approx(120, abs=5)


def test_a_claim_nobody_is_honouring_is_overdue(tmp_path, armed):
    """The crash case: the file says a loop meant to act, the process table says otherwise."""
    settings = armed()
    claim = lease_mod.acquire(settings, strategy="Alpha")
    lease_mod.refresh(claim, next_wake=datetime.now(timezone.utc) + timedelta(minutes=30))
    # The holder "dies": its pid is gone, but its claim is still on disk.
    record = dict(loop_state.read(settings))
    record["pid"] = 2 ** 30
    state_files.write_json(loop_state.lease_path(settings), record)

    status = loop_service.status(settings)

    assert status["state"] == loop_service.OVERDUE
    assert status["claim"] is not None, "the file's own account of what it was doing"
    assert status["holder"] is None, "but nothing is honouring it"
    assert status["next_wake"] == record["next_wake"]


def test_a_live_holder_is_running(tmp_path, armed):
    settings = armed()
    claim = lease_mod.acquire(settings, strategy="Alpha")
    lease_mod.refresh(claim, next_wake=datetime.now(timezone.utc) + timedelta(minutes=30))

    status = loop_service.status(settings)

    assert status["state"] == loop_service.RUNNING
    assert status["holder"] is not None and status["holder"]["pid"] == claim.pid
    assert "pid" in status["holder_text"]


def test_the_age_is_measured_from_the_ticks_own_moment(tmp_path, armed):
    """Not from when the file was written: a replayed or backfilled tick says when it was."""
    settings = armed()
    _tick_at(settings, seconds_ago=3600)

    assert loop_service.status(settings)["last_tick_age_seconds"] == pytest.approx(3600, abs=10)


def test_the_last_refusal_comes_from_the_log_not_the_last_tick(tmp_path, armed):
    """The last tick may have been a success, and "why did nothing happen at 14:30" wants
    the last thing that WENT WRONG rather than the last thing that went right."""
    settings = armed()
    _tick_at(settings, action="refused", reason="stale bar", seconds_ago=1800)
    _tick_at(settings, action="decided", seconds_ago=60)

    status = loop_service.status(settings)

    assert status["last_action"] == "decided", "the log holds a declined tick AFTER the refusal"
    assert status["last_refusal"]["reason"] == "stale bar"


def test_a_log_that_cannot_be_read_is_not_a_failure(tmp_path, armed, monkeypatch):
    """A deleted or unreadable log degrades to "no refusal known", never to an exception."""
    settings = armed()
    _tick_at(settings)

    def broken(*_args, **_kwargs):
        raise OSError("the log is on a volume that went away")

    monkeypatch.setattr(loop_service.store, "read_ticks", broken)

    assert loop_service.status(settings)["last_refusal"] is None


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    """Point the whole app at a tmp account for the duration of one test.

    ``active_strategy_name`` is stubbed as well, because it reads the REAL strategy store:
    without this a test would answer with whatever strategy the machine running the suite
    happens to have active, which is the kind of thing that passes on one laptop and fails
    on another.
    """
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    settings = _settings(tmp_path)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.clear()


def test_the_loop_endpoint_answers_with_nothing_on_disk(api_settings):
    response = client.get("/api/v1/loop")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == loop_service.NEVER
    assert body["has_run"] is False
    assert body["env"] == "paper"


def test_the_positions_endpoint_counts_what_is_held(api_settings):
    """Both accounts, and the count comes from the same cached reader the trading gate uses."""
    response = client.get("/api/v1/positions")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["instrument"] == "AAPL"
    assert body["open_count"] == 0 and body["unknown_count"] == 0
    assert isinstance(body["positions"], list)


def test_the_orders_endpoint_degrades_without_credentials(api_settings):
    """No keys is the ordinary state of a fresh install, and it must render as that."""
    response = client.get("/api/v1/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "message" in body
    assert body["open"] == [] and body["closed"] == [] and body["resting"] == []


def test_the_orders_endpoint_degrades_when_the_broker_cannot_be_read(api_settings, monkeypatch):
    """A configured account and an unreachable broker must render, not 500.

    The 500 would blank the panel at exactly the moment it has something to say, and the
    dashboard is the window you debug a broken loop through — it has to survive one.
    """
    from src.web.routes import live as live_routes

    monkeypatch.setattr(live_routes, "execution_status", lambda settings: {"ok": True, "env": "paper"})

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the broker is down")

    monkeypatch.setattr(live_routes.positions, "executor_for", refuse)

    response = client.get("/api/v1/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and "the broker is down" in body["message"]
    assert body["open"] == [] and body["closed"] == []


def test_an_order_view_keeps_the_legs_one_level_deep(api_settings):
    """A bracket's protection is its children, and an operator asking whether a position is
    protected is looking for them. The projection is deliberate: the raw payload carries
    account-level detail that has no business being proxied into a browser."""
    from src.web.routes.live import _order_view

    view = _order_view(
        {
            "id": "ord-1", "client_order_id": "traider-A-b3", "side": "buy",
            "status": "filled", "qty": "10", "filled_avg_price": "101.50",
            "id": "ord-1", "account_number": "PA36Z5MM1R9V", "secret": "nope",
            "legs": [{"id": "leg-stop", "type": "stop", "stop_price": "96.00", "account_number": "PA1"}],
        }
    )

    assert view["id"] == "ord-1" and view["client_order_id"] == "traider-A-b3"
    assert view["legs"][0]["stop_price"] == "96.00"
    assert "account_number" not in view and "secret" not in view
    assert "account_number" not in view["legs"][0]


def test_the_trades_endpoint_is_empty_before_anything_trades(api_settings):
    response = client.get("/api/v1/trades")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "strategy": "AAPL", "count": 0, "trades": []}


def test_the_trades_endpoint_returns_the_newest_first(api_settings):
    settings = api_settings
    write_state(settings, {"on": True, "strategy": "Alpha", "env": "paper"})
    for index in range(2):
        store.append_trade(
            settings, "Alpha",
            store.trade_record(
                settings=settings, strategy="Alpha", env="paper",
                at=datetime.now(timezone.utc), leg={"entry_idx": index, "reason": f"r{index}"},
            ),
        )

    body = client.get("/api/v1/trades").json()

    assert body["strategy"] == "Alpha" and body["count"] == 2
    assert [t["reason"] for t in body["trades"]] == ["r1", "r0"], "newest first"


# ---------------------------------------------------------------------------
# is the open position protected?
# ---------------------------------------------------------------------------
def _hold(settings, *, stop=96.0, take=104.0, name="Alpha", env="paper"):
    """Write the driver's state file as if a position were open at these levels."""
    state_files.write_json(
        store.state_path(settings, name, env),
        {
            "position": {
                "entry_index": 3, "entry_price": 100.0, "raw_entry_price": 100.0,
                "short": False, "stop": stop, "take": take, "weight": 0.5, "stop_pct": 2.0,
            },
            "bar_index": 3, "last_decided_bar": "2026-09-16T17:30:00+00:00",
        },
    )


def _resting(stop=96.0, take=104.0):
    legs = []
    if stop is not None:
        legs.append({"id": "leg-stop", "type": "stop", "stop_price": f"{stop:.2f}"})
    if take is not None:
        legs.append({"id": "leg-take", "type": "limit", "limit_price": f"{take:.2f}"})
    return legs


def test_nothing_held_needs_no_protection(tmp_path, armed):
    settings = armed()

    verdict = loop_service.protection(settings, env="paper", legs=[])

    assert verdict["state"] == loop_service.NO_POSITION
    assert verdict["uncovered"] == []


def test_a_position_whose_levels_are_resting_is_protected(tmp_path, armed):
    settings = armed()
    _hold(settings)

    verdict = loop_service.protection(settings, env="paper", legs=_resting())

    assert verdict["state"] == loop_service.PROTECTED
    assert verdict["uncovered"] == []
    assert verdict["levels"]["stop"]["ok"] is True and verdict["levels"]["take"]["ok"] is True


def test_a_position_with_NO_exits_configured_is_naked_by_design(tmp_path, armed):
    """Not a failure: a strategy configured with no stop gets no bracket, deliberately.

    Warning here is how the warning that matters gets ignored.
    """
    settings = armed()
    _hold(settings, stop=None, take=None)

    verdict = loop_service.protection(settings, env="paper", legs=[])

    assert verdict["state"] == loop_service.NAKED
    assert "asked for" in verdict["message"]


def test_a_configured_stop_with_no_leg_resting_is_UNPROTECTED(tmp_path, armed):
    """The silent one: a local level with no order behind it protects nothing.

    A stop cancelled at the broker, or an amendment that was rejected, leaves exactly this —
    a position the machine believes is protected and the account does not.
    """
    settings = armed()
    _hold(settings)

    # Only the TARGET is still resting: the stop has gone missing, which is the dangerous
    # half — an unprotected long has unlimited downside and a finite upside.
    verdict = loop_service.protection(settings, env="paper", legs=_resting(stop=None))

    assert verdict["state"] == loop_service.UNPROTECTED
    assert verdict["uncovered"] == ["stop"]
    assert "NOT protected" in verdict["message"]
    assert "96.00" in verdict["message"]
    assert verdict["levels"]["take"]["ok"] is True, "the target is still fine"


def test_a_stop_that_DRIFTED_is_not_the_same_level(tmp_path, armed):
    """A stop that moved is a stop in the wrong place, and the risk per trade is not the one
    the position was sized for — so "close enough" is the wrong test."""
    settings = armed()
    _hold(settings, stop=96.0)

    verdict = loop_service.protection(settings, env="paper", legs=_resting(stop=93.0))

    assert verdict["state"] == loop_service.UNPROTECTED
    assert verdict["uncovered"] == ["stop"]


def test_a_level_that_is_only_rounded_is_still_the_same_level(tmp_path, armed):
    """The tolerance exists for float noise and broker rounding, not for drift."""
    settings = armed()
    _hold(settings, stop=96.0)

    verdict = loop_service.protection(settings, env="paper", legs=_resting(stop=96.001))

    assert verdict["state"] == loop_service.PROTECTED


def test_a_stop_limit_leg_classifies_as_a_stop(tmp_path, armed):
    """It carries both prices, so reading a price field would classify it by accident."""
    settings = armed()
    _hold(settings, stop=95.0, take=None)

    verdict = loop_service.protection(
        settings, env="paper",
        legs=[{"type": "stop_limit", "stop_price": "95.00", "limit_price": "94.00"}],
    )

    assert verdict["state"] == loop_service.PROTECTED


def test_the_verdict_asks_the_LOCAL_record_not_the_current_configuration(tmp_path, armed):
    """A configuration edited since the position opened must not make an unprotected
    position look protected: what matters is the levels this position was SIZED for."""
    settings = armed()
    _hold(settings, stop=96.0, take=104.0)

    verdict = loop_service.protection(settings, env="paper", legs=_resting())

    assert verdict["levels"]["stop"]["wanted"] == 96.0, "the levels on the position, not settings"


def test_the_position_is_read_for_the_account_the_panel_is_pointed_at(tmp_path, armed):
    """Paper and live are different accounts holding different things."""
    settings = armed()
    _hold(settings, stop=96.0, take=104.0, env="paper")

    paper = loop_service.protection(settings, env="paper", legs=_resting())
    live = loop_service.protection(settings, env="live", legs=_resting())

    assert paper["state"] == loop_service.PROTECTED
    assert live["state"] == loop_service.NO_POSITION, "the live account holds nothing here"


def test_the_orders_endpoint_reports_the_verdict_even_with_no_broker(api_settings):
    """An unreachable broker must not blank the one line that says the stop is missing."""
    write_state(api_settings, {"on": True, "strategy": "Alpha", "env": "paper"})
    _hold(api_settings)

    body = client.get("/api/v1/orders").json()

    assert body["ok"] is False, "no credentials in this fixture"
    assert body["protection"]["state"] == loop_service.UNPROTECTED
    assert body["protection"]["uncovered"] == ["stop", "take"]
