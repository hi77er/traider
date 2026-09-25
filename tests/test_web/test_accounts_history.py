"""The account's equity THROUGH the session — the curve under the log page's price chart.

The loop's own records carry no equity: a tick says what it decided, not what the account was
worth, and a page that sampled the balance itself would only know the moments it happened to be
open. So the series comes from the broker's own history (``GET /v2/account/portfolio/history``),
which is also the only source that has the marks BETWEEN ticks.

Two things are pinned here, and the second one is the reason the first is trustworthy:

* a null equity point is DROPPED, never drawn as zero. Alpaca pads a point whose value it does
  not have with a null, and a curve that plots those as $0 has invented a 100% drawdown that
  never happened. The account's opening value is exactly where that padding lands;
* every failure — no credentials, a broker that refuses, a payload without the arrays — comes
  back as ``ok: False`` with a reason and no points. The panel this feeds sits under a chart
  that must still draw, so a curve that cannot be read is a sentence, not an exception.
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

#: Trimmed from a real response: aligned arrays, five-minute steps, and the null padding at the
#: start that the account's own opening value sits in.
HISTORY = {
    "timestamp": [1789977600, 1789977900, 1789978200, 1789978500],
    "equity": [None, "100000", 100250.5, "99800"],
    "profit_loss": [0.0, 0.0, 250.5, -200.0],
    "profit_loss_pct": [0.0, 0.0, 0.0025, -0.002],
    "base_value": 100000,
    "timeframe": "5Min",
}


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="AAPL",
        historical_bar_size="5m",
        **kwargs,
    )


def _history(monkeypatch, answer, calls=None):
    """The broker door for this read: a payload, or an exception to raise."""

    def probe(viewed, env, period, timeframe):
        if calls is not None:
            calls.append((env, period, timeframe))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(accounts, "probe_history", probe)


def test_the_curve_is_the_brokers_own_points(tmp_path, monkeypatch):
    _history(monkeypatch, HISTORY)

    out = accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert out["ok"] is True and out["env"] == "paper"
    assert out["base_value"] == 100000
    assert out["timeframe"] == "5Min"
    assert [p["time"] for p in out["points"]] == [1789977900, 1789978200, 1789978500], (
        "ascending, and the null-padded point is not one of them"
    )


def test_a_point_with_no_equity_is_dropped_rather_than_drawn_at_zero(tmp_path, monkeypatch):
    """A null is the ABSENCE of a balance. Plotting it as $0 draws a 100% drawdown that never
    happened, and the value it usually pads is the account's opening figure — the one point a
    curve of the day most needs to be right about."""
    _history(monkeypatch, {"timestamp": [1, 2], "equity": [None, 100000]})

    out = accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert [p["equity"] for p in out["points"]] == [100000.0]
    assert all(p["equity"] != 0 for p in out["points"])


def test_money_arrives_as_strings_and_lands_as_numbers(tmp_path, monkeypatch):
    """Alpaca sends money as JSON-decimal strings; a curve of strings would sort and subtract
    like text. Converted once, at the boundary, like every other number in this module."""
    _history(monkeypatch, {"timestamp": [1], "equity": ["101234.567"]})

    out = accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert out["points"] == [{"time": 1, "equity": 101234.57}]


def test_an_unparseable_stamp_is_skipped_not_turned_into_the_epoch(tmp_path, monkeypatch):
    _history(monkeypatch, {"timestamp": ["not a time", 42], "equity": ["1", "2"]})

    out = accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert [p["time"] for p in out["points"]] == [42]


def test_no_credentials_is_a_reason_rather_than_a_request(tmp_path, monkeypatch):
    """The state a live account is in on this machine: nothing to ask, and nothing to invent."""
    calls = []
    _history(monkeypatch, HISTORY, calls)

    out = accounts.history(_settings(tmp_path), "live")

    assert out["ok"] is False and out["points"] == []
    assert "live" in out["reason"]
    assert calls == [], "an account with no key is not asked about"


def test_a_refused_read_degrades_with_the_brokers_own_words(tmp_path, monkeypatch):
    _history(monkeypatch, AlpacaError("401 Unauthorized"))

    out = accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert out["ok"] is False and out["points"] == []
    assert "401" in out["reason"]


def test_an_empty_payload_is_not_a_flat_account(tmp_path, monkeypatch):
    """No arrays at all is unknown, which must not render as a line at zero."""
    _history(monkeypatch, {})

    out = accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert out["ok"] is False and out["points"] == []


def test_the_read_is_not_cached(tmp_path, monkeypatch):
    """Deliberate, and the opposite of ``snapshot``: this is drawn as a curve beside a live
    price chart, and a series served from a half-minute cache lags the line next to it."""
    calls = []
    _history(monkeypatch, HISTORY, calls)

    st = _settings(tmp_path, **PAPER_KEYS)
    accounts.history(st, "paper")
    accounts.history(st, "paper")

    assert len(calls) == 2


def test_it_asks_for_the_session_at_five_minute_steps(tmp_path, monkeypatch):
    calls = []
    _history(monkeypatch, HISTORY, calls)

    accounts.history(_settings(tmp_path, **PAPER_KEYS), "paper")

    assert calls == [("paper", "1D", "5Min")]


# ---------------------------------------------------------------------------
# the endpoint the log page calls
# ---------------------------------------------------------------------------
@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    settings = _settings(tmp_path, **PAPER_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    yield settings
    app.dependency_overrides.clear()


def test_the_endpoint_returns_the_curve(api_settings, monkeypatch):
    _history(monkeypatch, HISTORY)

    body = client.get("/api/v1/accounts/history", params={"env": "paper"}).json()

    assert body["ok"] is True
    assert [p["equity"] for p in body["points"]] == [100000.0, 100250.5, 99800.0]


def test_the_endpoint_defaults_to_the_account_in_play(api_settings, monkeypatch):
    """One curve for one account: two lines under one price chart would be read as one account
    with two names."""
    calls = []
    _history(monkeypatch, HISTORY, calls)

    body = client.get("/api/v1/accounts/history").json()

    assert body["env"] == "paper", "the settings' own execution_env"
    assert calls and calls[0][0] == "paper"


def test_the_endpoint_degrades_instead_of_failing(api_settings, monkeypatch):
    """The chart above this pane must draw whether or not the account can be read."""
    _history(monkeypatch, AlpacaError("the broker is down"))

    response = client.get("/api/v1/accounts/history")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["points"] == []
    assert "the broker is down" in body["reason"]
