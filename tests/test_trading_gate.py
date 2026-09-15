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
import re
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.execution.config import LIVE_BASE_URL, PAPER_BASE_URL
from src.web.app import app
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


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Redirect the runtime state file (and the rules store) into tmp_path."""
    monkeypatch.setattr(trading_service, "state_path", lambda s: tmp_path / "trading.json")
    return tmp_path / "trading.json"


@pytest.fixture
def wired(tmp_path, monkeypatch, state_file):
    """A TestClient whose settings point at tmp_path, so no real file is touched."""
    settings = _s(tmp_path)
    monkeypatch.setattr(trading_service, "state_path", lambda s: state_file)
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


def test_paper_turns_on_and_off(tmp_path, state_file):
    settings = _s(tmp_path, **PAPER_KEYS)
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


def test_live_needs_an_explicit_confirmation_every_time(tmp_path, state_file):
    """Real money costs one deliberate extra action — and it is NOT remembered."""
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)
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


def test_the_state_file_is_private_and_atomic(tmp_path, state_file):
    settings = _s(tmp_path, **PAPER_KEYS)
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
    ("post", "/api/v1/config", {"values": {"OPENBB_PROVIDER": "yfinance"}}),
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
        "/api/v1/config",
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
        "trading", "locked", "execution", "env_options", "strategy", "instrument", "bar_size",
    }
    assert body["trading"]["on"] is False and body["locked"] is False
    assert [o["value"] for o in body["env_options"]] == ["paper", "live"]
    # The dropdown's options come from the server, so the client cannot invent a
    # third environment.
    assert body["env_options"] == config_service.EXECUTION_ENV_OPTIONS


def test_turning_on_through_the_api_reports_why_it_cannot(wired):
    body = client.post("/api/v1/trading/on", json={}).json()
    assert body["ok"] is False
    assert "not set" in body["message"]
    assert body["state"]["on"] is False


def test_turning_on_a_live_strategy_through_the_api_needs_the_flag(tmp_path, monkeypatch, state_file):
    settings = _s(tmp_path, execution_env="live", **LIVE_KEYS)
    monkeypatch.setattr(trading_service, "state_path", lambda s: state_file)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        assert client.post("/api/v1/trading/on", json={}).json()["needs_live_confirmation"] is True
        assert client.post("/api/v1/trading/on", json={"confirm_live": True}).json()["ok"] is True
        assert client.get("/api/v1/trading").json()["locked"] is True
    finally:
        app.dependency_overrides.clear()


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
# the dashboard wiring (static: the ids the JS binds to must exist)
# ---------------------------------------------------------------------------
def test_the_trading_switch_and_its_lock_are_wired_into_the_dashboard():
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    # The Execution panel reports the resolved target and explains the lock; the
    # switch itself lives in the header (see the layout test below).
    assert 'id="execution-card"' in html
    assert 'id="trading-toggle"' in html
    assert 'id="exec-state-line"' in html
    assert 'id="exec-msg"' in html
    assert 'id="exec-facts"' in html
    assert 'id="exec-lock-note"' in html
    # The armed panel sits under the chart, i.e. above the backtest card it freezes.
    panel = html.index('id="trading-panel"')
    assert html.index('id="backtest"') > panel
    assert 'id="trading-off-btn"' in html

    js = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function applyConfigLock()" in js
    # The lock is applied from ONE place, over one shared list of buttons.
    assert "!!state.tradingLocked" in js
    for key in ("save-config", "account-save", "save-pconfig", "save-rules", "save-risk", "exec-env"):
        assert f'"{key}"' in js
    # The off switch is never disabled, or the lock could not be released.
    assert "btn.disabled = false; // the off switch must always be reachable" in js

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".exec-state.on" in css
    assert "body.trading-on" in css


def test_the_header_keeps_identity_and_the_switch_left_and_config_right():
    """Which account the bot trades and whether it is trading are identity, not
    settings: both sit beside the logo, always on screen. Configuration and
    navigation stay on the right."""
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    left = html.index('class="header-left"')
    switch = html.index('id="exec-env"')
    toggle = html.index('id="trading-toggle"')
    actions = html.index('class="header-actions"')
    assert left < switch < actions, "the paper/live switch must sit in the left group"
    assert left < toggle < actions, "the master switch must sit in the left group too"
    for el in ("open-account-settings", "open-global-settings"):
        assert html.index(el) > actions, f"{el} must stay in the right group"
    # The logo is the product name, not the page name.
    assert "TRAIDER Dashboard" not in html
    assert "📈 TRAIDER<" in html


def test_the_two_header_controls_are_coloured_by_different_things():
    """The account type tints the outlined pill; the run state fills the button.
    They are deliberately different shapes AND different scales, because a live
    account with trading off and a paper account with trading on must not look
    alike at a glance."""
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")

    def rule(selector):
        start = css.index(selector + " {")
        return css[start:css.index("}", start)]

    paper, live = rule(".exec-select.paper"), rule(".exec-select.live")
    assert paper != live
    # "Would be refused" is a RING. If it also repainted the pill, the warning
    # would hide the account type it is warning about.
    blocked = rule(".exec-select.blocked")
    assert "box-shadow" in blocked
    assert not re.search(r"(?<![-a-z])color\s*:", blocked), "blocked must not recolour the pill"
    assert not re.search(r"(?<![-a-z])background-color\s*:", blocked)

    off, on = rule(".exec-toggle.off"), rule(".exec-toggle.on")
    assert off != on
    # Green = armed, grey = not. The extra ring is for "armed with real money".
    assert ".exec-toggle.on.live" in css
