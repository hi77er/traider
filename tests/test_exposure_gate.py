"""The exposure gates — what stops the bot being armed on top of a position.

Four actions can strand a position: arming the bot, switching the environment, changing the
active strategy, and deleting it. All four ask the broker first, and the point of these
tests is the direction the answer fails in.

The interesting cases are not "a position is open" but the two that look similar and are
not:

* an account with **no** credentials is provably flat (no order could have been placed
  there), so a data-only install keeps working;
* an account that is configured but **unreachable** is UNKNOWN, and unknown refuses —
  because a 401 and an empty account are indistinguishable from the outside.

The broker is never contacted: ``account`` stands in for it, and ``tests/conftest.py``
patches the same door for every test in the suite.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import state_files
from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.execution import credentials, positions
from src.execution.alpaca_client import AlpacaError
from src.web.app import app
from src.web.services import trading_service

PAPER_KEYS = {"alpaca_paper_api_key": "PK-PAPER", "alpaca_paper_api_secret": "S-PAPER"}
LIVE_KEYS = {"alpaca_live_api_key": "PK-LIVE", "alpaca_live_api_secret": "S-LIVE"}


def _s(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        strategy_rules_file=str(tmp_path / "active.json"),
        **kwargs,
    )


def _position(symbol="AAPL", qty="90", side="long"):
    return {
        "symbol": symbol, "qty": qty, "side": side,
        "avg_entry_price": "100.00", "market_value": "9000.00", "unrealized_pl": "12.00",
    }


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(state_files, "state_path", lambda s, name: tmp_path / name)
    return tmp_path / "trading.json"


@pytest.fixture
def verified(monkeypatch):
    """A PASSING credential verdict for an environment, offline.

    Arming re-checks the keys against the broker every time, so any test that arms needs
    this — patched at the same door ``test_trading_gate`` uses.
    """

    def probe(key_id, secret, base_url, timeout=0):
        return {"ok": True, "message": "accepted", "account_number": "A1", "status": "ACTIVE"}

    monkeypatch.setattr(credentials, "probe", probe)

    def verify(settings, env="paper", ok=True):
        return credentials.verify(settings, env, force=True)

    return verify


@pytest.fixture
def account(monkeypatch):
    """Stands in for the broker: what each account holds, and how a flatten behaves.

    One fixture for both doors, so the two can never disagree — a test that says "the
    paper account holds 90 AAPL" is then true of the gate AND of the flatten.
    """
    state = {
        "paper": [], "live": [], "error": None,
        "exits": [], "flatten_fails": False, "flattened": [],
    }

    def probe(viewed, env):
        if state["error"]:
            raise AlpacaError(state["error"])
        return list(state[env])

    class FakeExecutor:
        def resting_exits(self, instrument=None):
            return list(state["exits"])

        def flatten(self, symbol, reason=""):
            if state["flatten_fails"]:
                raise AlpacaError("the broker refused to close it")
            state["flattened"].append(symbol)
            return {"was_flat": False, "order_id": "ord-1", "status": "filled"}

    monkeypatch.setattr(positions, "probe", probe)
    monkeypatch.setattr(positions, "executor_for", lambda settings, env: FakeExecutor())
    return state


# ---------------------------------------------------------------------------
# the read: three ways to be flat, and one way to be unknown
# ---------------------------------------------------------------------------
def test_no_credentials_is_flat_by_construction_and_costs_no_call(tmp_path, monkeypatch):
    """A key-less account cannot hold anything, so it is proven flat, not assumed flat."""
    calls = []
    monkeypatch.setattr(positions, "probe", lambda viewed, env: calls.append(env) or [])

    answer = positions.snapshot(_s(tmp_path), "paper")

    assert answer.flat is True
    assert answer.by_construction is True
    assert calls == [], "an account with no key must not be contacted at all"
    assert "no paper credentials" in answer.reason


def test_a_configured_account_is_asked(tmp_path, account):
    account["paper"] = [_position()]
    answer = positions.snapshot(_s(tmp_path, **PAPER_KEYS), "paper")
    assert answer.known is True and answer.flat is False and answer.count == 1
    assert answer.by_construction is False


def test_an_unreachable_account_is_unknown_and_never_flat(tmp_path, account):
    """The distinction the whole gate rests on: a 401 is not an empty account."""
    account["error"] = "unauthorized"
    answer = positions.snapshot(_s(tmp_path, **PAPER_KEYS), "paper")
    assert answer.known is False
    assert answer.flat is False, "unknown must never read as flat"
    assert "could not be read" in answer.reason


def test_an_account_that_could_not_be_read_is_not_flat_even_when_empty_looking(tmp_path, account):
    """Belt and braces on the same property, because this one costs money if wrong."""
    account["error"] = "connection reset"
    states = positions.snapshots(_s(tmp_path, **PAPER_KEYS, **LIVE_KEYS))
    assert [s.known for s in states] == [False, False]
    assert positions.held(states) == []
    assert len(positions.unknown(states)) == 2


def test_both_accounts_are_read_not_just_the_one_in_play(tmp_path, account):
    """Paper and live are different accounts; a position in the other one is still a
    position, and switching between them is exactly how it gets stranded."""
    account["live"] = [_position("MSFT", qty="5")]
    settings = _s(tmp_path, **PAPER_KEYS, **LIVE_KEYS)   # the strategy is on PAPER
    states = positions.snapshots(settings)
    assert [s.env for s in states] == ["paper", "live"]
    assert positions.held(states)[0].count == 1


def test_a_key_without_its_secret_is_unknown_rather_than_flat(tmp_path, account):
    """A half-configured pair cannot place an order, but it is not proof about the
    account either — so it refuses instead of guessing."""
    settings = _s(tmp_path, alpaca_paper_api_key="PK-PAPER")   # no secret
    answer = positions.snapshot(settings, "paper")
    assert answer.known is False and answer.flat is False


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------
def test_a_burst_of_checks_is_one_broker_call(tmp_path, account, monkeypatch):
    calls = []
    monkeypatch.setattr(positions, "probe", lambda viewed, env: calls.append(env) or [])
    settings = _s(tmp_path, **PAPER_KEYS)

    positions.snapshot(settings, "paper")
    positions.snapshot(settings, "paper")
    positions.snapshot(settings, "paper")

    assert calls == ["paper"], "three checks on one page must not be three round trips"


def test_force_re_reads_and_the_cache_expires(tmp_path, account, monkeypatch):
    calls = []
    monkeypatch.setattr(positions, "probe", lambda viewed, env: calls.append(env) or [])
    settings = _s(tmp_path, **PAPER_KEYS)

    positions.snapshot(settings, "paper")
    positions.snapshot(settings, "paper", force=True)
    assert calls == ["paper", "paper"]

    # And the TTL alone is enough, without force — proven by moving the clock rather than
    # by sleeping.
    now = [1000.0]
    monkeypatch.setattr(positions, "_now", lambda: now[0])
    positions.forget()
    positions.snapshot(settings, "paper")
    now[0] += positions.CACHE_SECONDS + 0.1
    positions.snapshot(settings, "paper")
    assert calls == ["paper", "paper", "paper", "paper"]


def test_the_cache_is_keyed_by_account_so_a_new_key_cannot_inherit_an_answer(tmp_path, account, monkeypatch):
    """Swapping credentials must invalidate what the old ones proved."""
    calls = []
    monkeypatch.setattr(positions, "probe", lambda viewed, env: calls.append(env) or [])
    positions.snapshot(_s(tmp_path, **PAPER_KEYS), "paper")
    positions.snapshot(_s(tmp_path, alpaca_paper_api_key="PK-OTHER", alpaca_paper_api_secret="S"), "paper")
    assert calls == ["paper", "paper"]


# ---------------------------------------------------------------------------
# the blocker, and the message
# ---------------------------------------------------------------------------
def test_nothing_open_blocks_nothing(tmp_path, account):
    assert trading_service.flat_blocker(_s(tmp_path, **PAPER_KEYS), action="X cannot be done") is None


def test_the_refusal_names_the_position_and_both_ways_out(tmp_path, account):
    account["paper"] = [_position()]
    blocker = trading_service.flat_blocker(_s(tmp_path, **PAPER_KEYS), action="Trading cannot start")
    assert blocker.startswith("Trading cannot start")
    assert "90 AAPL" in blocker, "the operator must be told WHAT is open"
    assert "paper account" in blocker, "and WHERE"
    assert "Stop trading & flatten" in blocker, "one way out"
    assert "wait" in blocker and "bracket" in blocker, "and the other, which needs no cleanup"


def test_the_refusal_for_an_unreadable_account_does_not_pretend_to_know(tmp_path, account):
    account["error"] = "unauthorized"
    blocker = trading_service.flat_blocker(_s(tmp_path, **PAPER_KEYS), action="Trading cannot start")
    assert "could not be read" in blocker
    assert "assuming it is flat" in blocker


def test_a_second_position_is_counted_not_hidden(tmp_path, account):
    account["paper"] = [_position("AAPL"), _position("MSFT", qty="3")]
    blocker = trading_service.flat_blocker(_s(tmp_path, **PAPER_KEYS), action="X cannot be done")
    assert "and 1 more" in blocker


def test_several_accounts_are_named_in_one_message(tmp_path, account):
    account["paper"] = [_position("AAPL")]
    account["live"] = [_position("TSLA", qty="2")]
    blocker = trading_service.flat_blocker(_s(tmp_path, **PAPER_KEYS, **LIVE_KEYS), action="X cannot be done")
    assert "the paper account holds 90 AAPL" in blocker
    assert "the live account holds 2 TSLA" in blocker


# ---------------------------------------------------------------------------
# the gated endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def wired(tmp_path, state_file):
    settings = _s(tmp_path, **PAPER_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        yield TestClient(app), settings
    finally:
        app.dependency_overrides.clear()


def test_the_three_orphan_prone_endpoints_refuse_while_a_position_is_open(wired, account):
    client, _ = wired
    account["paper"] = [_position()]

    for path, body in (
        ("/api/v1/rules/select", {"name": "beta"}),
        ("/api/v1/rules/delete", {"name": "beta", "delete_data": False}),
        ("/api/v1/execution/env", {"env": "live"}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 409, path
        detail = response.json()["detail"]
        assert "90 AAPL" in detail, path
        assert "Flatten first" in detail, path


def test_the_same_endpoints_go_through_when_nothing_is_open(wired, account):
    client, _ = wired
    account["paper"] = []
    for path, body in (
        ("/api/v1/rules/select", {"name": "beta"}),
        ("/api/v1/rules/delete", {"name": "beta", "delete_data": False}),
        ("/api/v1/execution/env", {"env": "live"}),
    ):
        response = client.post(path, json=body)
        assert response.status_code != 409, path
        assert response.status_code < 500, path


def test_a_live_position_blocks_an_action_on_a_paper_strategy(wired, account):
    """The reason both accounts are read: the strategy on screen is not the whole story."""
    client, settings = wired
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings.model_copy(
        update={"alpaca_live_api_key": "PK-LIVE", "alpaca_live_api_secret": "S-LIVE"}
    )
    account["live"] = [_position("TSLA", qty="7")]

    response = client.post("/api/v1/rules/select", json={"name": "beta"})

    assert response.status_code == 409
    assert "the live account holds 7 TSLA" in response.json()["detail"]


def test_an_unreachable_broker_blocks_rather_than_permitting(wired, account):
    """Unknown still refuses — for the actions that would reach that account.

    This used to point at ``/rules/delete``. It moved to ``/execution/env`` when a 401 was
    ruled out as a reason to freeze the strategy picker: the property under test is
    unchanged, and the endpoint that keeps it is the one that can send orders to the
    account nobody can read.
    """
    client, _ = wired
    account["error"] = "unauthorized"
    response = client.post("/api/v1/execution/env", json={"env": "live"})
    assert response.status_code == 409
    assert "could not be read" in response.json()["detail"]


# ---------------------------------------------------------------------------
# arming
# ---------------------------------------------------------------------------
def test_trading_cannot_be_armed_on_top_of_a_position(tmp_path, state_file, account, verified):
    """The decision: refuse to arm while anything is open.

    It is what makes "only the active strategy trades" true of the POSITION as well as of
    new entries — the bot will not start on top of something it did not open.
    """
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")
    account["paper"] = [_position()]

    result = trading_service.turn_on(settings)

    assert result["ok"] is False
    assert result["needs_flatten"] is True
    assert "90 AAPL" in result["message"]
    assert trading_service.is_trading_on(settings) is False
    assert not state_file.exists(), "a refused arming must leave no state behind"


def test_arming_stamps_which_strategy_it_armed(tmp_path, state_file, account, verified):
    """The loop trades the STAMPED strategy and refuses on a mismatch, so the name has to
    be recorded at the moment of arming — while trading is off and it still means the
    strategy on screen."""
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")

    result = trading_service.turn_on(settings)

    assert result["ok"] is True
    assert result["state"]["strategy"] == trading_service.active_strategy_name()
    assert trading_service.get_state(settings)["strategy"] == result["state"]["strategy"]


def test_arming_is_still_refused_when_no_order_could_be_placed(tmp_path, state_file, account):
    """Unchanged fail-closed behaviour: no keys means "trading on" would be a lie."""
    result = trading_service.turn_on(_s(tmp_path))
    assert result["ok"] is False
    assert "not set" in result["message"]


# ---------------------------------------------------------------------------
# stopping: always allowed, and honest about what is left
# ---------------------------------------------------------------------------
def test_stopping_reports_nothing_open(tmp_path, state_file, account):
    result = trading_service.turn_off(_s(tmp_path, **PAPER_KEYS))
    assert result["ok"] is True
    assert result["left"]["open"] == 0
    assert "no positions are open" in result["left"]["message"]


def test_stopping_reports_a_position_that_still_has_its_exit(tmp_path, state_file, account):
    account["paper"] = [_position()]
    account["exits"] = [{"id": "leg", "type": "stop"}]
    result = trading_service.turn_off(_s(tmp_path, **PAPER_KEYS))
    assert result["left"]["open"] == 1
    assert result["left"]["protected"] is True
    assert "resting exit" in result["left"]["message"]
    assert "STILL OPEN" not in result["message"], "a protected position is not the loud case"


def test_stopping_reports_the_loud_case_a_position_with_no_exit(tmp_path, state_file, account):
    """An empty STOP_LOSS_PERCENT means no bracket, so this is an ordinary way to end up
    holding something nothing will close."""
    account["paper"] = [_position()]
    account["exits"] = []
    result = trading_service.turn_off(_s(tmp_path, **PAPER_KEYS))
    assert result["left"]["protected"] is False
    assert result["left"]["unprotected"] == ["AAPL"]
    assert "no resting exit" in result["left"]["message"]
    assert "unmanaged" in result["left"]["message"]
    assert "NO exit" in result["message"], "the stop message itself must say so, loudly"


def test_stopping_never_fails_even_when_the_broker_cannot_be_reached(tmp_path, state_file, account):
    """The stop button. A broker outage must not be able to trap the bot in ON."""
    account["error"] = "connection refused"
    settings = _s(tmp_path, **PAPER_KEYS)
    write_state(settings, {"on": True, "since": "2026-01-01T00:00:00+00:00"})

    result = trading_service.turn_off(settings)

    assert result["ok"] is True
    assert trading_service.is_trading_on(settings) is False, "the state was written regardless"
    assert result["left"]["known"] is False
    assert "could not be read" in result["left"]["message"]


def test_stopping_keeps_the_record_of_what_was_running(tmp_path, state_file, account, verified):
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")
    trading_service.turn_on(settings)
    armed = trading_service.get_state(settings)["strategy"]

    trading_service.turn_off(settings)

    state = trading_service.get_state(settings)
    assert state["on"] is False and state["since"] is None
    assert state["strategy"] == armed, "what was running is the first thing anyone asks"
    assert state["env"] == "paper"


# ---------------------------------------------------------------------------
# stop & flatten
# ---------------------------------------------------------------------------
def test_stop_and_flatten_stops_first_and_then_closes(tmp_path, state_file, account, verified):
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")
    trading_service.turn_on(settings)
    account["paper"] = [_position()]

    result = trading_service.stop_and_flatten(settings)

    assert result["ok"] is True
    assert account["flattened"] == ["AAPL"]
    assert trading_service.is_trading_on(settings) is False
    assert "were closed" in result["message"]


def test_a_flatten_that_fails_leaves_trading_off(tmp_path, state_file, account, verified):
    """The order of the two steps is the point: a broker failure must not leave the bot
    armed."""
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")
    trading_service.turn_on(settings)
    account["paper"] = [_position()]
    account["flatten_fails"] = True

    result = trading_service.stop_and_flatten(settings)

    assert result["ok"] is False
    assert trading_service.is_trading_on(settings) is False, "stopped regardless"
    assert "could not be closed" in result["message"]
    assert result["flattened"][0]["symbol"] == "AAPL"
    assert "error" in result["flattened"][0]


def test_stop_and_flatten_on_live_needs_the_confirmation_first(tmp_path, state_file, account):
    settings = _s(tmp_path, **LIVE_KEYS, execution_env="live")
    result = trading_service.stop_and_flatten(settings)
    assert result["ok"] is False
    assert result["needs_live_confirmation"] is True
    assert account["flattened"] == [], "nothing is closed before the operator confirms"


def test_stop_and_flatten_with_nothing_open_is_not_an_error(tmp_path, state_file, account):
    result = trading_service.stop_and_flatten(_s(tmp_path, **PAPER_KEYS))
    assert result["ok"] is True
    assert result["flattened"] == []
    assert "nothing open" in result["message"]


def test_stop_and_flatten_in_the_route(tmp_path, state_file, account):
    settings = _s(tmp_path, **PAPER_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        account["paper"] = [_position()]
        response = TestClient(app).post("/api/v1/trading/off-flatten", json={})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["flattened"][0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# a 401 is a verdict about a KEY, not about a position
#
# Reading both accounts is right, and it is why a live position blocks a paper
# strategy. But an account we cannot read is not the same finding as a position
# we can see, and the two actions that place no orders anywhere — changing the
# active strategy and deleting one — must not be frozen by it:
#
#   * a switch cannot be the reason an order lands somewhere unreadable;
#   * the remedy for a dead key is elsewhere, and refusing does not make an
#     invisible position visible or manageable;
#   * arming is ALSO refused while an account is blind, so nothing is made safe
#     by blocking the switch as well;
#   * and a rejected key never fixes itself, so a false positive here never
#     clears on retry — the picker stays locked for good.
#
# Arming and the environment switch keep the strict rule, because those two DO
# reach an account whose contents are unknown.
# ---------------------------------------------------------------------------
def _blind(monkeypatch, env):
    """Make one environment unreachable, leaving the other provably flat."""

    def probe(viewed, env_name):
        if env_name == env:
            raise AlpacaError("unauthorized")
        return []

    monkeypatch.setattr(positions, "probe", probe)


def test_a_401_on_the_account_not_being_traded_allows_a_switch(tmp_path, monkeypatch):
    _blind(monkeypatch, "live")
    settings = _s(tmp_path, execution_env="paper", **PAPER_KEYS, **LIVE_KEYS)

    assert trading_service.flat_blocker(
        settings, action="switch", unreadable_blocks=False
    ) is None


def test_a_401_on_the_account_being_traded_also_allows_a_switch(tmp_path, monkeypatch):
    """The rule is about the 401, not about which side of the switch it is on."""
    _blind(monkeypatch, "live")
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)

    assert trading_service.flat_blocker(
        settings, action="switch", unreadable_blocks=False
    ) is None


def test_a_401_on_both_accounts_still_allows_a_switch(tmp_path, monkeypatch):
    """The blunt form of the same rule: nothing readable anywhere is not a position."""
    monkeypatch.setattr(
        positions, "probe", lambda viewed, env: (_ for _ in ()).throw(AlpacaError("unauthorized"))
    )
    settings = _s(tmp_path, **PAPER_KEYS, **LIVE_KEYS)

    assert trading_service.flat_blocker(
        settings, action="switch", unreadable_blocks=False
    ) is None


def test_a_position_in_the_other_account_still_blocks_the_switch(tmp_path, monkeypatch):
    """The held rule is never relaxed: a position that can be SEEN is one that would be
    orphaned, and which account it sits in does not change that."""
    monkeypatch.setattr(
        positions, "probe", lambda viewed, env: [_position()] if env == "live" else []
    )
    settings = _s(tmp_path, execution_env="paper", **PAPER_KEYS, **LIVE_KEYS)

    blocker = trading_service.flat_blocker(
        settings, action="switch", unreadable_blocks=False
    )

    assert blocker is not None
    assert "90 AAPL" in blocker and "Flatten first" in blocker


def test_a_seen_position_outranks_a_blind_account(tmp_path, monkeypatch):
    """Both findings at once. ``describe`` names the position AND the account it could not
    read, because the operator needs both facts — but the position leads, since that is the
    one with a button next to it."""
    monkeypatch.setattr(
        positions, "probe", lambda viewed, env: [_position()] if env == "paper" else (_ for _ in ()).throw(AlpacaError("unauthorized"))
    )
    settings = _s(tmp_path, execution_env="paper", **PAPER_KEYS, **LIVE_KEYS)

    blocker = trading_service.flat_blocker(
        settings, action="switch", unreadable_blocks=False
    )

    assert blocker is not None
    assert "90 AAPL" in blocker and "Flatten first" in blocker
    assert blocker.index("90 AAPL") < blocker.index("could not be read"), \
        "the position is the actionable finding, so it leads"


def test_arming_is_still_refused_by_the_same_blind_account(tmp_path, monkeypatch):
    """The asymmetry, in one place: identical settings that allow a strategy change must
    not allow arming, because arming is what would put orders in the blind account."""
    _blind(monkeypatch, "live")
    settings = _s(tmp_path, execution_env="paper", **PAPER_KEYS, **LIVE_KEYS)

    blocker = trading_service.flat_blocker(settings, action="Trading cannot be started")

    assert blocker is not None
    assert "unknown" in blocker and "live" in blocker
    assert trading_service.flat_blocker(
        settings, action="switch", unreadable_blocks=False
    ) is None, "the switch is the one that has to keep working"


def test_the_strategy_routes_allow_a_401_but_arming_does_not(tmp_path, monkeypatch):
    """End to end on the reported bug, with both strategy actions exercised."""
    _blind(monkeypatch, "live")
    settings = _s(tmp_path, execution_env="paper", **PAPER_KEYS, **LIVE_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        client = TestClient(app)
        selected = client.post("/api/v1/rules/select", json={"name": "gamma"})
        deleted = client.post("/api/v1/rules/delete", json={"name": "gamma", "delete_data": False})
        armed = client.post("/api/v1/trading/on", json={"confirm": True})
    finally:
        app.dependency_overrides.clear()

    for name, response in (("select", selected), ("delete", deleted)):
        assert response.status_code != 409, (name, response.json())

    # Arming refuses the same account, but as a PAYLOAD rather than a status code —
    # ``turn_on`` never raises, because the panel shows the reason inline instead of
    # as an error toast. The refusal is the ``ok: false``.
    assert armed.json()["ok"] is False, "arming must still refuse the account it cannot read"
    assert armed.json()["needs_flatten"] is True
    assert "live" in armed.json()["message"]
