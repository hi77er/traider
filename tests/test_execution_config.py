"""Paper/live execution configuration: the switch, and the safeguards around it.

The design being pinned down here:

* The broker and its credentials belong to the trading ACCOUNT (one file shared
  by every strategy); the paper/live MODE belongs to each STRATEGY.
* Switching is one triple swap (base URL + key pair), because Alpaca's paper and
  live environments are the same API.
* Going live requires TWO independent keys — the environment AND an explicit
  acknowledgement — and a misconfiguration is always REFUSED rather than
  silently resolved somewhere unexpected. Quietly falling back would either hide
  a broken live setup or spend real money.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.backtest import engine
from src.config.settings import Settings
from src.execution.config import (
    LIVE_BASE_URL,
    PAPER_BASE_URL,
    ExecutionConfigError,
    execution_status,
    resolve_execution_target,
)
from src.web.app import app
from src.web.services import config_service

client = TestClient(app)

PAPER_KEYS = {"alpaca_paper_api_key": "PK-paper", "alpaca_paper_api_secret": "PS-paper"}
LIVE_KEYS = {"alpaca_live_api_key": "LK-live", "alpaca_live_api_secret": "LS-live"}


def _s(**kwargs) -> Settings:
    """Settings with no .env in play, so the test owns every value."""
    return Settings(_env_file=None, **kwargs)


# ---------------------------------------------------------------------------
# resolving the target (the thing an order uses)
# ---------------------------------------------------------------------------
def test_defaults_are_the_alpaca_paper_account():
    target = resolve_execution_target(_s(**PAPER_KEYS))
    assert target.broker == "alpaca"
    assert target.env == "paper"
    assert target.live is False
    assert target.base_url == PAPER_BASE_URL
    assert target.label == "PAPER"


def test_live_resolves_only_with_both_the_environment_and_the_acknowledgement():
    target = resolve_execution_target(
        _s(execution_env="live", execution_live_ack=True, **LIVE_KEYS)
    )
    assert target.live is True
    assert target.label == "LIVE"
    assert target.base_url == LIVE_BASE_URL
    assert target.key_id == "LK-live"


def test_paper_and_live_use_different_endpoints_and_keys():
    """The whole point: same code, one swapped triple."""
    paper = resolve_execution_target(_s(**PAPER_KEYS, **LIVE_KEYS))
    live = resolve_execution_target(
        _s(execution_env="live", execution_live_ack=True, **PAPER_KEYS, **LIVE_KEYS)
    )
    assert (paper.base_url, live.base_url) == (PAPER_BASE_URL, LIVE_BASE_URL)
    assert paper.key_id != live.key_id
    assert paper.secret != live.secret


@pytest.mark.parametrize(
    "overrides, expected",
    [
        # The acknowledgement is missing: one field flip must not start real trading.
        ({"execution_env": "live", **LIVE_KEYS}, "EXECUTION_LIVE_ACK"),
        # Acknowledged but no live credentials.
        ({"execution_env": "live", "execution_live_ack": True}, "LIVE API key/secret are missing"),
        # Paper keys are NOT accepted for live — they are different key pairs.
        ({"execution_env": "live", "execution_live_ack": True, **PAPER_KEYS}, "LIVE API key/secret are missing"),
        # Paper with no credentials cannot trade either.
        ({}, "PAPER API key/secret are not set"),
    ],
)
def test_misconfiguration_is_refused_not_guessed(overrides, expected):
    with pytest.raises(ExecutionConfigError) as excinfo:
        resolve_execution_target(_s(**overrides))
    assert expected in str(excinfo.value)


def test_an_unimplemented_broker_is_refused_rather_than_ignored():
    with pytest.raises(ExecutionConfigError) as excinfo:
        resolve_execution_target(_s(execution_broker="ibkr", **PAPER_KEYS))
    assert "no executor" in str(excinfo.value)


def test_a_secret_never_reaches_a_repr_or_log_line():
    target = resolve_execution_target(_s(**PAPER_KEYS))
    assert "PS-paper" not in repr(target)
    assert "PS-paper" not in str(target)


# ---------------------------------------------------------------------------
# the settings surface itself
# ---------------------------------------------------------------------------
def test_environment_and_broker_are_normalised_but_typos_are_rejected():
    assert _s(execution_env="LIVE", execution_broker=" Alpaca ").execution_env == "live"
    with pytest.raises(Exception):
        _s(execution_env="production")
    with pytest.raises(Exception):
        _s(execution_broker="trading212")


def test_paper_trading_is_kept_as_a_derived_alias():
    """The old boolean became EXECUTION_ENV; the alias keeps callers working."""
    assert _s(**PAPER_KEYS).paper_trading is True
    assert _s(execution_env="live", execution_live_ack=True, **LIVE_KEYS).paper_trading is False
    # Derived, not stored: it must not be an editable field or a saved value.
    assert "paper_trading" not in Settings.model_fields
    assert "paper_trading" not in Settings(_env_file=None).model_dump()

    # An old .env carrying PAPER_TRADING is ignored, not fatal (extra="ignore").
    assert _s(paper_trading="True").execution_env == "paper"


def test_the_mode_is_per_strategy_and_credentials_are_account_wide():
    strategy_keys = set(config_service.STRATEGY_SCOPED_KEYS)
    account_keys = set(config_service.ACCOUNT_SCOPED_KEYS)
    assert {"EXECUTION_ENV", "EXECUTION_LIVE_ACK"} <= strategy_keys
    assert {"EXECUTION_BROKER", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
            "ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET"} <= account_keys
    # Exactly one home for each, or the two layers would fight.
    assert not {"EXECUTION_ENV", "EXECUTION_LIVE_ACK"} & account_keys
    assert not {"EXECUTION_BROKER", "ALPACA_LIVE_API_KEY"} & strategy_keys
    # The old single switch is gone.
    assert "PAPER_TRADING" not in strategy_keys | account_keys


def test_the_paper_live_dropdown_spells_out_the_consequence():
    groups = config_service.strategy_config_groups(_s())
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    env = by_key["EXECUTION_ENV"]
    assert [o["value"] for o in env["options"]] == ["paper", "live"]
    assert "REAL" in env["options"][1]["label"]
    # It leads the panel, so the mode is the first thing you see.
    assert groups[0]["name"] == "Execution"


# ---------------------------------------------------------------------------
# status (never raises — the badge and the run stamp both rely on that)
# ---------------------------------------------------------------------------
def test_status_explains_why_orders_would_be_refused_instead_of_raising():
    status = execution_status(_s())
    assert status["ok"] is False
    assert status["env"] == "paper"
    assert "not set" in status["message"]


def test_status_reports_which_key_pairs_are_configured_without_revealing_them():
    status = execution_status(_s(**PAPER_KEYS))
    assert status["ok"] is True
    assert status["paper_keys_set"] is True
    assert status["live_keys_set"] is False
    assert "PK-paper" not in json.dumps(status)
    assert "Trading212" not in json.dumps(status)


def test_status_flags_a_live_selection_that_cannot_trade():
    status = execution_status(_s(execution_env="live", execution_live_ack=True))
    assert status["live"] is True
    assert status["ok"] is False
    assert status["base_url"] == LIVE_BASE_URL


# ---------------------------------------------------------------------------
# provenance: which environment did this run belong to?
# ---------------------------------------------------------------------------
def test_the_run_provenance_records_the_environment_without_secrets():
    snapshot = engine._execution_snapshot(_s(**PAPER_KEYS, **LIVE_KEYS))
    assert snapshot == {
        "broker": "alpaca",
        "env": "paper",
        "live": False,
        "base_url": PAPER_BASE_URL,
        "configured": True,
    }
    assert "PK-paper" not in json.dumps(snapshot)
    # A live strategy is stamped as live, so its results are never confused with
    # a paper run of the same rules.
    live = engine._execution_snapshot(
        _s(execution_env="live", execution_live_ack=True, **LIVE_KEYS)
    )
    assert live["live"] is True and live["env"] == "live"


# ---------------------------------------------------------------------------
# the API + the dashboard wiring
# ---------------------------------------------------------------------------
def test_status_endpoint_returns_the_resolved_environment():
    got = client.get("/api/v1/execution/status")
    assert got.status_code == 200
    body = got.json()
    assert set(body) == {
        "broker", "env", "live", "base_url", "ok", "message",
        "paper_keys_set", "live_keys_set",
    }
    assert body["env"] in ("paper", "live")


def test_the_dashboard_shows_which_environment_orders_would_use():
    """Both accounts look identical everywhere else, so the badge is the guard."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]  # tests/ -> repo root
    html = (root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="exec-badge"' in html
    js = (root / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "async function loadExecutionStatus()" in js
    assert "/api/v1/execution/status" in js
    # Rendered at boot, and re-rendered after the account form is saved (the
    # broker or the keys may have just changed).
    assert js.count("loadExecutionStatus()") >= 2
    assert js.count("await loadExecutionStatus()") >= 1

    css = (root / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    # `display` must NOT be set here or the UA [hidden] rule would stop working.
    assert ".exec-badge {" in css
    assert ".exec-badge.live" in css and ".exec-badge.paper" in css
