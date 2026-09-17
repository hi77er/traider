"""The exchange clock — whether the market is open, and when it next changes.

The failure this file exists to prevent is a plausible lie. "The market is closed" and "we
could not read the clock" look the same on a panel and could not be more different: the first
tells an operator to come back at 09:30, and the second tells them nothing at all. So the
distinction is tested rather than the happy path — including the payload that arrives without
``is_open`` at all, which is the shape that would otherwise be read as closed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.execution.alpaca_client import AlpacaError
from src.web.app import app
from src.web.services import clock_service

client = TestClient(app)

PAPER_KEYS = {"alpaca_paper_api_key": "PK-PAPER", "alpaca_paper_api_secret": "S-PAPER"}

CLOSED = {
    "is_open": False,
    "timestamp": "2026-09-17T07:29:41-04:00",
    "next_open": "2026-09-17T09:30:00-04:00",
    "next_close": "2026-09-17T16:00:00-04:00",
}
OPEN = {
    "is_open": True,
    "timestamp": "2026-09-17T10:15:00-04:00",
    "next_open": "2026-09-18T09:30:00-04:00",
    "next_close": "2026-09-17T16:00:00-04:00",
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
    clock_service.forget()
    yield
    clock_service.forget()


def _clock(monkeypatch, payload, calls=None):
    def probe(executor):
        if calls is not None:
            calls.append(1)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(clock_service, "probe", probe)


# ---------------------------------------------------------------------------
# the two answers, and the one that is neither
# ---------------------------------------------------------------------------
def test_a_closed_market_says_when_it_opens(tmp_path, monkeypatch):
    """The whole point: not just "shut", but "shut until 09:30"."""
    _clock(monkeypatch, CLOSED)

    answer = clock_service.state(_settings(tmp_path, **PAPER_KEYS))

    assert answer["ok"] is True
    assert answer["is_open"] is False
    assert answer["next_open"] == "2026-09-17T09:30:00-04:00"


def test_an_open_market_says_when_it_closes(tmp_path, monkeypatch):
    _clock(monkeypatch, OPEN)

    answer = clock_service.state(_settings(tmp_path, **PAPER_KEYS))

    assert answer["ok"] is True and answer["is_open"] is True
    assert answer["next_close"] == "2026-09-17T16:00:00-04:00"


def test_an_unreadable_clock_is_unknown_and_never_closed(tmp_path, monkeypatch):
    """A 401 must not render as "the market is closed" — that is a fabrication about the
    exchange from a failure that had nothing to do with it."""
    _clock(monkeypatch, AlpacaError("unauthorized"))

    answer = clock_service.state(_settings(tmp_path, **PAPER_KEYS))

    assert answer["ok"] is False
    assert answer["is_open"] is None, "unknown is not False; the panel's wording depends on it"
    assert "could not be read" in answer["message"]
    assert answer["next_open"] is None


def test_a_payload_without_is_open_is_unknown_too(tmp_path, monkeypatch):
    """Absence of evidence is not evidence of a closed session."""
    _clock(monkeypatch, {"timestamp": "2026-09-17T10:15:00-04:00"})

    answer = clock_service.state(_settings(tmp_path, **PAPER_KEYS))

    assert answer["ok"] is False and answer["is_open"] is None
    assert "did not say whether the market is open" in answer["message"]


def test_no_credentials_is_unknown_and_costs_no_call(tmp_path, monkeypatch):
    """A data-only install has no clock to ask, and must not try."""
    calls = []
    _clock(monkeypatch, CLOSED, calls=calls)

    answer = clock_service.state(_settings(tmp_path))

    assert answer["ok"] is False and answer["is_open"] is None
    assert calls == [], "with no key there is nothing to ask, so nothing may be asked"


# ---------------------------------------------------------------------------
# the cache: a page left open all day must not be a broker call a second
# ---------------------------------------------------------------------------
def test_the_answer_stands_for_a_minute_and_then_is_re_read(tmp_path, monkeypatch):
    calls = []
    _clock(monkeypatch, CLOSED, calls=calls)
    moment = [1000.0]
    monkeypatch.setattr(clock_service, "_now", lambda: moment[0])

    settings = _settings(tmp_path, **PAPER_KEYS)
    clock_service.state(settings)
    moment[0] += clock_service.CACHE_SECONDS - 1
    clock_service.state(settings)
    assert len(calls) == 1, "a minute-old session is not a stale one — it moves at 09:30"

    moment[0] += 2  # over the TTL
    clock_service.state(settings)
    assert len(calls) == 2


def test_force_re_reads_the_clock(tmp_path, monkeypatch):
    calls = []
    _clock(monkeypatch, CLOSED, calls=calls)

    settings = _settings(tmp_path, **PAPER_KEYS)
    clock_service.state(settings)
    clock_service.state(settings, force=True)

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------
def test_the_clock_endpoint_serves_the_session(tmp_path, monkeypatch):
    _clock(monkeypatch, CLOSED)
    settings = _settings(tmp_path, **PAPER_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        response = client.get("/api/v1/clock")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["is_open"] is False
    assert body["next_open"] == "2026-09-17T09:30:00-04:00"


def test_the_clock_endpoint_degrades_instead_of_failing(tmp_path, monkeypatch):
    from src.web.routes import live as live_routes

    monkeypatch.setattr(
        live_routes.clock_service, "probe",
        lambda executor: (_ for _ in ()).throw(AlpacaError("the clock request timed out")),
    )
    settings = _settings(tmp_path, **PAPER_KEYS)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        response = client.get("/api/v1/clock")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["is_open"] is None
    assert "timed out" in body["message"]
