"""The trading switch, and the configuration lock it holds.

Two rules are pinned down here:

1. **Trading starts OFF and can only be turned on deliberately.** Turning it on
   is refused outright when the resolved account could not actually place an
   order (otherwise "trading on" is a lie), and on the LIVE account it needs a
   per-request confirmation, because that is the one action that spends real
   money.

2. **While trading is on, nothing that changes what the bot is running may be
   changed.** That is enforced by the server (HTTP 409), not merely by disabling
   buttons — a stale browser tab or a scripted POST must not be able to
   reconfigure a strategy mid-flight. Turning trading off is the only action that
   releases the lock, and it is always available.

The state is runtime, not configuration: it lives in ``data/trading.json`` beside
the datasets, because the configuration files it freezes cannot be where the
freeze switch is stored.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import state_files
from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.execution import credentials
from src.execution.config import LIVE_BASE_URL, PAPER_BASE_URL
from src.web.app import app
from src.web.services import loop_control
from src.web.services import config_service, rules_service, trading_service

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]  # tests/ -> repo root

PAPER_KEYS = {"alpaca_paper_api_key": "PK-paper", "alpaca_paper_api_secret": "PS-paper"}
LIVE_KEYS = {"alpaca_live_api_key": "LK-live", "alpaca_live_api_secret": "LS-live"}


def _s(tmp_path, **kwargs) -> Settings:
    """Settings with no .env in play and every file inside tmp_path."""
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        strategy_rules_file=str(tmp_path / "active.json"),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def no_real_loop(monkeypatch):
    """Turning trading on starts a REAL ``python -m src.main`` process.

    That process is its own interpreter against the real data root, whatever temp settings the
    test passed in — so it read the repo's real switch (off), wrote an "action: off" tick into
    the live tree of the machine's ACTIVE strategy, and exited. Every suite run therefore left a
    session behind for the selected strategy: the Session monitor showed a tick nobody ran, and a
    strategy that had never ticked looked as if it had.

    ``loop_control._spawn`` is stubbed, so the switch's own behaviour is still exercised in full
    (the state file, the credential re-check, the refusals) and only the process is not started.
    """
    calls = []

    class _Process:
        pid = 4242

    def spawn(command, **kwargs):
        calls.append(command)
        return _Process()

    monkeypatch.setattr(loop_control, "_spawn", spawn)
    return calls


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Redirect every runtime state file into tmp_path.

    Both `trading.json` and `credential_checks.json` resolve through the shared
    `state_files.state_path`, so patching it there covers the switch AND the
    credential verdicts — no test may touch the repo's real data directory.
    """
    monkeypatch.setattr(state_files, "state_path", lambda s, name, folder="": tmp_path / folder / name)
    return tmp_path / "trading.json"


@pytest.fixture
def verified(monkeypatch):
    """Record a PASSING credential verdict for an environment, offline."""
    monkeypatch.setattr(credentials, "probe", _accepting_probe())
    return lambda settings, env="paper", ok=True: credentials.verify(settings, env, force=True)


def _accepting_probe(ok=True, message="Credentials accepted — account A1 (ACTIVE)"):
    """A stand-in for the Alpaca call, so tests never touch the network."""
    calls = []

    def probe(key_id, secret, base_url, timeout=0):
        calls.append({"key_id": key_id, "base_url": base_url})
        return {"ok": ok, "message": message, "account_number": "A1", "status": "ACTIVE"}

    probe.calls = calls
    return probe


@pytest.fixture
def wired(tmp_path, monkeypatch, state_file):
    """A TestClient whose settings point at tmp_path, so no real file is touched."""
    settings = _s(tmp_path)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# the switch itself
# ---------------------------------------------------------------------------
def test_trading_starts_off_and_creates_no_state_file(tmp_path, state_file):
    settings = _s(tmp_path, **PAPER_KEYS)
    assert trading_service.is_trading_on(settings) is False
    assert trading_service.get_state(settings) == {"on": False, "since": None}
    assert not state_file.exists()  # OFF is the absence of state, not a stored flag


def test_turning_on_is_refused_while_an_order_could_not_be_placed(tmp_path, state_file):
    """Fail closed: with no keys, "trading on" would be a lie, so it is refused."""
    settings = _s(tmp_path)  # no Alpaca keys at all
    res = trading_service.turn_on(settings)
    assert res["ok"] is False
    assert "not set" in res["message"]
    assert trading_service.is_trading_on(settings) is False
    assert not state_file.exists()


def test_paper_turns_on_and_off(tmp_path, state_file, verified):
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")
    on = trading_service.turn_on(settings)
    assert on["ok"] is True
    assert trading_service.is_trading_on(settings) is True
    # The stamp records WHERE orders would go, so a run is never confused later.
    state = trading_service.get_state(settings)
    assert state["env"] == "paper" and state["broker"] == "alpaca"
    assert state["base_url"] == PAPER_BASE_URL
    assert state["since"]

    off = trading_service.turn_off(settings)
    assert off["ok"] is True
    assert trading_service.is_trading_on(settings) is False
    assert trading_service.get_state(settings)["since"] is None


def test_live_needs_an_explicit_confirmation_every_time(tmp_path, state_file, verified):
    """Real money costs one deliberate extra action — and it is NOT remembered."""
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)
    verified(settings, "live")
    refused = trading_service.turn_on(settings)
    assert refused["ok"] is False
    assert refused["needs_live_confirmation"] is True
    assert trading_service.is_trading_on(settings) is False

    allowed = trading_service.turn_on(settings, confirm_live=True)
    assert allowed["ok"] is True
    assert trading_service.get_state(settings)["env"] == "live"
    assert trading_service.get_state(settings)["base_url"] == LIVE_BASE_URL

    # Turning it off and on again asks again: the confirmation is not a stored flag.
    trading_service.turn_off(settings)
    assert trading_service.turn_on(settings)["needs_live_confirmation"] is True


def test_the_live_confirmation_is_not_a_stored_setting():
    """It used to be a config field (EXECUTION_LIVE_ACK), which meant one save
    could arm real trading. It must not come back as configuration."""
    assert "execution_live_ack" not in Settings.model_fields
    assert "EXECUTION_LIVE_ACK" not in config_service.STRATEGY_SCOPED_KEYS
    assert "EXECUTION_LIVE_ACK" not in config_service.ACCOUNT_SCOPED_KEYS
    assert "EXECUTION_LIVE_ACK" in config_service.RETIRED_STRATEGY_KEYS


def test_the_state_file_is_private_and_atomic(tmp_path, state_file, verified):
    settings = _s(tmp_path, **PAPER_KEYS)
    verified(settings, "paper")
    trading_service.turn_on(settings)
    mode = stat.S_IMODE(state_file.stat().st_mode)
    assert mode == 0o600  # owner-only, like the account file
    # No temp files left behind by the atomic write.
    assert [p.name for p in state_file.parent.glob(".trading.*")] == []


def test_an_unreadable_state_file_is_not_mistaken_for_running(tmp_path, state_file):
    settings = _s(tmp_path, **PAPER_KEYS)
    state_file.write_text("{ this is not json", encoding="utf-8")
    assert trading_service.is_trading_on(settings) is False


# ---------------------------------------------------------------------------
# the lock (server-side)
# ---------------------------------------------------------------------------
LOCKED_WRITES = [
    ("post", "/api/v1/account", {"values": {"DATA_DIR": "/tmp/x"}}),
    ("post", "/api/v1/rules", {"rules": []}),
    ("post", "/api/v1/rules/create", {"name": "new"}),
    ("post", "/api/v1/rules/select", {"name": "new"}),
    ("post", "/api/v1/rules/delete", {"name": "new"}),
    ("post", "/api/v1/rules/rename", {"name": "a", "new_name": "b"}),
    ("post", "/api/v1/rules/reset", {}),
    ("post", "/api/v1/backtest/run", {}),
    ("post", "/api/v1/dataset/backfill", {}),
    ("post", "/api/v1/dataset/rebuild", {}),
    ("post", "/api/v1/delta/sync", {}),
    ("post", "/api/v1/execution/env", {"env": "live"}),
]


def _force_on(state_file) -> None:
    """Write the ON state directly: these tests are about the LOCK, and the
    tests above already prove the switch itself will not turn on unconfigured."""
    state_file.write_text(
        json.dumps(
            {
                "on": True,
                "since": "2026-01-01T00:00:00+00:00",
                "env": "paper",
                "broker": "alpaca",
                "base_url": PAPER_BASE_URL,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("method, path, body", LOCKED_WRITES)
def test_every_configuration_write_is_refused_while_trading_is_on(wired, state_file, method, path, body):
    _force_on(state_file)
    r = getattr(client, method)(path, json=body)
    assert r.status_code == 409, f"{path} answered {r.status_code}: {r.text[:120]}"
    assert "Turn trading off" in r.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/account",
        "/api/v1/rules",
        "/api/v1/backtest",
        "/api/v1/dataset/status",
        "/api/v1/delta/status",
        "/api/v1/execution/status",
        "/api/v1/trading",
    ],
)
def test_reads_stay_available_while_trading_is_on(wired, state_file, path):
    """A frozen dashboard is useless — the lock must gate writes only."""
    _force_on(state_file)
    assert client.get(path).status_code == 200


def test_turning_trading_off_releases_the_lock(wired, state_file):
    _force_on(state_file)
    assert client.post("/api/v1/rules/reset").status_code == 409

    off = client.post("/api/v1/trading/off")
    assert off.status_code == 200
    assert off.json()["ok"] is True
    assert client.post("/api/v1/rules/reset").status_code == 200


def test_turn_off_is_always_allowed_even_when_the_account_is_broken(wired, state_file):
    """The release hatch must not depend on the very configuration it releases."""
    _force_on(state_file)
    r = client.post("/api/v1/trading/off")
    assert r.status_code == 200
    assert r.json()["state"]["on"] is False


# ---------------------------------------------------------------------------
# the API surface
# ---------------------------------------------------------------------------
def test_get_trading_reports_the_state_the_lock_and_the_options(wired):
    body = client.get("/api/v1/trading").json()
    assert set(body) == {
        "trading", "locked", "execution", "env_options", "strategy", "instrument",
        "bar_size", "verification", "freshness",
        # What the ACCOUNTS hold, which is not the same question as whether trading is
        # armed — the panel stays on screen while something is open precisely because
        # "off" is not "flat". Cached, so polling this is not a broker call per poll.
        "positions", "open_count", "unknown_count",
    }
    # No keys in this fixture, so each account is provably empty rather than assumed so.
    assert body["open_count"] == 0 and body["unknown_count"] == 0
    assert [p["env"] for p in body["positions"]] == ["paper", "live"]
    assert all(p["by_construction"] for p in body["positions"])
    # The payload says whether the process is still the gate the files describe.
    assert body["freshness"]["stale"] is False, "the test process just imported it"
    assert any(p.endswith("trading_service.py") for p in body["freshness"]["watched"])
    assert body["trading"]["on"] is False and body["locked"] is False
    assert [o["value"] for o in body["env_options"]] == ["paper", "live"]
    # The dropdown's options come from the server, so the client cannot invent a
    # third environment.
    assert body["env_options"] == config_service.EXECUTION_ENV_OPTIONS
    # The header can see WHY the switch would refuse, before it is pressed.
    assert body["verification"]["env"] == "paper"
    assert body["verification"]["verified"] is False


def test_turning_on_through_the_api_reports_why_it_cannot(wired):
    body = client.post("/api/v1/trading/on", json={}).json()
    assert body["ok"] is False
    assert "not set" in body["message"]
    assert body["state"]["on"] is False


def test_turning_on_a_live_strategy_through_the_api_needs_the_flag(tmp_path, monkeypatch, state_file, verified):
    monkeypatch.setattr(credentials, "probe", _accepting_probe())
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        # The credentials are checked first, and they pass — so the next thing in the
        # way is the acknowledgement, not the keys.
        assert client.post("/api/v1/trading/on", json={}).json()["needs_live_confirmation"] is True
        assert client.post("/api/v1/trading/on", json={"confirm_live": True}).json()["ok"] is True
        assert client.get("/api/v1/trading").json()["locked"] is True
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# configured is not the same as WORKING
#
# Turning trading on re-checks the credentials of the environment in play against
# the broker, every time, in BOTH environments — a second layer under the stored
# verdict. A key can be revoked or rotated without the fingerprint changing, so
# "it passed last week" is not evidence about now.
# ---------------------------------------------------------------------------
def test_paper_is_revalidated_every_time_trading_is_turned_on(tmp_path, state_file, monkeypatch):
    """Nobody has to press Validate first, and nobody gets to rely on an old pass
    either: the switch asks the broker, and asking again is the point."""
    probe = _accepting_probe()
    monkeypatch.setattr(credentials, "probe", probe)
    settings = _s(tmp_path, **PAPER_KEYS)
    assert credentials.check_for(settings, "paper")["verified"] is False

    assert trading_service.turn_on(settings)["ok"] is True
    assert len(probe.calls) == 1, "turning trading on is where paper gets checked"

    trading_service.turn_off(settings)
    assert trading_service.turn_on(settings)["ok"] is True
    assert len(probe.calls) == 2, "...and checked again, not remembered"


def test_a_revoked_key_cannot_be_armed_from_a_stored_pass(tmp_path, state_file, monkeypatch):
    """The case the second layer exists for: the verdict on file says the pair works,
    and it does not work any more."""
    monkeypatch.setattr(credentials, "probe", _accepting_probe())
    settings = _s(tmp_path, **PAPER_KEYS)
    assert trading_service.turn_on(settings)["ok"] is True
    trading_service.turn_off(settings)
    assert credentials.check_for(settings, "paper")["verified"] is True, "a pass is on file"

    # Alpaca revokes it between then and now.
    monkeypatch.setattr(
        credentials, "probe",
        _accepting_probe(ok=False, message="Alpaca rejected these credentials (401)"),
    )
    refused = trading_service.turn_on(settings)
    assert refused["ok"] is False
    assert "401" in refused["message"]
    assert trading_service.is_trading_on(settings) is False, "trading must stay off"
    assert credentials.check_for(settings, "paper")["verified"] is False, "the pass is spent"


def test_a_broker_that_cannot_be_reached_leaves_trading_off(tmp_path, state_file, monkeypatch):
    """Fail closed. A key we cannot prove is not a key we can claim to trade with."""
    monkeypatch.setattr(
        credentials, "probe",
        lambda *a, **k: {"ok": False, "reason": "unreachable", "message": "Could not reach https://paper-api.alpaca.markets — timeout"},
    )
    settings = _s(tmp_path, **PAPER_KEYS)
    refused = trading_service.turn_on(settings)
    assert refused["ok"] is False
    assert "Could not reach" in refused["message"]
    assert trading_service.is_trading_on(settings) is False


def test_paper_with_no_credentials_is_refused_and_nothing_is_written(tmp_path, state_file):
    settings = _s(tmp_path)  # no keys at all
    refused = trading_service.turn_on(settings)
    assert refused["ok"] is False
    assert "Account Settings" in refused["message"], "the refusal must say what to do"
    assert trading_service.is_trading_on(settings) is False
    assert not state_file.exists(), "a refusal must not leave a half-written state file"


def test_half_a_pair_is_not_enough_to_start(tmp_path, state_file):
    """A key without its secret is not a credential. The execution gate catches it
    before the credential check is even reached, and still says what to do."""
    settings = _s(tmp_path, alpaca_paper_api_key="PK-key-only")
    refused = trading_service.turn_on(settings)
    assert refused["ok"] is False
    assert "not set" in refused["message"] and "Account Settings" in refused["message"]
    assert trading_service.is_trading_on(settings) is False
    assert not state_file.exists()


def test_a_failed_check_blocks_paper_trading_and_says_why(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(
        credentials, "probe",
        _accepting_probe(ok=False, message="Alpaca rejected these credentials (401)"),
    )
    settings = _s(tmp_path, **PAPER_KEYS)
    refused = trading_service.turn_on(settings)
    assert refused["ok"] is False
    assert refused["needs_verification"] is True
    assert "401" in refused["message"], "the operator needs the reason, not just a no"
    assert trading_service.is_trading_on(settings) is False


def test_live_is_revalidated_at_the_click_too(tmp_path, state_file, monkeypatch):
    """Real money does not get a weaker gate than paper: the same fresh check runs,
    and the only extra step is the acknowledgement."""
    probe = _accepting_probe()
    monkeypatch.setattr(credentials, "probe", probe)
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)

    # Nothing on file, and it still starts — because the check happens NOW.
    assert credentials.check_for(settings, "live")["verified"] is False
    result = trading_service.turn_on(settings, confirm_live=True)
    assert result["ok"] is True and len(probe.calls) == 1

    trading_service.turn_off(settings)
    monkeypatch.setattr(
        credentials, "probe",
        _accepting_probe(ok=False, message="Alpaca rejected these credentials (401)"),
    )
    refused = trading_service.turn_on(settings, confirm_live=True)
    assert refused["ok"] is False and refused["needs_verification"] is True
    assert "401" in refused["message"]
    assert trading_service.is_trading_on(settings) is False
    assert len(probe.calls) == 1, "...and the refusal came from the fresh check"


def test_changing_the_stored_key_is_checked_at_the_next_start(tmp_path, state_file, monkeypatch):
    """Swapping a key expires its verdict — and since the switch re-checks anyway,
    what matters is whether the key in place now works."""
    monkeypatch.setattr(credentials, "probe", _accepting_probe())
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)
    credentials.verify(settings, "live", force=True)
    assert trading_service.turn_on(settings, confirm_live=True)["ok"] is True
    trading_service.turn_off(settings)

    replaced = _s(tmp_path, execution_env="live", alpaca_live_api_key="LK-other", alpaca_live_api_secret="LS-other")
    state = credentials.check_for(replaced, "live")
    assert state["verified"] is False and state["stale"] is True, "the old pass is not inherited"
    assert trading_service.turn_on(replaced, confirm_live=True)["ok"] is True


def test_a_changed_paper_key_is_checked_again_rather_than_refused(tmp_path, state_file, monkeypatch):
    probe = _accepting_probe()
    monkeypatch.setattr(credentials, "probe", probe)
    settings = _s(tmp_path, **PAPER_KEYS)
    assert trading_service.turn_on(settings)["ok"] is True
    trading_service.turn_off(settings)

    replaced = _s(tmp_path, alpaca_paper_api_key="PK-other", alpaca_paper_api_secret="PS-other")
    assert credentials.check_for(replaced, "paper")["stale"] is True
    assert trading_service.turn_on(replaced)["ok"] is True
    assert len(probe.calls) == 2, "the new key is checked, not carried over"


def test_a_passing_check_is_not_repeated_but_a_failure_is(tmp_path, state_file, monkeypatch):
    """The operator asked for exactly this economy: verify a pair once, not on every
    save. A FAILURE is not cached, because it may just be the network."""
    probe = _accepting_probe()
    monkeypatch.setattr(credentials, "probe", probe)
    settings = _s(tmp_path, **PAPER_KEYS)

    first = credentials.verify(settings, "paper")
    assert first["ok"] is True and first["checked"] is True and len(probe.calls) == 1

    again = credentials.verify(settings, "paper")
    assert again["checked"] is False
    assert len(probe.calls) == 1, "a known-good pair must not cost another call"

    # ...but a failing check is retried rather than remembered.
    failing = _accepting_probe(ok=False, message="boom")
    monkeypatch.setattr(credentials, "probe", failing)
    other = _s(tmp_path, alpaca_paper_api_key="PK-bad", alpaca_paper_api_secret="PS-bad")
    assert credentials.verify(other, "paper")["ok"] is False
    assert credentials.verify(other, "paper")["ok"] is False
    assert len(failing.calls) == 2, "a failure may be a blip: check again next time"


def test_verify_new_checks_only_pairs_without_a_verdict(tmp_path, state_file, monkeypatch):
    from src.execution import credentials as creds

    probe = _accepting_probe()
    monkeypatch.setattr(creds, "probe", probe)
    settings = _s(tmp_path, **PAPER_KEYS, **LIVE_KEYS)

    first = creds.verify_new(settings)
    assert set(first) == {"paper", "live"} and len(probe.calls) == 2
    second = creds.verify_new(settings)
    assert len(probe.calls) == 2, "nothing new to check: no further calls"
    assert all(not r["checked"] for r in second.values())


def test_the_endpoint_body_cannot_arrive_empty(wired):
    """An empty body is valid (= no confirmation), which is what a bare click sends."""
    assert client.post("/api/v1/trading/on").status_code == 200


# ---------------------------------------------------------------------------
# the environment dropdown's narrow write path
# ---------------------------------------------------------------------------
def test_the_dropdown_writes_only_the_environment_of_the_active_strategy(tmp_path):
    settings = _s(tmp_path)
    rules_service.create_strategy(settings, "alpha")
    created = rules_service.update_strategy(settings, "alpha", {"name": "alpha", "rules": []})
    assert created["ok"] is True

    res = rules_service.set_execution_env(settings, "live")
    assert res["ok"] is True and res["environment"] == "live"
    stored = rules_service.payload(settings)["strategies"]["alpha"]["config"]
    assert stored["EXECUTION_ENV"] == "live"
    # Nothing else was invented or lost.
    assert set(stored) <= {"EXECUTION_ENV", "INSTRUMENT"}


def test_a_new_strategy_starts_on_paper_even_with_live_keys_configured(tmp_path):
    """Real money is opt-in, never inherited. Configuring live credentials, or
    reusing a strategy, must not move the default: the operator picks live."""
    settings = _s(tmp_path, **LIVE_KEYS)
    assert settings.execution_env == "paper", "the setting's own default"
    rules_service.create_strategy(settings, "alpha")
    stored = rules_service.payload(settings)["strategies"]["alpha"]["config"]
    assert stored["EXECUTION_ENV"] == "paper"
    # And with a paper pair configured too, still paper.
    both = _s(tmp_path, **PAPER_KEYS, **LIVE_KEYS)
    assert both.execution_env == "paper"


def test_the_dropdown_rejects_an_unknown_environment(tmp_path):
    settings = _s(tmp_path)
    rules_service.create_strategy(settings, "alpha")
    before = rules_service.payload(settings)["strategies"]["alpha"]["config"]["EXECUTION_ENV"]
    res = rules_service.set_execution_env(settings, "production")
    assert res["ok"] is False
    assert res["errors"]
    # Refused, not coerced: the stored value is exactly what it was.
    after = rules_service.payload(settings)["strategies"]["alpha"]["config"]["EXECUTION_ENV"]
    assert after == before == "paper"


def test_the_dropdown_needs_an_active_strategy(tmp_path):
    settings = _s(tmp_path)  # empty store
    res = rules_service.set_execution_env(settings, "live")
    assert res["ok"] is False
    assert "No active strategy" in res["message"]


def test_saving_the_configuration_panel_preserves_the_hidden_environment(tmp_path):
    """The panel never renders EXECUTION_ENV, so a panel save must carry it over —
    otherwise a plain Save would silently reset a live strategy back to paper."""
    settings = _s(tmp_path)
    rules_service.create_strategy(settings, "alpha")
    rules_service.set_execution_env(settings, "live")

    rules_service.update_strategy(
        settings, "alpha", {"name": "alpha", "rules": [], "config": {"INSTRUMENT": "AAPL"}}
    )
    stored = rules_service.payload(settings)["strategies"]["alpha"]["config"]
    assert stored["EXECUTION_ENV"] == "live"
    assert stored["INSTRUMENT"] == "AAPL"


def test_a_retired_key_is_dropped_not_rejected(tmp_path):
    """A strategy saved before EXECUTION_LIVE_ACK was removed must still be
    saveable — dropping the key is the only way that works."""
    settings = _s(tmp_path)
    rules_service.create_strategy(settings, "alpha")
    res = rules_service.update_strategy(
        settings,
        "alpha",
        {"name": "alpha", "rules": [], "config": {"EXECUTION_LIVE_ACK": True, "INSTRUMENT": "AAPL"}},
    )
    assert res["ok"] is True
    stored = rules_service.payload(settings)["strategies"]["alpha"]["config"]
    assert "EXECUTION_LIVE_ACK" not in stored
    assert stored["INSTRUMENT"] == "AAPL"


# ---------------------------------------------------------------------------
# the wiring (static: the ids the JS binds to must exist, and the ones that
# must NOT exist are the point)
# ---------------------------------------------------------------------------
def test_the_lab_has_no_trading_controls_at_all():
    """The Strategy lab builds a strategy; the Session monitor trades one.

    The Trading panel — the mode, the master switch, the flatten button, the account's boxes and
    the working orders — was removed from this page WHOLE, so everything it owned must be gone
    from the markup AND from the script: an id the JS still binds to is a page that breaks, and a
    write path left behind is a second button on the switch that nobody can see.

    Pinned as a list because this is exactly the kind of removal that half-happens: the panel
    goes, and the handler that armed the account stays reachable from somewhere else.
    """
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    for gone in ("live-card", "live-body", "live-state", "live-protection", "live-metrics",
                 "live-refresh", "trading-facts", "trading-flatten-btn", "trading-toggle",
                 "trading-panel", "trading-msg", "open-count", "exec-env"):
        assert f'id="{gone}"' not in html, f"#{gone} belonged to the removed Trading panel"
    for gone in ("renderTradingPanel", "renderLiveDetail", "stopAndFlatten", "onModeBoxClick",
                 "toggleTrading", "liveTile", "scheduleLivePoll", "runLivePoll", "loadLive",
                 "livePanelVisible", "execRow", "TraiderSwitch.flip("):
        assert gone not in js, f"{gone} is dead code from the removed panel"
    # The panel's styles went with it.
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    for gone in ("#live-state", "#live-protection", "#live-metrics", ".live-metrics",
                 ".strategy-slot > #live-card", "#trading-flatten-btn"):
        assert gone not in css, f"{gone} styled the removed panel"

    # The Session monitor carries all of it now — the same boxes, built by the shared module.
    log_html = (ROOT / "src" / "web" / "templates" / "log.html").read_text(encoding="utf-8")
    log_js = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")
    assert 'id="lg-accounts"' in log_html
    assert "TraiderSwitch.envTile(" in log_js and "TraiderSwitch.tradeTile(" in log_js
    assert '"toggleTrading()"' in log_js, "the Trading box calls the master switch"


def test_the_lab_still_reads_the_lock_that_freezes_its_configuration():
    """Trading ON freezes every configuration surface, and that is the ONE fact this page still
    borrows from trading: the server refuses a write with a 409, and the page mirrors it so nothing
    is clickable that would be refused. Read it, never act on it — the lab cannot arm anything."""
    js = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function applyConfigLock()" in js
    assert "!!state.tradingLocked" in js
    assert 'api("/api/v1/trading")' in js, "the lock comes from the trading read"
    assert "/api/v1/trading/on" not in js and "/api/v1/trading/off" not in js
    for key in ("account-save", "save-pconfig", "save-rules", "save-risk"):
        assert f'"{key}"' in js
    assert "classList.toggle(\"trading-on\"" in js

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".exec-state" not in css, "the removed Execution panel's styles should not linger"
    # ``body.trading-on`` survives as the lock's hook (applyConfigLock still sets it) but nothing
    # is styled by it any more: the green edge it drew is gone.
    assert "body.trading-on" not in css


def test_the_header_carries_no_trading_controls_at_all():
    """One pill, one button, three answers — the header used to repeat trading three times over:
    the mode, the master switch and the open count, each a copy of something the Trading panel
    already says and can act on.

    All three are gone. What is left in the left group is the identity, and everything that
    configures or navigates away stays on the right."""
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    left = html.index('class="header-left"')
    actions = html.index('class="header-actions"')
    head = html[left:actions]

    for gone in ("exec-env", "trading-toggle", "open-count"):
        assert f'id="{gone}"' not in head, f"{gone} belongs to the Trading panel now"
    assert ">TRAIDER<" in head, "the identity stays"
    for el in ("open-account-settings",):
        assert html.index(el) > actions, f"{el} must stay in the right group"
    # The global (.env) settings form is gone: infrastructure settings are edited in
    # `.env` directly, and a button that opens a form for removed code is worse than
    # no button at all.
    assert "open-global-settings" not in html
    assert "global-settings-backdrop" not in html
    # The logo is the product name, not the page name.
    assert "TRAIDER Dashboard" not in html
    assert "<h1>TRAIDER</h1>" in html


def test_the_boxes_are_never_styled_per_state():
    """NO state gets its own styling: same border, radius, padding, font, tint and background,
    whatever the state. One rule that restyles a single state is enough to make one box read as a
    different kind of control from the ones beside it, which is the thing this guards.

    The pill that used to dress the header's two controls — and then the log page's switch — is
    gone with the last thing that wore it: the mode and the switch are `.bt-stat` boxes on both
    pages now, and what varies between their states is two named tints and the dot.
    """
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    shared = (ROOT / "src" / "web" / "static" / "trading_switch.js").read_text(encoding="utf-8")

    # Nothing is styled per state: the boxes' own tints are the only ones, and they are named.
    assert ".exec-pill" not in css, "the pill went with the last control that wore it"
    assert css.count(".bt-stat.flash-red") >= 1 and ".bt-stat.tint-blue" in css

    def rule(selector):
        start = css.index(selector + " {")
        return css[start:css.index("}", start) + 1]

    base = rule(".bt-stat")
    for prop in ("border", "border-radius", "padding", "background"):
        assert prop in base, f"the shared box must define {prop}"

    # The tint goes on the BOX, and only these two states have one.
    assert 'flash-red' in shared and 'tint-blue' in shared

    # The mode that cannot trade still says so in words — quoted on the box that reports it, which
    # is where the dropdown's "⚠ no keys" label went.
    assert "Trading cannot start — " in shared
