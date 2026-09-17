"""Tests for the Rules service + API endpoints.

Service tests exercise a temp rules file; endpoint tests monkeypatch the
service so nothing touches disk / real state.
"""

import pandas as pd
from fastapi.testclient import TestClient

from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.model import rules as rules_mod
from src.web.app import app
from src.web.services import rules_service

client = TestClient(app)


def _settings(tmp_path) -> Settings:
    return Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
    )


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
def test_payload_includes_builder_metadata(tmp_path):
    st = _settings(tmp_path)
    p = rules_service.payload(st)
    assert "file" in p and "file_exists" in p
    assert p["ops"] == list(rules_mod.OPS)
    feats = p["allowed_features"]
    assert "close" in feats and "volume" in feats  # raw series available
    assert "sma_50" in feats and "rsi_14" in feats  # active features available
    assert any(f.startswith("ema_") for f in feats)  # EMA is offered to rules too
    # no strategies yet -> empty store so the panel shows "create first"
    assert p["strategies"] == {}
    assert p["active"] is None


def test_create_strategy_adds_and_activates(tmp_path):
    st = _settings(tmp_path)
    res = rules_service.create_strategy(st, "  mean-reversion  ")
    assert res["ok"] is True
    payload = rules_service.payload(st)
    assert "mean-reversion" in payload["strategies"]
    assert payload["active"] == "mean-reversion"
    assert payload["strategies"]["mean-reversion"]["rules"] == []  # empty new strategy


def test_create_strategy_switches_to_existing(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "beta")
    res = rules_service.create_strategy(st, "alpha")  # exists -> just select
    assert res["ok"] is True
    assert rules_service.payload(st)["active"] == "alpha"
    assert set(rules_service.payload(st)["strategies"]) == {"alpha", "beta"}


def test_create_strategy_requires_name(tmp_path):
    st = _settings(tmp_path)
    res = rules_service.create_strategy(st, "   ")
    assert res["ok"] is False


def test_update_saves_to_named_strategy(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.rules = [rules_mod.Rule(side="BUY", conditions=[
        rules_mod.RuleCondition(feature="close", op="<", ref="sma_50")])]
    res = rules_service.update_strategy(st, "alpha", rs.model_dump())
    assert res["ok"] is True
    saved = rules_service.payload(st)["strategies"]["alpha"]
    assert saved["rules"][0]["side"] == "BUY"
    assert saved["name"] == "alpha"  # name forced to the store key


def test_update_keeps_other_strategies_intact(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "beta")
    beta_rs = rules_mod.empty_strategy("beta", "AAPL")
    beta_rs.rules = [rules_mod.Rule(side="SELL", conditions=[
        rules_mod.RuleCondition(feature="rsi_14", op=">", value=70.0)])]
    rules_service.update_strategy(st, "beta", beta_rs.model_dump())
    p = rules_service.payload(st)
    assert p["strategies"]["beta"]["rules"][0]["side"] == "SELL"
    assert p["strategies"]["alpha"]["rules"] == []  # untouched


def test_update_rejects_invalid_rules(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    bad = rules_mod.empty_strategy("alpha", "AAPL").model_dump()
    bad["rules"] = [{"side": "BUY", "conditions": []}]  # rule needs >=1 condition
    res = rules_service.update_strategy(st, "alpha", bad)
    assert res["ok"] is False
    assert res["errors"]


def test_reset_only_resets_active_strategy_rules(tmp_path):
    st = _settings(tmp_path)
    # two strategies, both with a custom rule + config
    a = rules_mod.empty_strategy("alpha", "MSFT")
    a.rules = [rules_mod.Rule(side="BUY", conditions=[
        rules_mod.RuleCondition(feature="close", op="<", ref="sma_50")])]
    a.config = {"INSTRUMENT": "MSFT", "GATE_MIN_SHARPE": "0.3"}
    rules_service.update_strategy(st, "alpha", a.model_dump())
    b = rules_mod.empty_strategy("beta", "AAPL")
    b.rules = [rules_mod.Rule(side="SELL", conditions=[
        rules_mod.RuleCondition(feature="rsi_14", op=">", value=70.0)])]
    rules_service.update_strategy(st, "beta", b.model_dump())  # active = beta
    # clear beta's rules (simulate "deleting the rules")
    b2 = rules_mod.empty_strategy("beta", "AAPL")
    b2.config = dict(b.config)
    rules_service.update_strategy(st, "beta", b2.model_dump())

    res = rules_service.reset(st)  # reset ACTIVE (beta) rules to the example
    assert res["ok"] is True
    p = rules_service.payload(st)
    assert set(p["strategies"]) == {"alpha", "beta"}  # both strategies remain!
    assert p["active"] == "beta"
    assert p["strategies"]["beta"]["rules"]  # example rules restored on beta
    # alpha is untouched (custom rule + MSFT instrument + threshold kept)
    assert len(p["strategies"]["alpha"]["rules"]) == 1
    assert p["strategies"]["alpha"]["config"]["INSTRUMENT"] == "MSFT"
    assert p["strategies"]["alpha"]["config"]["GATE_MIN_SHARPE"] == "0.3"


def test_reset_requires_active_strategy(tmp_path):
    st = _settings(tmp_path)  # no strategies yet
    res = rules_service.reset(st)
    assert res["ok"] is False


def test_delete_strategy_soft_deletes_and_switches_active(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "beta")  # active = beta
    res = rules_service.delete_strategy(st, "beta")
    assert res["ok"] is True
    p = rules_service.payload(st)
    assert p["active"] == "alpha"  # switched away from the deleted one
    assert set(p["strategies"]) == {"alpha"}  # beta hidden from the panel
    # soft: entry is still on disk, flagged deleted
    on_disk = rules_mod.load_store(st)
    assert on_disk.strategies["beta"].deleted is True
    assert on_disk.strategies["alpha"].deleted is False


def test_delete_last_strategy_leaves_empty_panel(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "only")
    res = rules_service.delete_strategy(st, "only")
    assert res["ok"] is True
    p = rules_service.payload(st)
    assert p["active"] is None
    assert p["strategies"] == {}  # create-first form reappears
    assert rules_mod.load_store(st).strategies["only"].deleted is True  # kept in file


def test_delete_missing_strategy_fails(tmp_path):
    st = _settings(tmp_path)
    res = rules_service.delete_strategy(st, "nope")
    assert res["ok"] is False
    assert "nope" not in rules_service.payload(st)["strategies"]


def test_delete_data_removes_dataset_files(tmp_path):
    """delete_data=True removes every dataset file for the strategy instrument
    when no other live strategy uses it."""
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    st = Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
        historical_data_dir=str(hist),
    )
    df = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02", tz="UTC")]),
    )
    save_dataset(st, df, "AAPL", "1d")
    save_dataset(st, df, "AAPL", "1h")
    rules_service.create_strategy(st, "alpha")  # instrument AAPL from settings

    res = rules_service.delete_strategy(st, "alpha", delete_data=True)
    assert res["ok"] is True
    assert sorted(res["removed"]) == ["AAPL_1d", "AAPL_1h"]
    assert res["skipped"] is None
    assert not (hist / "AAPL_1d.parquet").exists()
    assert not (hist / "AAPL_1h.parquet").exists()


def test_delete_without_data_keeps_dataset(tmp_path):
    """Default delete leaves the dataset file untouched."""
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    st = Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
        historical_data_dir=str(hist),
    )
    df = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02", tz="UTC")]),
    )
    save_dataset(st, df, "AAPL", "1d")
    rules_service.create_strategy(st, "alpha")

    res = rules_service.delete_strategy(st, "alpha")  # delete_data defaults False
    assert res["ok"] is True
    assert res["removed"] == []
    assert res["skipped"] is None
    assert (hist / "AAPL_1d.parquet").exists()


def test_delete_data_keeps_shared_dataset(tmp_path):
    """A dataset still used by another live strategy is NOT deleted."""
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    st = Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="AAPL",
        historical_data_dir=str(hist),
    )
    df = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02", tz="UTC")]),
    )
    save_dataset(st, df, "AAPL", "1d")
    rules_service.create_strategy(st, "alpha")  # AAPL
    rules_service.create_strategy(st, "beta")   # also AAPL (same global instrument)

    res = rules_service.delete_strategy(st, "alpha", delete_data=True)
    assert res["ok"] is True
    assert res["removed"] == []
    assert res["skipped"] and "beta" in res["skipped"]
    assert (hist / "AAPL_1d.parquet").exists()


def test_delete_then_recreate_resurrects(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.delete_strategy(st, "alpha")
    # creating the same name again brings it back (deleted=False)
    rules_service.create_strategy(st, "alpha")
    p = rules_service.payload(st)
    assert "alpha" in p["strategies"]
    assert p["active"] == "alpha"


def _one_bar_df() -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02", tz="UTC")]),
    )


def _set_strategy_config(st, name, config: dict) -> None:
    rs = rules_mod.empty_strategy(name, config.get("INSTRUMENT", ""))
    rs.config = config
    assert rules_service.update_strategy(st, name, rs.model_dump())["ok"] is True


def test_delete_data_keeps_the_bar_size_a_sibling_still_uses(tmp_path):
    """A dataset file is keyed by instrument AND bar size, so the reference check
    must be per pair: deleting the daily NVDA strategy removes NVDA_1d.parquet and
    leaves the minute file a sibling is running on untouched."""
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    st = Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="NVDA",
        historical_data_dir=str(hist),
    )
    save_dataset(st, _one_bar_df(), "NVDA", "1d")
    save_dataset(st, _one_bar_df(), "NVDA", "1m")
    rules_service.create_strategy(st, "alpha")  # NVDA, global bar size (1d)
    rules_service.create_strategy(st, "beta")
    _set_strategy_config(st, "beta", {"INSTRUMENT": "NVDA", "HISTORICAL_BAR_SIZE": "1m"})

    res = rules_service.delete_strategy(st, "alpha", delete_data=True)

    assert res["ok"] is True
    assert res["removed"] == ["NVDA_1d"]
    assert not (hist / "NVDA_1d.parquet").exists()
    assert (hist / "NVDA_1m.parquet").exists(), "a sibling's dataset file was deleted"


def test_delete_data_reports_the_pair_it_kept(tmp_path):
    """When every file under the instrument is in use, nothing is deleted and the
    message names the strategy AND the bar size that is holding it."""
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    st = Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="NVDA",
        historical_data_dir=str(hist),
    )
    save_dataset(st, _one_bar_df(), "NVDA", "1m")
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "beta")
    for who in ("alpha", "beta"):
        _set_strategy_config(st, who, {"INSTRUMENT": "NVDA", "HISTORICAL_BAR_SIZE": "1m"})

    res = rules_service.delete_strategy(st, "alpha", delete_data=True)

    assert res["removed"] == []
    assert res["skipped"] and "beta" in res["skipped"] and "1m" in res["skipped"]
    assert (hist / "NVDA_1m.parquet").exists()


def test_delete_data_leftover_bar_sizes_go_when_nothing_uses_them(tmp_path):
    """Files a strategy left behind by changing bar size are still cleaned up on
    delete — deleting is the opt-in the operator just made, and no live strategy
    references those pairs any more."""
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    st = Settings(
        strategy_rules_file=str(tmp_path / "active.json"),
        instrument="NVDA",
        historical_data_dir=str(hist),
    )
    save_dataset(st, _one_bar_df(), "NVDA", "1d")  # left over from an earlier bar size
    save_dataset(st, _one_bar_df(), "NVDA", "1m")
    rules_service.create_strategy(st, "alpha")
    _set_strategy_config(st, "alpha", {"INSTRUMENT": "NVDA", "HISTORICAL_BAR_SIZE": "1m"})

    res = rules_service.delete_strategy(st, "alpha", delete_data=True)

    assert sorted(res["removed"]) == ["NVDA_1d", "NVDA_1m"]
    assert not (hist / "NVDA_1d.parquet").exists()
    assert not (hist / "NVDA_1m.parquet").exists()


# ---------------------------------------------------------------------------
# service: rename
# ---------------------------------------------------------------------------
def test_rename_strategy_renames_and_moves_active_pointer(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "beta")  # active = beta
    res = rules_service.rename_strategy(st, "beta", "gamma")
    assert res["ok"] is True
    p = rules_service.payload(st)
    assert p["active"] == "gamma"  # active pointer followed the rename
    assert set(p["strategies"]) == {"alpha", "gamma"}
    assert p["strategies"]["gamma"]["name"] == "gamma"
    on_disk = rules_mod.load_store(st)
    assert "beta" not in on_disk.strategies and "gamma" in on_disk.strategies


def test_rename_strategy_preserves_rules_and_config(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.rules = [rules_mod.Rule(side="BUY", conditions=[
        rules_mod.RuleCondition(feature="close", op="<", ref="sma_50")])]
    rs.config = {"INSTRUMENT": "AAPL", "GATE_MIN_SHARPE": "0.7"}
    rules_service.update_strategy(st, "alpha", rs.model_dump())
    res = rules_service.rename_strategy(st, "alpha", "renamed")
    assert res["ok"] is True
    saved = rules_service.payload(st)["strategies"]["renamed"]
    assert saved["rules"][0]["side"] == "BUY"
    assert saved["config"]["GATE_MIN_SHARPE"] == "0.7"


def test_rename_strategy_requires_both_names(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    assert rules_service.rename_strategy(st, "alpha", "   ")["ok"] is False
    assert rules_service.rename_strategy(st, "", "beta")["ok"] is False


def test_rename_strategy_rejects_same_or_live_taken_name(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "beta")
    assert rules_service.rename_strategy(st, "alpha", "alpha")["ok"] is False  # same
    assert rules_service.rename_strategy(st, "alpha", "beta")["ok"] is False   # live taken


def test_rename_strategy_can_claim_deleted_name(tmp_path):
    """A soft-deleted name is invisible in the panel, so renaming another
    strategy to it is allowed — the old deleted entry is permanently dropped."""
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rules_service.create_strategy(st, "gamma")
    rules_service.delete_strategy(st, "gamma")  # soft-deleted, hidden
    res = rules_service.rename_strategy(st, "alpha", "gamma")
    assert res["ok"] is True
    p = rules_service.payload(st)
    assert set(p["strategies"]) == {"gamma"}
    assert p["strategies"]["gamma"]["deleted"] is False
    assert p["active"] == "gamma"  # active pointer followed the rename
    on_disk = rules_mod.load_store(st)
    assert "gamma" in on_disk.strategies and on_disk.strategies["gamma"].deleted is False
    assert not any(rs.deleted for rs in on_disk.strategies.values())


def test_rename_strategy_rejects_missing_or_deleted(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    assert rules_service.rename_strategy(st, "nope", "x")["ok"] is False
    rules_service.delete_strategy(st, "alpha")
    assert rules_service.rename_strategy(st, "alpha", "x")["ok"] is False  # soft-deleted


def test_payload_includes_strategy_config_schema(tmp_path):
    st = _settings(tmp_path)
    p = rules_service.payload(st)
    groups = p["config_groups"]
    assert groups  # per-strategy schema present
    keys = {f["key"] for g in groups for f in g["fields"]}
    assert "FEATURE_RSI_ENABLED" in keys
    assert "POSITION_SIZING_MODE" not in keys  # moved to the Risk Management panel

    # Risk has its own panel: its own group list, and no key in both.
    risk_groups = p["risk_groups"]
    assert [g["name"] for g in risk_groups] == ["Risk Management"]
    risk_keys = {f["key"] for g in risk_groups for f in g["fields"]}
    assert {
        "ALLOW_SHORT", "RISK_LIMIT_PERCENT", "POSITION_SIZING_MODE", "STOP_LOSS_PERCENT",
        "TAKE_PROFIT_PERCENT", "MAX_EXPOSURE_PERCENT", "MAX_LOSS_PERCENT",
        "MAX_CONSECUTIVE_LOSSES",
    } <= risk_keys
    # The two master switches are GONE, not merely hidden: what is configured is what
    # is applied, so "apply the risk layer" and "circuit breaker enabled" asked a
    # question the settings above already answer.
    assert not ({"APPLY_RISK_LAYER", "CIRCUIT_BREAKER_ENABLED"} & risk_keys)
    assert not (keys & risk_keys)  # every key renders in exactly one panel
    # Exposure leads the panel: it is the only risk setting with a default, so it is
    # what describes a run before anything else is filled in.
    assert risk_groups[0]["fields"][0]["key"] == "MAX_EXPOSURE_PERCENT"
    # And the optional ones say so, with an example of the number they want.
    lines = {f["key"]: " ".join(f.get("hints") or []) for f in risk_groups[0]["fields"]}
    for key in ("RISK_LIMIT_PERCENT", "STOP_LOSS_PERCENT", "TAKE_PROFIT_PERCENT",
                "MAX_EXPOSURE_PERCENT", "MAX_LOSS_PERCENT", "MAX_CONSECUTIVE_LOSSES"):
        assert lines.get(key), f"{key} should carry an example/explanation line"
    # Empty is an instruction, so every optional field has to say what leaving it
    # empty does — otherwise "blank" reads as "not filled in yet".
    for key in ("RISK_LIMIT_PERCENT", "STOP_LOSS_PERCENT", "TAKE_PROFIT_PERCENT",
                "MAX_LOSS_PERCENT", "MAX_CONSECUTIVE_LOSSES"):
        assert "e.g." in lines[key] and "empty" in lines[key], lines[key]
    # The Model section is gone: the only model is the rule-based one, so MODEL_TYPE
    # and the thresholds that fed the unimplemented one are not offered at all.
    assert not [k for k in keys if k.startswith("MODEL_")], sorted(keys)


def test_create_strategy_prefills_config_with_globals(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    cfg = rules_service.payload(st)["strategies"]["alpha"]["config"]
    assert cfg  # seeded from current global values
    assert "FEATURE_RSI_ENABLED" in cfg and "POSITION_SIZING_MODE" in cfg


def test_update_strategy_saves_config(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.config = {"GATE_MIN_SHARPE": "0.7", "FEATURE_RSI_ENABLED": "off"}
    res = rules_service.update_strategy(st, "alpha", rs.model_dump())
    assert res["ok"] is True
    saved = rules_service.payload(st)["strategies"]["alpha"]["config"]
    assert saved["GATE_MIN_SHARPE"] == "0.7"
    assert saved["FEATURE_RSI_ENABLED"] == "off"


def test_update_strategy_drops_retired_model_keys(tmp_path):
    """A strategy saved while the Model section existed still carries its keys, and
    the panel posts the strategy back verbatim — so the save must DROP them rather
    than reject a config the operator can no longer see or fix. MODEL_TYPE was set to
    the only value it could usefully take, and the two thresholds equal the `.env`
    value, so dropping them changes nothing in effect."""
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.config = {
        "MODEL_TYPE": "rule_based", "MODEL_BUY_THRESHOLD": "0.6",
        "MODEL_SELL_THRESHOLD": "0.6", "MODEL_RETRAIN_INTERVAL_DAYS": "30",
        "INSTRUMENT": "AAPL",
    }
    res = rules_service.update_strategy(st, "alpha", rs.model_dump())
    assert res["ok"] is True, res
    saved = rules_service.payload(st)["strategies"]["alpha"]["config"]
    assert [k for k in saved if k.startswith("MODEL_")] == []
    assert saved["INSTRUMENT"] == "AAPL", "and nothing else was lost"


def test_update_strategy_rejects_invalid_config(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.config = {"GATE_MIN_SHARPE": "99.0"}  # out of 0..10
    res = rules_service.update_strategy(st, "alpha", rs.model_dump())
    assert res["ok"] is False
    assert res["errors"]


def test_update_strategy_rejects_non_scoped_config_key(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.config = {"WEB_PORTAL_PORT": "8080"}  # global setting, not per-strategy
    res = rules_service.update_strategy(st, "alpha", rs.model_dump())
    assert res["ok"] is False
    assert res["errors"]


def test_update_strategy_can_set_instrument_per_strategy(tmp_path):
    st = _settings(tmp_path)
    rules_service.create_strategy(st, "alpha")
    rs = rules_mod.empty_strategy("alpha", "AAPL")
    rs.config = {"INSTRUMENT": "MSFT", "HISTORICAL_BAR_SIZE": "1d"}
    res = rules_service.update_strategy(st, "alpha", rs.model_dump())
    assert res["ok"] is True
    saved = rules_service.payload(st)["strategies"]["alpha"]
    assert saved["config"]["INSTRUMENT"] == "MSFT"
    assert saved["instrument"] == "MSFT"  # strategy object stays in sync


# ---------------------------------------------------------------------------
# endpoints (service mocked)
# ---------------------------------------------------------------------------
def test_endpoint_get_rules(monkeypatch):
    monkeypatch.setattr(
        rules_service,
        "payload",
        lambda settings, default=False: {
            "file": "/x/active.json",
            "file_exists": False,
            "allowed_features": ["close"],
            "ops": ["<"],
            "active": None,
            "strategies": {},
            "error": None,
        },
    )
    r = client.get("/api/v1/rules")
    assert r.status_code == 200
    assert r.json()["strategies"] == {}


def test_endpoint_delete_strategy(monkeypatch):
    calls = {}

    def fake_delete(settings, name, delete_data=False):
        calls["name"] = name
        calls["delete_data"] = delete_data
        return {
            "ok": True, "message": "deleted", "errors": [], "file": "/x/active.json",
            "removed": [], "skipped": None,
            "payload": {"active": None, "strategies": {}},
        }

    monkeypatch.setattr(rules_service, "delete_strategy", fake_delete)
    r = client.post("/api/v1/rules/delete", json={"name": "alpha", "delete_data": True})
    assert r.status_code == 200
    assert r.json()["payload"]["active"] is None
    assert calls == {"name": "alpha", "delete_data": True}


def test_endpoint_rename_strategy(monkeypatch):
    monkeypatch.setattr(
        rules_service,
        "rename_strategy",
        lambda settings, name, new_name: {
            "ok": True, "message": "renamed", "errors": [], "file": "/x/active.json",
            "payload": {"active": new_name, "strategies": {new_name: {"name": new_name, "rules": []}}},
        },
    )
    r = client.post("/api/v1/rules/rename", json={"name": "alpha", "new_name": "beta"})
    assert r.status_code == 200
    assert r.json()["payload"]["active"] == "beta"


def test_endpoint_create_strategy(monkeypatch):
    monkeypatch.setattr(
        rules_service,
        "create_strategy",
        lambda settings, name: {"ok": True, "message": "created", "errors": [], "file": "/x/active.json",
                                "payload": {"active": name, "strategies": {name: {"rules": []}}}},
    )
    r = client.post("/api/v1/rules/create", json={"name": "alpha"})
    assert r.status_code == 200
    assert r.json()["payload"]["active"] == "alpha"


def test_endpoint_post_rules(monkeypatch):
    monkeypatch.setattr(
        rules_service,
        "update_strategy",
        lambda settings, name, ruleset: {
            "ok": True, "message": "Saved", "errors": [], "file": "/x/active.json",
            "payload": {"active": name, "strategies": {name: ruleset}},
        },
    )
    r = client.post("/api/v1/rules", json={"name": "alpha", "ruleset": {"name": "alpha", "rules": []}})
    assert r.status_code == 200
    assert r.json()["payload"]["active"] == "alpha"
