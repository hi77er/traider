"""Tests for the Daily Delta API endpoints.

The provider-fetching data functions are monkeypatched so no network is hit.
"""

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from src.config.settings import Settings
from src.web.app import app
from src.web.services import delta_service

client = TestClient(app)


def _fake_status(**over):
    payload = {
        "exists": True,
        "symbol": "AAPL",
        "interval": "1d",
        "last_date": "2026-09-01",
        "rows": 1170,
        "eligible_until": "2026-09-02",
        "missing": [],
        "synced": True,
        "last_available": "2026-09-01",
        "error": None,
        "recent": [
            {"date": "2026-08-28", "open": 320.1, "high": 322.0, "low": 319.0, "close": 321.5, "volume": 100},
        ],
    }
    payload.update(over)
    return payload


def test_endpoint_status_synced(monkeypatch):
    monkeypatch.setattr(delta_service, "_compute_status", lambda settings=None: _fake_status())
    r = client.get("/api/v1/delta/status")
    assert r.status_code == 200
    body = r.json()
    assert body["synced"] is True
    assert body["missing"] == []
    assert len(body["recent"]) == 1


def test_endpoint_status_missing(monkeypatch):
    monkeypatch.setattr(
        delta_service, "_compute_status",
        lambda settings=None: _fake_status(synced=False, missing=["2026-09-01", "2026-09-02"]),
    )
    r = client.get("/api/v1/delta/status")
    assert r.status_code == 200
    assert r.json()["missing"] == ["2026-09-01", "2026-09-02"]


def test_endpoint_sync(monkeypatch):
    monkeypatch.setattr(
        delta_service, "_sync_delta", lambda settings=None: _fake_status(rows=1172, last_date="2026-09-02")
    )
    r = client.post("/api/v1/delta/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 1172
    assert body["synced"] is True


def test_endpoint_status_error(monkeypatch):
    monkeypatch.setattr(
        delta_service, "_compute_status",
        lambda settings=None: _fake_status(
            exists=None, error="provider exploded", synced=False, missing=[], recent=[]
        ),
    )
    r = client.get("/api/v1/delta/status")
    assert r.status_code == 200
    assert "exploded" in r.json()["error"]


# ---------------------------------------------------------------------------
# schedule fields: rendered by the strategy panel, validated by Settings
# ---------------------------------------------------------------------------
def test_the_delta_pull_time_is_per_strategy():
    from src.config.settings import Settings as S
    from src.web.services import config_service

    # The schedules are per-strategy, so they render in the strategy panel. There is
    # no global .env form to keep them out of any more.
    groups = config_service.strategy_config_groups(S(_env_file=None))
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    assert by_key["DATA_DELTA_PULL_TIME"]["value"] == "16:30"
    trading = next(g for g in groups if g["name"] == "Trading")
    assert [f["key"] for f in trading["fields"]] == [
        "MARKET_TIMEZONE", "TRADING_START_HOUR", "TRADING_END_HOUR",
        "DATA_DELTA_PULL_TIME",
    ]


def test_the_decision_cadence_is_not_a_setting():
    """A decision is made on every newly generated bar — signal at a bar's close,
    fill at the next bar's open — so there is no "how often" to configure. Both keys
    were offered and documented while no code read them; they are now gone from
    ``Settings`` too, so a leftover line in `.env` cannot set a schedule the bot
    does not keep."""
    from src.config.settings import Settings as S
    from src.web.services import config_service

    fields = {name.upper() for name in S.model_fields}
    assert "DECISION_INTERVAL_HOURS" not in fields and "DECISION_TIME" not in fields
    offered = {f["key"] for g in config_service.strategy_config_groups(S(_env_file=None))
               for f in g["fields"]}
    assert not {"DECISION_INTERVAL_HOURS", "DECISION_TIME"} & offered
    # Retired, not hidden: a strategy stored while they existed still carries them and
    # the panel posts it back verbatim, so a save must DROP them rather than refuse it.
    assert {"DECISION_INTERVAL_HOURS", "DECISION_TIME"} <= config_service.RETIRED_STRATEGY_KEYS


def test_schedule_time_validation():
    Settings(data_delta_pull_time="16:45")
    with pytest.raises(ValidationError):
        Settings(data_delta_pull_time="25:00")
    with pytest.raises(ValidationError):
        Settings(data_delta_pull_time="4pm")
