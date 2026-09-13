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
# config additions: schedule times in schema + validation
# ---------------------------------------------------------------------------
def test_new_schedule_fields_in_config_schema(monkeypatch):
    import tempfile
    from src.web.services import config_service

    with tempfile.TemporaryDirectory() as td:
        path = config_service.env_file_path()
        config_service.env_file_path = lambda: __import__("pathlib").Path(td) / ".env"
        try:
            cfg = config_service.get_config_schema()
        finally:
            config_service.env_file_path = lambda: path

    # The schedules are per-strategy now, so they render in the strategy panel
    # instead of the global .env form.
    from src.config.settings import Settings as S

    groups = config_service.strategy_config_groups(S(_env_file=None))
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    assert by_key["DECISION_TIME"]["value"] == "09:45"
    assert by_key["DATA_DELTA_PULL_TIME"]["value"] == "16:30"
    trading = next(g for g in groups if g["name"] == "Trading")
    keys = [f["key"] for f in trading["fields"]]
    assert "DECISION_TIME" in keys and "DATA_DELTA_PULL_TIME" in keys
    global_keys = {f["key"] for s in cfg["sections"] for f in s["fields"]}
    assert "DECISION_TIME" not in global_keys and "DATA_DELTA_PULL_TIME" not in global_keys


def test_schedule_time_validation():
    Settings(decision_time="07:15", data_delta_pull_time="16:45")
    with pytest.raises(ValidationError):
        Settings(decision_time="25:00")
    with pytest.raises(ValidationError):
        Settings(data_delta_pull_time="4pm")
