"""The two settings layers the portal still edits, and what is no longer there.

Tools to configure the bot without hand-editing JSON: the per-strategy settings
(``settings/strategies/store.json``, rendered by the strategy panel) and the
per-account settings (``settings/account/account.json``, the 🏦 popup).

The global ``.env`` form used to be a third surface. It is gone — global settings are
edited in ``.env`` itself — and the tests that pinned its behaviour went with it. What
remains here is the part that still has a caller, plus a guard so the removal cannot
be undone halfway: an endpoint without a button, or a form whose fields no longer
exist.
"""

import json

from fastapi.testclient import TestClient

from src.config.settings import Settings as S
from src.web.app import app
from src.web.services import config_service

client = TestClient(app)
ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# per-strategy settings (the strategy panel)
# ---------------------------------------------------------------------------
def test_strategy_config_groups_schema():
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


def test_validate_strategy_config():
    ok, errs = config_service.validate_strategy_config({"MODEL_BUY_THRESHOLD": "0.5"})
    assert ok and not errs
    ok, errs = config_service.validate_strategy_config({"MODEL_BUY_THRESHOLD": "1.5"})  # out of 0..1
    assert not ok and errs
    ok, errs = config_service.validate_strategy_config({"MODEL_TYPE": "rule_based"})
    assert ok and not errs
    # A machine-level key is refused here: it is not a strategy's business.
    ok, errs = config_service.validate_strategy_config({"OPENBB_PROVIDER": "yfinance"})
    assert not ok and errs
    ok, errs = config_service.validate_strategy_config({"FEATURE_RSI_ENABLED": "off"})  # bool spelling
    assert ok and not errs


def test_strategy_scope_carries_trading_model_gates_scheduler():
    """Everything a strategy needs is editable per strategy."""
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
def test_account_schema_groups_and_derived_folders():
    groups = config_service.account_sections(S(_env_file=None))
    names = [g["name"] for g in groups]
    assert names == ["Trading Account", "Data & Folders", "Backtest",
                     "Cloud Storage", "State Storage — postponed"]
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    # All four Alpaca credential fields are secrets, and a secret is never echoed
    # back through the schema — this machine may or may not have keys configured
    # (settings/account/account.json is local data), so the assertion is about the
    # MASK, not about the value being absent.
    for key in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
                "ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET"):
        assert by_key[key]["sensitive"] is True, f"{key} must be treated as a secret"
        assert by_key[key]["value"] in ("", "********"), f"{key} leaked its value"
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


def test_the_removed_account_settings_are_gone_from_the_form():
    """Dates and the split were three answers to questions the strategy panel already
    answers: the history window is the strategy's lookback and bar size, a backtest is
    triggered by hand and covers the strategy's whole period, and the only model in use
    is rule-based (nothing to train on). A second place to set them would drift."""
    by_key = {f["key"]: f for g in config_service.account_sections(S(_env_file=None))
              for f in g["fields"]}
    for gone in ("HISTORICAL_START_DATE", "HISTORICAL_END_DATE",
                 "BACKTEST_START_DATE", "BACKTEST_END_DATE", "TRAIN_TEST_SPLIT"):
        assert gone not in by_key, f"{gone} must not be offered any more"
        assert gone not in config_service.ACCOUNT_SCOPED_KEYS, "nor writable through the API"
    # ...and the settings themselves still exist on the model: the dataset code and the
    # backtester read them. Unset is a real value here — no backtest window means the
    # strategy's whole period, which is exactly the behaviour wanted.
    for field in ("historical_start_date", "backtest_start_date", "backtest_end_date",
                  "train_test_split"):
        assert field in S.model_fields, field
    defaults = S(_env_file=None)
    assert defaults.backtest_start_date is None and defaults.backtest_end_date is None
    assert defaults.train_test_split == 0.8


def test_state_storage_is_named_as_postponed():
    """It is not built, and when it is it belongs in `.env` with the other
    infrastructure settings — so the section says so rather than looking ready."""
    names = [g["name"] for g in config_service.account_sections(S(_env_file=None))]
    assert "State Storage" not in names
    assert "State Storage — postponed" in names


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
    # ...and so must a dropped IBKR key, or a date the form no longer offers.
    for gone in ("IBKR_ACCOUNT_ID", "BACKTEST_START_DATE", "TRAIN_TEST_SPLIT"):
        refused = config_service.update_account({gone: "x"})
        assert refused["ok"] is False and refused["errors"], gone


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
    assert S(feature_sma_enabled="on").feature_sma_enabled is True
    assert S(feature_rsi_enabled="off").feature_rsi_enabled is False
    assert S(feature_atr_enabled="0").feature_atr_enabled is False
    assert S(feature_bollinger_enabled="true").feature_bollinger_enabled is True


# ---------------------------------------------------------------------------
# the global (.env) form is gone — all of it
# ---------------------------------------------------------------------------
def test_the_config_endpoint_is_gone():
    """Half a removal is worse than none: a live endpoint writing `.env` behind a
    button nobody can see would be configuration the operator cannot audit."""
    assert client.get("/api/v1/config").status_code == 404
    assert client.post("/api/v1/config", json={"values": {}}).status_code == 404
    assert not (ROOT / "src" / "web" / "routes" / "config.py").exists()


def test_the_service_no_longer_writes_dotenv():
    for name in ("get_config_schema", "update_config", "env_file_path", "_read_env",
                 "_parse_env_file", "_replace_value", "_atomic_write", "_section_for",
                 "_hidden_from_global"):
        assert not hasattr(config_service, name), f"{name} should be gone with the form"


def test_nothing_in_the_ui_still_opens_the_global_form():
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for gone in ("open-global-settings", "global-settings-backdrop", "settings-fields",
                 'id="save-config"', "reload-config", 'id="config-msg"'):
        assert gone not in html, f"{gone} is still in the template"
    for gone in ("openGlobalSettings", "closeGlobalSettings", "loadConfig", "saveConfig",
                 "showSaveErrors", "state.config"):
        assert gone not in js, f"{gone} is still in app.js"
    # The renderer stays: the account popup and the strategy panel share it.
    assert "function fieldInput(" in js
