"""What each account is worth — equity, the day's change, cash.

The distinction this file exists to protect is between **unknown** and **zero**. "$0.00" is a
claim about a balance; an account nobody could read is the absence of one, and a panel that
renders the second as the first tells an operator their money is gone. So the degrade paths are
tested as carefully as the numbers, and the raw broker payload is checked for NOT being
proxied — it carries the account number.

Both environments are read, which is why the real live account's rejected key shows up here as
a degraded row rather than as a failure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.execution import accounts
from src.execution.alpaca_client import AlpacaError
from src.web.app import app

client = TestClient(app)

PAPER_KEYS = {"alpaca_paper_api_key": "PK-PAPER", "alpaca_paper_api_secret": "S-PAPER"}
LIVE_KEYS = {"alpaca_live_api_key": "PK-LIVE", "alpaca_live_api_secret": "S-LIVE"}

#: Trimmed from a real Alpaca response, field names and all — including the two that must not
#: survive the projection.
PAYLOAD = {
    "id": "8f0e1b0e-0000-4000-8000-000000000001",
    "account_number": "PA36Z5MM1R9V",
    "status": "ACTIVE",
    "currency": "USD",
    "equity": "100000",
    "last_equity": "99000",
    "cash": "95000",
    "buying_power": "400000",
    "portfolio_value": "100000",
    "long_market_value": "5000",
    "short_market_value": "0",
    "multiplier": "4",
    "trading_blocked": False,
    "account_blocked": False,
}


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="AAPL",
        historical_bar_size="1h",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    """The cache is module-level, so one test's answer must not survive into the next."""
    accounts.forget()
    yield
    accounts.forget()


def _account(monkeypatch, by_env, calls=None):
    """The broker door: a payload per environment, or an exception to raise for it."""

    def probe(viewed, env):
        if calls is not None:
            calls.append(env)
        answer = by_env.get(env, {})
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(accounts, "probe", probe)


# ---------------------------------------------------------------------------
# the numbers
# ---------------------------------------------------------------------------
def test_equity_and_the_days_change_come_from_the_account(tmp_path, monkeypatch):
    _account(monkeypatch, {"paper": PAYLOAD})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper").as_dict()

    assert row["known"] is True
    assert row["equity"] == 100000
    assert row["day_pl"] == 1000.0, "equity - last_equity is the day's change"
    assert row["day_pl_pct"] == pytest.approx(1.0101, abs=1e-3)


def test_a_flat_day_reports_zero_rather_than_nothing(tmp_path, monkeypatch):
    """Zero is a real answer and must not be treated as missing — a freshly funded paper
    account is exactly this case."""
    flat = dict(PAYLOAD, equity="100000", last_equity="100000")
    _account(monkeypatch, {"paper": flat})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper").as_dict()

    assert row["day_pl"] == 0.0 and row["day_pl_pct"] == 0.0


def test_a_missing_previous_close_leaves_the_percentage_unknown(tmp_path, monkeypatch):
    """A percentage of nothing is not zero percent. With no denominator the change cannot be
    expressed as one, and printing "0.00%" would report a flat day that was never measured."""
    no_last = dict(PAYLOAD, last_equity="0")
    _account(monkeypatch, {"paper": no_last})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper").as_dict()

    assert row["day_pl"] == 100000.0
    assert row["day_pl_pct"] is None


def test_a_blocked_account_says_so(tmp_path, monkeypatch):
    _account(monkeypatch, {"paper": dict(PAYLOAD, trading_blocked=True)})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper").as_dict()

    assert row["blocked"] is True and row["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# what must never reach the browser
# ---------------------------------------------------------------------------
def test_the_account_number_is_masked_and_the_id_is_dropped(tmp_path, monkeypatch):
    """The payload carries an account number and an id, and a dashboard has no business
    forwarding them. Three characters is enough to tell two accounts apart."""
    _account(monkeypatch, {"paper": PAYLOAD})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper").as_dict()

    assert row["account"] == "****R9V"
    flat = str(row)
    assert PAYLOAD["account_number"] not in flat
    assert PAYLOAD["id"] not in flat


def test_the_route_does_not_proxy_the_payload(tmp_path, monkeypatch):
    """The same guarantee at the door the browser actually uses."""
    _account(monkeypatch, {"paper": PAYLOAD})
    settings = _settings(tmp_path, **PAPER_KEYS, **LIVE_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        body = client.get("/api/v1/accounts").json()
    finally:
        app.dependency_overrides.clear()

    assert PAYLOAD["account_number"] not in str(body)
    assert PAYLOAD["id"] not in str(body)
    assert [row["env"] for row in body["accounts"]] == ["paper", "live"]


def test_the_raw_payload_is_not_kept_in_memory_either(tmp_path, monkeypatch):
    """The other half of the projection, and the half nothing else can see.

    ``as_dict`` projects too, so a mutation that stored the broker's whole payload passed
    every other test here — it was invisible through the only public accessor. It is asserted
    anyway because projecting at the READ is what keeps an account number and an id from
    sitting in a thirty-second cache waiting for whoever writes the next accessor.
    """
    _account(monkeypatch, {"paper": PAYLOAD})

    account = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper")
    kept = dict(account.fields)

    assert "id" not in kept, "the broker's id has no reason to be stored at all"
    assert set(kept) <= set(accounts._FIELDS) | {"account_number"}, \
        "nothing outside the projection may be carried"


# ---------------------------------------------------------------------------
# unknown is not zero
# ---------------------------------------------------------------------------
def test_an_unreadable_account_is_unknown_with_its_reason(tmp_path, monkeypatch):
    _account(monkeypatch, {"paper": AlpacaError("unauthorized"), "live": PAYLOAD})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS, **LIVE_KEYS), "paper").as_dict()

    assert row["known"] is False
    assert row["equity"] is None, "not 0 — an unreadable account is not a broke one"
    assert row["cash"] is None and row["day_pl"] is None
    assert "could not be read" in row["reason"] and "unauthorized" in row["reason"]


def test_an_account_with_no_credentials_is_unknown_and_costs_no_call(tmp_path, monkeypatch):
    """A paper-only install must not report a live balance of zero, and must not ask."""
    calls = []
    _account(monkeypatch, {"paper": PAYLOAD}, calls=calls)

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "live").as_dict()

    assert row["known"] is False
    assert "no live credentials" in row["reason"]
    assert calls == [], "with no key there is nothing to ask, so nothing may be asked"


def test_an_empty_payload_is_not_a_zero_balance(tmp_path, monkeypatch):
    _account(monkeypatch, {"paper": {}})

    row = accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper").as_dict()

    assert row["known"] is False and row["equity"] is None


def test_one_broken_account_does_not_hide_the_other(tmp_path, monkeypatch):
    """The reason both are read: a rejected key on live must not blank paper's numbers."""
    _account(monkeypatch, {"paper": PAYLOAD, "live": AlpacaError("401")})

    rows = [row.as_dict() for row in accounts.snapshots(_settings(tmp_path, **PAPER_KEYS, **LIVE_KEYS))]

    assert rows[0]["known"] is True and rows[0]["equity"] == 100000
    assert rows[1]["known"] is False and rows[1]["equity"] is None


def test_the_route_degrades_instead_of_failing(tmp_path, monkeypatch):
    from src.web.routes import live as live_routes

    monkeypatch.setattr(
        live_routes.accounts, "probe",
        lambda viewed, env: (_ for _ in ()).throw(AlpacaError("the broker is down")),
    )
    settings = _settings(tmp_path, **PAPER_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        response = client.get("/api/v1/accounts")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert all(row["known"] is False for row in body["accounts"])


# ---------------------------------------------------------------------------
# the cache: a polled panel must not be a broker call a second
# ---------------------------------------------------------------------------
def test_the_answer_stands_for_half_a_minute_and_then_is_re_read(tmp_path, monkeypatch):
    calls = []
    _account(monkeypatch, {"paper": PAYLOAD}, calls=calls)
    moment = [1000.0]
    monkeypatch.setattr(accounts, "_now", lambda: moment[0])

    settings = _settings(tmp_path, **PAPER_KEYS)
    accounts.snapshot(settings, "paper")
    moment[0] += accounts.CACHE_SECONDS - 1
    accounts.snapshot(settings, "paper")
    assert len(calls) == 1

    moment[0] += 2  # over the TTL
    accounts.snapshot(settings, "paper")
    assert len(calls) == 2


def test_a_rotated_key_does_not_reuse_the_old_answer(tmp_path, monkeypatch):
    """The key is part of the cache key, so a changed key is a different account."""
    calls = []
    _account(monkeypatch, {"paper": PAYLOAD}, calls=calls)

    accounts.snapshot(_settings(tmp_path, **PAPER_KEYS), "paper")
    accounts.snapshot(
        _settings(tmp_path, alpaca_paper_api_key="PK-OTHER", alpaca_paper_api_secret="S-OTHER"),
        "paper",
    )

    assert len(calls) == 2
