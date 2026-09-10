"""Tests for the Web Portal config (.env) service + API endpoints.

The service reads/writes the project-root ``.env``; tests redirect it to a
temp directory so nothing in the repo is touched.
"""

from fastapi.testclient import TestClient

from src.web.app import app
from src.web.services import config_service

client = TestClient(app)


def _point_env_to(tmp_path):
    path = tmp_path / ".env"
    config_service.env_file_path = lambda: path
    return path


# ---------------------------------------------------------------------------
# service: schema (GET)
# ---------------------------------------------------------------------------
def test_get_config_schema_returns_sections(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("DECISION_TIME=08:15\n", encoding="utf-8")
    cfg = config_service.get_config_schema()
    assert cfg["file_exists"] is True
    assert cfg["file"]
    names = [s["name"] for s in cfg["sections"]]
    assert "Trading" in names and "Model" in names and "Web Portal" in names
    trading = next(s for s in cfg["sections"] if s["name"] == "Trading")
    decision = next(f for f in trading["fields"] if f["key"] == "DECISION_TIME")
    assert decision["value"] == "08:15"
    assert decision["set"] is True
    assert decision["type"] == "str"


def test_get_config_uses_defaults_when_unset(tmp_path):
    _point_env_to(tmp_path)  # no .env file yet
    cfg = config_service.get_config_schema()
    assert cfg["file_exists"] is False
    trading = next(s for s in cfg["sections"] if s["name"] == "Trading")
    decision = next(f for f in trading["fields"] if f["key"] == "DECISION_TIME")
    assert decision["value"] == "09:45"
    assert decision["set"] is False


def test_get_config_masks_secrets(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("OPENBB_API_KEY=supersecret123\nWEB_PORTAL_PASSWORD=hunter2\n", encoding="utf-8")
    cfg = config_service.get_config_schema()
    fields = [f for s in cfg["sections"] for f in s["fields"]]
    key = next(f for f in fields if f["key"] == "OPENBB_API_KEY")
    assert key["sensitive"] is True
    assert key["value"] == config_service.MASK
    pwd = next(f for f in fields if f["key"] == "WEB_PORTAL_PASSWORD")
    assert pwd["sensitive"] is True
    assert pwd["value"] == config_service.MASK


def test_get_config_types(tmp_path):
    _point_env_to(tmp_path)
    cfg = config_service.get_config_schema()
    fields = {f["key"]: f for s in cfg["sections"] for f in s["fields"]}
    assert fields["DECISION_INTERVAL_HOURS"]["type"] == "int"
    assert fields["TRAIN_TEST_SPLIT"]["type"] == "float"
    assert fields["PAPER_TRADING"]["type"] == "bool"
    assert fields["MODEL_TYPE"]["options"] == ["logistic_regression", "rule_based"]


# ---------------------------------------------------------------------------
# service: update (POST)
# ---------------------------------------------------------------------------
def test_update_config_writes_values_and_keeps_secret(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("# header\nCACHE_DIR=.cache\nOPENBB_API_KEY=oldsecret\n", encoding="utf-8")

    res = config_service.update_config(
        {"CACHE_DIR": ".alt_cache", "OPENBB_API_KEY": "", "GATE_MIN_SHARPE": "1.5"}
    )
    assert res["ok"] is True
    assert "CACHE_DIR" in res["updated"]
    assert "GATE_MIN_SHARPE" in res["updated"]

    content = path.read_text(encoding="utf-8")
    assert "# header" in content              # comments preserved
    assert "CACHE_DIR=.alt_cache" in content  # value updated in place
    assert "OPENBB_API_KEY=oldsecret" in content  # empty submission kept secret
    assert "GATE_MIN_SHARPE=1.5" in content   # new key appended


def test_update_config_replaces_masked_secret(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("WEB_PORTAL_PASSWORD=hunter2\n", encoding="utf-8")
    res = config_service.update_config({"WEB_PORTAL_PASSWORD": "newpass"})
    assert res["ok"] is True
    assert "WEB_PORTAL_PASSWORD=newpass" in path.read_text(encoding="utf-8")


def test_update_config_invalid_value(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("INSTRUMENT=AAPL\n", encoding="utf-8")
    res = config_service.update_config({"MODEL_TYPE": "not_a_real_model"})
    assert res["ok"] is False
    assert res["errors"]
    # file untouched on validation failure
    assert "MODEL_TYPE=" not in path.read_text(encoding="utf-8").replace("INSTRUMENT=AAPL", "")


def test_update_config_creates_file_when_missing(tmp_path):
    path = _point_env_to(tmp_path)
    res = config_service.update_config({"CACHE_DIR": ".cache"})
    assert res["ok"] is True
    assert path.exists()
    assert "CACHE_DIR=.cache" in path.read_text(encoding="utf-8")


def test_instrument_is_strategy_scoped_not_global(tmp_path):
    _point_env_to(tmp_path)
    cfg = config_service.get_config_schema()
    fields = {f["key"] for s in cfg["sections"] for f in s["fields"]}
    assert "INSTRUMENT" not in fields  # moved into the per-strategy config


def test_update_config_ignores_instrument(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("INSTRUMENT=AAPL\n", encoding="utf-8")
    res = config_service.update_config({"INSTRUMENT": "tsla"})
    assert res["ok"] is True
    assert "INSTRUMENT" not in res["updated"]
    assert "INSTRUMENT=AAPL" in path.read_text(encoding="utf-8")  # untouched


def test_per_strategy_config_not_in_global_schema(tmp_path):
    _point_env_to(tmp_path)
    cfg = config_service.get_config_schema()
    keys = {f["key"] for s in cfg["sections"] for f in s["fields"]}
    assert "INSTRUMENT" not in keys
    assert "HISTORICAL_BAR_SIZE" not in keys
    assert "FEATURE_SMA_ENABLED" not in keys
    assert "FEATURES_RSI_PERIOD" not in keys
    assert "RISK_LIMIT_PERCENT" not in keys
    assert "MODEL_BUY_THRESHOLD" not in keys


def test_strategy_config_groups_schema(tmp_path):
    from src.config.settings import Settings as S

    _point_env_to(tmp_path)
    groups = config_service.strategy_config_groups(S(_env_file=None))
    names = [g["name"] for g in groups]
    assert "Instrument" in names
    assert "Model" in names
    assert "Features" in names and "Feature Parameters" in names and "Risk Management" in names
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    assert by_key["INSTRUMENT"]["type"] == "str"
    assert by_key["HISTORICAL_BAR_SIZE"]["type"] == "str"
    assert by_key["FEATURE_RSI_ENABLED"]["type"] == "bool"
    assert by_key["FEATURE_RSI_ENABLED"]["value"] == "True"
    assert by_key["FEATURE_SMA_ENABLED"]["label"] == "Simple Moving Average (SMA)"
    assert by_key["FEATURES_RSI_PERIOD"]["type"] == "int"
    assert by_key["MODEL_BUY_THRESHOLD"]["type"] == "float"
    assert by_key["POSITION_SIZING_MODE"]["options"] == ["fixed_risk", "volatility_target"]
    # overrides win over the global default
    over = config_service.strategy_config_groups(S(_env_file=None), {"MODEL_BUY_THRESHOLD": "0.42"})
    ob = {f["key"]: f for g in over for f in g["fields"]}
    assert ob["MODEL_BUY_THRESHOLD"]["value"] == "0.42"


def test_update_config_ignores_strategy_scoped_keys(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("FEATURE_RSI_ENABLED=True\n", encoding="utf-8")
    res = config_service.update_config({"FEATURE_RSI_ENABLED": "off"})
    assert res["ok"] is True
    assert "FEATURE_RSI_ENABLED" not in res["updated"]
    assert "FEATURE_RSI_ENABLED=True" in path.read_text(encoding="utf-8")  # untouched


def test_validate_strategy_config():
    from src.config.settings import Settings as S

    ok, errs = config_service.validate_strategy_config({"MODEL_BUY_THRESHOLD": "0.5"})
    assert ok and not errs
    ok, errs = config_service.validate_strategy_config({"MODEL_BUY_THRESHOLD": "1.5"})  # out of 0..1
    assert not ok and errs
    ok, errs = config_service.validate_strategy_config({"MODEL_TYPE": "rule_based"})  # not scoped
    assert not ok and errs
    ok, errs = config_service.validate_strategy_config({"FEATURE_RSI_ENABLED": "off"})  # bool spelling
    assert ok and not errs


def test_feature_toggle_on_off_parsing():
    from src.config.settings import Settings as S

    assert S(feature_sma_enabled="on").feature_sma_enabled is True
    assert S(feature_rsi_enabled="off").feature_rsi_enabled is False
    assert S(feature_atr_enabled="0").feature_atr_enabled is False
    assert S(feature_bollinger_enabled="true").feature_bollinger_enabled is True


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
def test_endpoint_get_config(monkeypatch):
    monkeypatch.setattr(
        config_service,
        "get_config_schema",
        lambda: {
            "file": "/x/.env",
            "file_exists": True,
            "sections": [
                {"name": "Trading", "fields": [{"key": "INSTRUMENT", "value": "AAPL"}]}
            ],
        },
    )
    r = client.get("/api/v1/config")
    assert r.status_code == 200
    assert r.json()["sections"][0]["name"] == "Trading"


def test_endpoint_post_config(monkeypatch):
    monkeypatch.setattr(
        config_service,
        "update_config",
        lambda values: {"ok": True, "message": "Saved 1 setting(s)", "errors": [], "updated": ["INSTRUMENT"]},
    )
    r = client.post("/api/v1/config", json={"values": {"INSTRUMENT": "MSFT"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
