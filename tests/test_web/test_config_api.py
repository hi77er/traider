"""Tests for the Web Portal config (.env) service + API endpoints.

The service reads/writes the project-root ``.env``; tests redirect it to a
temp directory so nothing in the repo is touched.
"""

import json

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
    path.write_text("OPENBB_PROVIDER=polygon\n", encoding="utf-8")
    cfg = config_service.get_config_schema()
    assert cfg["file_exists"] is True
    assert cfg["file"]
    # Only the machine-level settings stay global.
    assert [s["name"] for s in cfg["sections"]] == ["Market Data"]
    market = next(s for s in cfg["sections"] if s["name"] == "Market Data")
    provider = next(f for f in market["fields"] if f["key"] == "OPENBB_PROVIDER")
    assert provider["value"] == "polygon"
    assert provider["set"] is True
    assert provider["type"] == "str"


def test_moved_settings_are_not_in_the_global_form(tmp_path):
    """Trading/model/gates/scheduler moved to the strategy layer; broker,
    folders, backtest and cloud storage to the account layer."""
    _point_env_to(tmp_path)
    keys = {f["key"] for s in config_service.get_config_schema()["sections"] for f in s["fields"]}
    assert keys == {"OPENBB_PROVIDER", "OPENBB_API_KEY", "OPENBB_BACKUP_PROVIDERS"}
    for moved in ("DECISION_TIME", "DATA_DELTA_PULL_TIME", "TRADING_START_HOUR",
                  "MARKET_TIMEZONE", "MODEL_TYPE", "GATE_MIN_SHARPE",
                  "SCHEDULER_ENABLED", "ALPACA_PAPER_API_KEY", "DATA_DIR",
                  "CACHE_DIR", "TRAIN_TEST_SPLIT", "S3_BUCKET", "DYNAMODB_TABLE"):
        assert moved not in keys, moved


def test_get_config_uses_defaults_when_unset(tmp_path):
    _point_env_to(tmp_path)  # no .env file yet
    cfg = config_service.get_config_schema()
    assert cfg["file_exists"] is False
    market = next(s for s in cfg["sections"] if s["name"] == "Market Data")
    provider = next(f for f in market["fields"] if f["key"] == "OPENBB_PROVIDER")
    assert provider["value"] == "yfinance"
    assert provider["set"] is False


def test_get_config_masks_secrets(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("OPENBB_API_KEY=supersecret123\n", encoding="utf-8")
    cfg = config_service.get_config_schema()
    fields = [f for s in cfg["sections"] for f in s["fields"]]
    key = next(f for f in fields if f["key"] == "OPENBB_API_KEY")
    assert key["sensitive"] is True
    assert key["value"] == config_service.MASK


def test_get_config_types(tmp_path):
    _point_env_to(tmp_path)
    cfg = config_service.get_config_schema()
    fields = {f["key"]: f for s in cfg["sections"] for f in s["fields"]}
    assert fields["OPENBB_PROVIDER"]["type"] == "str"
    # The typed fields now live in the strategy / account schemas.
    from src.config.settings import Settings as S

    sg = {f["key"]: f for g in config_service.strategy_config_groups(S(_env_file=None)) for f in g["fields"]}
    assert sg["DECISION_INTERVAL_HOURS"]["type"] == "int"
    assert sg["MODEL_TYPE"]["options"] == ["logistic_regression", "rule_based"]
    ag = {f["key"]: f for g in config_service.account_sections(S(_env_file=None)) for f in g["fields"]}
    assert ag["TRAIN_TEST_SPLIT"]["type"] == "float"
    # Credential fields are never echoed back to the browser.
    assert ag["ALPACA_PAPER_API_SECRET"]["sensitive"] is True
    assert ag["ALPACA_LIVE_API_KEY"]["sensitive"] is True


# ---------------------------------------------------------------------------
# service: update (POST)
# ---------------------------------------------------------------------------
def test_update_config_writes_values_and_keeps_secret(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("# header\nOPENBB_PROVIDER=yfinance\nOPENBB_API_KEY=oldsecret\n", encoding="utf-8")

    res = config_service.update_config(
        {"OPENBB_PROVIDER": "polygon", "OPENBB_API_KEY": "",
         "OPENBB_BACKUP_PROVIDERS": "yfinance,fmp"}
    )
    assert res["ok"] is True
    assert "OPENBB_PROVIDER" in res["updated"]
    assert "OPENBB_BACKUP_PROVIDERS" in res["updated"]

    content = path.read_text(encoding="utf-8")
    assert "# header" in content              # comments preserved
    assert "OPENBB_PROVIDER=polygon" in content  # value updated in place
    assert "OPENBB_API_KEY=oldsecret" in content  # empty submission kept secret
    assert "OPENBB_BACKUP_PROVIDERS=yfinance,fmp" in content  # new key appended


def test_update_config_replaces_masked_secret(tmp_path):
    path = _point_env_to(tmp_path)
    path.write_text("OPENBB_API_KEY=hunter2\n", encoding="utf-8")
    res = config_service.update_config({"OPENBB_API_KEY": "newpass"})
    assert res["ok"] is True
    assert "OPENBB_API_KEY=newpass" in path.read_text(encoding="utf-8")


def test_update_config_invalid_value(tmp_path):
    """Unchanged file values are validated too, so a bad one blocks the save."""
    path = _point_env_to(tmp_path)
    path.write_text("MODEL_BUY_THRESHOLD=1.5\n", encoding="utf-8")  # out of 0..1
    res = config_service.update_config({"OPENBB_PROVIDER": "yfinance"})
    assert res["ok"] is False
    assert res["errors"]
    # file untouched on validation failure
    assert path.read_text(encoding="utf-8") == "MODEL_BUY_THRESHOLD=1.5\n"


def test_update_config_creates_file_when_missing(tmp_path):
    path = _point_env_to(tmp_path)
    res = config_service.update_config({"OPENBB_BACKUP_PROVIDERS": "yfinance"})
    assert res["ok"] is True
    assert path.exists()
    assert "OPENBB_BACKUP_PROVIDERS=yfinance" in path.read_text(encoding="utf-8")


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
    assert by_key["FEATURE_EMA_ENABLED"]["label"] == "Exponential Moving Average (EMA)"
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
    ok, errs = config_service.validate_strategy_config({"MODEL_BUY_THRESHOLD": "0.5"})
    assert ok and not errs
    ok, errs = config_service.validate_strategy_config({"MODEL_BUY_THRESHOLD": "1.5"})  # out of 0..1
    assert not ok and errs
    # MODEL_TYPE moved INTO the strategy scope, so it is accepted now...
    ok, errs = config_service.validate_strategy_config({"MODEL_TYPE": "rule_based"})
    assert ok and not errs
    # ...while a machine/account key is still refused here.
    ok, errs = config_service.validate_strategy_config({"OPENBB_PROVIDER": "yfinance"})
    assert not ok and errs
    ok, errs = config_service.validate_strategy_config({"FEATURE_RSI_ENABLED": "off"})  # bool spelling
    assert ok and not errs


def test_strategy_scope_carries_trading_model_gates_scheduler():
    """Everything a strategy needs is editable per strategy (previously global)."""
    from src.config.settings import Settings as S

    by_key = {f["key"]: f for g in config_service.strategy_config_groups(S(_env_file=None))
              for f in g["fields"]}
    for key in ("DECISION_INTERVAL_HOURS", "MARKET_TIMEZONE", "TRADING_START_HOUR",
                "TRADING_END_HOUR", "DECISION_TIME", "DATA_DELTA_PULL_TIME",
                "MODEL_TYPE", "GATE_MIN_SHARPE", "GATE_MAX_DRAWDOWN_PERCENT",
                "GATE_MIN_WIN_RATE_PERCENT", "GATE_MAX_WEEKLY_LOSS_PERCENT",
                "SCHEDULER_ENABLED", "SCHEDULER_TIMEZONE"):
        assert key in by_key, key
        assert config_service.validate_strategy_config({key: by_key[key]["value"]})[0], key


# ---------------------------------------------------------------------------
# account settings (settings/account/account.json)
# ---------------------------------------------------------------------------
def test_account_schema_groups_and_derived_folders(tmp_path):
    from src.config.settings import Settings as S

    groups = config_service.account_sections(S(_env_file=None))
    names = [g["name"] for g in groups]
    assert names == ["Trading Account", "Data & Folders", "Backtest",
                     "Cloud Storage", "State Storage"]
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    # All four Alpaca credential fields are secrets, and a secret is never echoed
    # back through the schema — this machine may or may not have keys configured
    # (settings/account/account.json is local data), so the assertion is about the
    # MASK, not about the value being absent.
    for key in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
                "ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET"):
        assert by_key[key]["sensitive"] is True, f"{key} must be treated as a secret"
        assert by_key[key]["value"] in ("", "********"), f"{key} leaked its value"
    # The model's own default is unset, whatever the local files hold.
    assert not S(_env_file=None).alpaca_paper_api_key
    assert by_key["EXECUTION_MAX_RETRIES"]["type"] == "int"
    # IBKR was dropped in favour of Alpaca; nothing may linger.
    assert not [k for k in by_key if "IBKR" in k]
    # The paper/live MODE is per-strategy, so it is deliberately not an account key.
    assert "EXECUTION_ENV" not in by_key
    assert by_key["DATA_DIR"]["value"] == "data"
    # One folder for both subfolders, which are derived and not editable.
    assert by_key["HISTORICAL_DATA_DIR"]["readonly"] is True
    assert by_key["BACKTEST_DIR"]["readonly"] is True
    assert by_key["HISTORICAL_DATA_DIR"]["value"] == "data/historical"
    assert by_key["BACKTEST_DIR"]["value"] == "data/backtest_results"


def test_update_account_persists_json_and_drives_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(config_service.account_mod, "account_file_path",
                        lambda settings: tmp_path / "account.json")

    res = config_service.update_account({
        "DATA_DIR": "srv/data", "S3_BUCKET": "my-bucket",
        "ALPACA_PAPER_API_SECRET": "s3cret",
    })
    assert res["ok"] is True, res
    # Only what the user configures is stored — the two subfolders are derived.
    assert json.loads((tmp_path / "account.json").read_text())["settings"] == {
        "DATA_DIR": "srv/data",
        "S3_BUCKET": "my-bucket",
        "ALPACA_PAPER_API_SECRET": "s3cret",
    }
    by_key = {f["key"]: f for g in res["groups"] for f in g["fields"]}
    assert by_key["HISTORICAL_DATA_DIR"]["value"] == "srv/data/historical"
    assert by_key["ALPACA_PAPER_API_SECRET"]["value"] == config_service.MASK  # never echoed back

    # An empty secret field keeps the stored credential; unknown keys are refused.
    res2 = config_service.update_account({"DATA_DIR": "srv/data", "ALPACA_PAPER_API_SECRET": ""})
    assert res2["ok"] is True
    assert json.loads((tmp_path / "account.json").read_text())["settings"]["ALPACA_PAPER_API_SECRET"] == "s3cret"
    res3 = config_service.update_account({"INSTRUMENT": "TSLA"})
    assert res3["ok"] is False and res3["errors"]
    # The paper/live MODE belongs to the strategy, so the account form must refuse
    # it — otherwise there would be two competing places to set it.
    res4 = config_service.update_account({"EXECUTION_ENV": "live"})
    assert res4["ok"] is False and res4["errors"]
    # ...and so must a dropped IBKR key.
    res5 = config_service.update_account({"IBKR_ACCOUNT_ID": "DU999"})
    assert res5["ok"] is False and res5["errors"]


def test_account_api_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config_service.account_mod, "account_file_path",
                        lambda settings: tmp_path / "account.json")
    got = client.get("/api/v1/account")
    assert got.status_code == 200
    assert [g["name"] for g in got.json()["groups"]][0] == "Trading Account"
    posted = client.post("/api/v1/account", json={"values": {"S3_BUCKET": "my-bucket",
                                                          "ALPACA_PAPER_API_KEY": "PK"}})
    assert posted.status_code == 200
    assert posted.json()["ok"] is True
    assert (tmp_path / "account.json").exists()


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
