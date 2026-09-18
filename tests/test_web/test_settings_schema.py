"""The two settings layers the portal still edits, and what is no longer there.

Tools to configure the bot without hand-editing JSON: the per-strategy settings
(``data/strategies/store.json``, rendered by the strategy panel) and the
per-account settings (``data/account/account.json``, the 🏦 popup).

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
    assert "Features" in names and "Feature Parameters" in names and "Risk Management" in names
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    assert by_key["INSTRUMENT"]["type"] == "str"
    assert by_key["HISTORICAL_BAR_SIZE"]["type"] == "str"
    assert by_key["FEATURE_RSI_ENABLED"]["type"] == "bool"
    assert by_key["FEATURE_RSI_ENABLED"]["value"] == "True"
    assert by_key["FEATURE_SMA_ENABLED"]["label"] == "Simple Moving Average (SMA)"
    assert by_key["FEATURE_EMA_ENABLED"]["label"] == "Exponential Moving Average (EMA)"
    assert by_key["FEATURES_RSI_PERIOD"]["type"] == "int"
    assert by_key["GATE_MIN_SHARPE"]["type"] == "float"
    assert by_key["POSITION_SIZING_MODE"]["options"] == ["fixed_risk", "volatility_target"]
    # overrides win over the global default
    over = config_service.strategy_config_groups(S(_env_file=None), {"GATE_MIN_SHARPE": "0.42"})
    ob = {f["key"]: f for g in over for f in g["fields"]}
    assert ob["GATE_MIN_SHARPE"]["value"] == "0.42"


def test_the_model_section_is_gone():
    """There is one model kind — rule-based — and it is a constant, not a setting, so
    MODEL_TYPE was a switch between a working model and a non-existent one. The
    thresholds beside it fed a model that is not implemented."""
    groups = config_service.strategy_config_groups(S(_env_file=None))
    assert "Model" not in [g["name"] for g in groups]
    offered = {f["key"] for g in groups for f in g["fields"]}
    assert not [k for k in offered if k.startswith("MODEL_")], sorted(offered)
    for gone in ("MODEL_TYPE", "MODEL_BUY_THRESHOLD", "MODEL_SELL_THRESHOLD",
                 "MODEL_RETRAIN_INTERVAL_DAYS"):
        assert gone not in config_service.STRATEGY_SCOPED_KEYS, "nor writable as a strategy key"
        assert gone in config_service.RETIRED_STRATEGY_KEYS, "...and dropped, not rejected"
    # MODEL_TYPE is gone from the settings model too, and what it reported is now the
    # module constant. The confidence thresholds stay: the generator still gates on them.
    assert "model_type" not in S.model_fields
    # ...as did the retrain cadence, which had no subject: nothing retrains a rule-based
    # generator.
    assert "model_retrain_interval_days" not in S.model_fields
    assert S(_env_file=None).model_buy_threshold == 0.6


def test_rule_based_is_the_model_in_force():
    """Exactly one model kind exists, so it is a constant rather than a setting: the
    module own it, and the backtest and the signal endpoint report it."""
    from src.model import simple_model

    assert simple_model.MODEL_KIND == "rule_based"
    assert "MODEL_KIND" in simple_model.__all__


def test_the_confidence_thresholds_still_gate_signals():
    """They were retired from the strategy layer, not because they are dead: the
    rule-based generator suppresses a signal whose confidence does not clear its
    side's threshold. Removing the section moved them to `.env`, and this pins that
    they are still read from the settings (a future cleanup must not delete them)."""
    from src.model import simple_model

    low = simple_model.resolve_signal([("BUY", 0.4)], buy_threshold=0.6, sell_threshold=0.6)
    assert low.signal == "HOLD" and "below threshold" in low.reason
    high = simple_model.resolve_signal([("BUY", 0.9)], buy_threshold=0.6, sell_threshold=0.6)
    assert high.signal == "BUY"


def test_validate_strategy_config():
    ok, errs = config_service.validate_strategy_config({"GATE_MIN_SHARPE": "0.5"})
    assert ok and not errs
    ok, errs = config_service.validate_strategy_config({"RISK_LIMIT_PERCENT": "x"})  # not a number
    assert not ok and errs
    # A round-tripped value is accepted whatever its spelling of a float.
    ok, errs = config_service.validate_strategy_config({"FEATURE_RSI_ENABLED": "off"})  # bool spelling
    assert ok and not errs
    # A machine-level key is refused here: it is not a strategy's business.
    ok, errs = config_service.validate_strategy_config({"DATA_CACHE_ENABLED": "True"})
    assert not ok and errs
    # ...and so is one whose section was removed.
    ok, errs = config_service.validate_strategy_config({"MODEL_TYPE": "rule_based"})
    assert not ok and errs


def test_market_timezone_is_a_short_list_of_exchanges():
    """The bot can be pointed at one of a handful of exchanges; the zone is what the
    code uses (trading hours, chart timestamps, the "today" a lookback counts back
    from) and it carries the exchange's own DST rules."""
    groups = config_service.strategy_config_groups(S(_env_file=None))
    field = [f for g in groups for f in g["fields"] if f["key"] == "MARKET_TIMEZONE"][0]
    assert [o["value"] for o in field["options"]] == [
        "America/New_York", "Europe/Berlin", "Europe/London",
    ]
    labels = " | ".join(o["label"] for o in field["options"])
    for named in ("Nasdaq", "NYSE", "Eastern Standard Time", "Central European Standard Time",
                  "Greenwich Mean Time", "Frankfurt Stock Exchange", "London Stock Exchange"):
        assert named in labels, named
    # The default is in the list, so the select shows a real choice on a fresh install.
    assert field["value"] in [o["value"] for o in field["options"]]


def test_strategy_scope_carries_trading_gates():
    """Everything a strategy needs is editable per strategy."""
    by_key = {f["key"]: f for g in config_service.strategy_config_groups(S(_env_file=None))
              for f in g["fields"]}
    for key in ("MARKET_TIMEZONE", "TRADING_START_HOUR",
                "TRADING_END_HOUR",
                "GATE_MIN_SHARPE", "GATE_MAX_DRAWDOWN_PERCENT",
                "GATE_MIN_WIN_RATE_PERCENT", "GATE_MAX_WEEKLY_LOSS_PERCENT"):
        assert key in by_key, key
        assert config_service.validate_strategy_config({key: by_key[key]["value"]})[0], key


def test_the_scheduler_section_is_gone():
    """Both scheduler keys asked for something the bot does not have.

    ``SCHEDULER_ENABLED`` was a second "is the bot on" switch — the trading switch already
    answers that, and a second one can disagree with it. ``SCHEDULER_TIMEZONE`` offered a
    clock the loop ignores, since it schedules from the exchange's own /v2/clock.
    """
    groups = config_service.strategy_config_groups(S(_env_file=None))
    assert "Scheduler" not in [g["name"] for g in groups]
    offered = {f["key"] for g in groups for f in g["fields"]}
    assert not [k for k in offered if k.startswith("SCHEDULER_")], sorted(offered)
    for gone in ("SCHEDULER_ENABLED", "SCHEDULER_TIMEZONE"):
        assert gone not in config_service.STRATEGY_SCOPED_KEYS, "nor writable as a strategy key"
        assert gone in config_service.RETIRED_STRATEGY_KEYS, "...and dropped, not rejected"
    # Deleted from Settings too, so a stray .env line cannot make them look live again.
    s = S(_env_file=None)
    assert not hasattr(s, "scheduler_enabled") and not hasattr(s, "scheduler_timezone")


# ---------------------------------------------------------------------------
# account settings (data/account/account.json)
# ---------------------------------------------------------------------------
def test_account_schema_groups_and_derived_folders():
    groups = config_service.account_sections(S(_env_file=None))
    names = [g["name"] for g in groups]
    assert names == ["Trading Account", "Data & Folders", "Backtest"]
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    # All four Alpaca credential fields are secrets, and a secret is never echoed
    # back through the schema — this machine may or may not have keys configured
    # (data/account/account.json is local data), so the assertion is about the
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
    # The historical dates are gone from the model too, not just from the form: a window
    # counted back from now is the only one a fetch uses, so a fixed start/end could only
    # disagree with the period the strategy reports.
    assert not [k for k in S.model_fields if k.startswith("historical_start") or k.startswith("historical_end")]
    assert "historical_lookback" in S.model_fields, "the period is the window"
    # The split fraction is the exception of a different kind: nothing trains, so it is gone.
    assert "train_test_split" not in S.model_fields


def test_cloud_and_state_storage_are_not_account_settings():
    """Both are INFRASTRUCTURE, not properties of a trading account: one dataset copy
    per bucket, one state table per deployment. Neither is offered here at all — they
    come from `.env`."""
    by_key = {f["key"] for g in config_service.account_sections(S(_env_file=None))
              for f in g["fields"]}
    for gone in ("S3_ENABLED", "S3_BUCKET", "S3_PREFIX", "S3_ENDPOINT_URL",
                 "AWS_REGION", "DYNAMODB_TABLE", "DYNAMODB_TTL_DAYS",
                 "DYNAMODB_ENDPOINT_URL"):
        assert gone not in by_key, f"{gone} must not be offered in Account Settings"
        assert gone not in config_service.ACCOUNT_SCOPED_KEYS, "nor writable through the API"
    # Neither the S3 mirror nor the DynamoDB state table exists any more, so the fields
    # are gone from the model rather than offered as dead switches.
    s = S(_env_file=None)
    assert not [k for k in ("s3_enabled", "s3_bucket", "s3_prefix", "s3_endpoint_url",
                            "aws_region", "dynamodb_table", "dynamodb_ttl_days",
                            "dynamodb_endpoint_url") if k in S.model_fields], "removed, not inert"
    assert not [k for k in S.model_fields if k.startswith("s3_") or k.startswith("aws_")]
    # The dataset is a local Parquet file and nothing else — no off-site copy to fall
    # back to, so a missing file is simply "no dataset yet".
    from src.data import dataset as ds

    assert not [n for n in dir(ds) if "s3" in n.lower()], "the mirror code went with the fields"


def test_update_account_persists_json_and_drives_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(config_service.account_mod, "account_file_path",
                        lambda settings: tmp_path / "account.json")

    res = config_service.update_account({
        "DATA_DIR": "srv/data", "CACHE_DIR": "srv/cache",
        "ALPACA_PAPER_API_SECRET": "s3cret",
    })
    assert res["ok"] is True, res
    # Only what the user configures is stored — the two subfolders are derived.
    assert json.loads((tmp_path / "account.json").read_text())["settings"] == {
        "DATA_DIR": "srv/data",
        "CACHE_DIR": "srv/cache",
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
    # ...and so must a dropped IBKR key, a date the form does not offer, or a
    # cloud/state key that moved out of this layer.
    for gone in ("IBKR_ACCOUNT_ID", "HISTORICAL_START_DATE", "TRAIN_TEST_SPLIT",
                 "S3_BUCKET", "DYNAMODB_TABLE"):
        refused = config_service.update_account({gone: "x"})
        assert refused["ok"] is False and refused["errors"], gone


def test_account_api_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config_service.account_mod, "account_file_path",
                        lambda settings: tmp_path / "account.json")
    got = client.get("/api/v1/account")
    assert got.status_code == 200
    assert [g["name"] for g in got.json()["groups"]][0] == "Trading Account"
    posted = client.post("/api/v1/account", json={"values": {"CACHE_DIR": "srv/cache",
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


def test_the_day_loss_limits_say_what_a_daily_bar_size_does_to_them():
    """D5: both limits are measured per exchange day, so a bar size of a day or coarser changes
    what each one can do — and it is not the same thing for the two of them.

    ``MAX_LOSS_PERCENT`` still fires. It is measured on the account's equity, so at a daily bar
    it acts when the bar closes and stops the NEXT session's entry rather than the one that just
    lost. ``MAX_CONSECUTIVE_LOSSES`` cannot be reached at all: the streak is per day, and a day
    holds at most one trade.

    The panel therefore says it per FIELD rather than in one note above both. An operator who
    cannot tell the two apart either trusts a setting that cannot work, or switches off one that
    does — and both mistakes are silent.
    """

    def hints(bar_size):
        groups = config_service.strategy_config_groups(
            S(_env_file=None, historical_bar_size=bar_size)
        )
        return {f["key"]: f["hints"] for g in groups for f in g["fields"]}

    hourly, daily = hints("1h"), hints("1d")

    for key in ("MAX_LOSS_PERCENT", "MAX_CONSECUTIVE_LOSSES"):
        assert daily[key][:-1] == hourly[key], f"{key} gains exactly one line at 1d"
        assert len(daily[key]) == len(hourly[key]) + 1, key

    assert any("NEXT session" in line for line in daily["MAX_LOSS_PERCENT"]), daily["MAX_LOSS_PERCENT"]
    assert any("above 1" in line for line in daily["MAX_CONSECUTIVE_LOSSES"]), daily[
        "MAX_CONSECUTIVE_LOSSES"
    ]
    assert daily["MAX_LOSS_PERCENT"] != daily["MAX_CONSECUTIVE_LOSSES"], "each in its own terms"


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
