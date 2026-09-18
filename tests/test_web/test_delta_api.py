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
# schedule fields: gone, and dropped from stored strategies rather than refused
# ---------------------------------------------------------------------------
def test_the_delta_pull_time_is_no_longer_a_setting():
    """The dataset is kept current by the delta CHECK and by the tick, whenever those
    happen to run — so a stored "pull at 16:30" was a schedule nothing kept. It was
    offered in the strategy panel and read by no code, and it is now gone from
    ``Settings`` too, so a leftover line in ``.env`` cannot set a time nothing honours.
    """
    from src.config.settings import Settings as S
    from src.web.services import config_service

    assert "DATA_DELTA_PULL_TIME" not in {name.upper() for name in S.model_fields}
    groups = config_service.strategy_config_groups(S(_env_file=None))
    offered = {f["key"] for g in groups for f in g["fields"]}
    assert "DATA_DELTA_PULL_TIME" not in offered
    trading = next(g for g in groups if g["name"] == "Trading")
    assert [f["key"] for f in trading["fields"]] == [
        "MARKET_TIMEZONE", "TRADING_START_HOUR", "TRADING_END_HOUR",
    ]
    # Retired, not hidden: a strategy stored while it existed still carries it, and the
    # panel posts that strategy back verbatim, so a save must DROP it rather than
    # refuse the whole config.
    assert "DATA_DELTA_PULL_TIME" in config_service.RETIRED_STRATEGY_KEYS


def test_the_decision_cadence_is_not_a_setting():
    """A decision is made on every newly generated bar — signal at a bar's close,
    fill at the next bar's open — so there is no "how often" to configure. Both keys
    were offered and documented while no code read them; they are now gone from
    ``Settings`` too, so a leftover line in `.env` cannot set a schedule the bot
    does not keep."""
    from src.config.settings import Settings as S
    from src.web.services import config_service

    fields = {name.upper() for name in S.model_fields}
    schedule = {"DECISION_INTERVAL_HOURS", "DECISION_TIME", "DATA_DELTA_PULL_TIME"}
    assert not schedule & fields
    offered = {f["key"] for g in config_service.strategy_config_groups(S(_env_file=None))
               for f in g["fields"]}
    assert not schedule & offered
    # Retired, not hidden: a strategy stored while they existed still carries them and
    # the panel posts it back verbatim, so a save must DROP them rather than refuse it.
    assert schedule <= config_service.RETIRED_STRATEGY_KEYS


def test_no_setting_validates_a_schedule_any_more():
    """The HH:MM validator went with the field it guarded.

    It is worth pinning the ABSENCE: a leftover ``DATA_DELTA_PULL_TIME=25:00`` in
    someone's `.env` must be inert (unknown keys are ignored), not a startup error.
    """
    s = Settings(data_delta_pull_time="25:00", data_delta_pull_time_typo="4pm")
    assert not hasattr(s, "data_delta_pull_time")
