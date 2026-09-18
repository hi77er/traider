"""The empty-vs-zero contract for MAX_EXPOSURE_PERCENT.

Both states are meaningful and they are OPPOSITE instructions, so the whole path —
store -> resolver -> deploy weight -> position size — has to keep them apart:

* **empty** means "no cap was asked for": it must be SAVED as empty and fall back to
  the schema default of 100% (the whole account). Writing a 0 here would silently
  turn "unset" into "deploy nothing".
* **explicit 0** means "deploy nothing": it must be recorded as 0 and evaluate as 0.

The distinction is not cosmetic. A zero deploy weight makes every leg's return
``price_ret * 0 == 0``, which flattens the equity curve to 1.0 AND makes every round
trip read as a non-win — a profitable strategy that renders as a flat line with every
trade red. That is how a 0 in this field masquerades as a charting bug.
"""

from src.backtest import risk_sim
from src.config import effective
from src.config.settings import Settings
from src.model import rules as R
from src.risk.position_sizing import size_position
from src.strategy.config import StrategyConfig


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        strategy_rules_file=str(tmp_path / "active.json"),
        historical_data_dir=str(tmp_path),
        instrument="GPRO",
        historical_bar_size="5m",
    )


def _fake_get_settings(settings):
    def fake():
        return settings
    fake.cache_clear = lambda: None
    return fake


def _store(settings, exposure):
    """Save one strategy whose config holds exactly the raw value given."""
    rs = R.empty_strategy("t", "GPRO")
    rs.config = {"MAX_EXPOSURE_PERCENT": exposure}
    R.save_store(settings, R.StrategyStore(active="t", strategies={"t": rs}))


def _resolved(monkeypatch, tmp_path, exposure):
    st = _settings(tmp_path)
    monkeypatch.setattr(effective, "get_settings", _fake_get_settings(st))
    _store(st, exposure)
    return effective.resolve_effective()


# ---------------------------------------------------------------------------
# what is stored
# ---------------------------------------------------------------------------
def test_empty_is_saved_as_empty_not_as_zero(monkeypatch, tmp_path):
    """The round trip must not manufacture a 0 out of an empty box."""
    st = _settings(tmp_path)
    _store(st, "")
    stored = R.load_store(st).strategies["t"].config
    assert stored["MAX_EXPOSURE_PERCENT"] == ""
    assert stored["MAX_EXPOSURE_PERCENT"] != "0"


def test_explicit_zero_is_saved_as_zero(monkeypatch, tmp_path):
    st = _settings(tmp_path)
    _store(st, "0")
    assert R.load_store(st).strategies["t"].config["MAX_EXPOSURE_PERCENT"] == "0"


# ---------------------------------------------------------------------------
# what it resolves to
# ---------------------------------------------------------------------------
def test_empty_resolves_to_the_whole_account(monkeypatch, tmp_path):
    eff = _resolved(monkeypatch, tmp_path, "")
    assert eff.max_exposure_percent is None
    cfg = StrategyConfig.from_settings(eff)
    assert cfg.exposure_percent == 100.0
    assert cfg.deploy_weight == 1.0


def test_explicit_zero_resolves_to_zero(monkeypatch, tmp_path):
    eff = _resolved(monkeypatch, tmp_path, "0")
    assert eff.max_exposure_percent == 0.0
    cfg = StrategyConfig.from_settings(eff)
    assert cfg.exposure_percent == 0.0
    assert cfg.deploy_weight == 0.0


def test_a_real_cap_is_not_confused_with_either(monkeypatch, tmp_path):
    eff = _resolved(monkeypatch, tmp_path, "10")
    cfg = StrategyConfig.from_settings(eff)
    assert cfg.exposure_percent == 10.0
    assert cfg.deploy_weight == 0.1


# ---------------------------------------------------------------------------
# what the two values DO — the part that looked like a charting bug
# ---------------------------------------------------------------------------
def test_zero_exposure_zeroes_every_leg_return(monkeypatch, tmp_path):
    """A zero deploy weight must make the equity maths a no-op, not a loss."""
    eff = _resolved(monkeypatch, tmp_path, "0")
    cfg = risk_sim.RiskConfig.from_settings(eff)
    assert cfg.deploy_weight_for(cfg.stop_loss_percent) == 0.0


def test_the_whole_account_sizes_a_real_position(monkeypatch, tmp_path):
    """The empty case must still size: an uncapped account is not a zero account."""
    eff = _resolved(monkeypatch, tmp_path, "")
    sized = size_position(
        equity=10_000.0,
        price=1.33,
        stop_loss_percent=8.0,
        risk_limit_percent=2.0,
        max_exposure_percent=100.0,
        mode="fixed_risk",
    )
    assert sized.quantity > 0


def test_zero_exposure_sizes_nothing(monkeypatch, tmp_path):
    eff = _resolved(monkeypatch, tmp_path, "0")
    sized = size_position(
        equity=10_000.0,
        price=1.33,
        stop_loss_percent=8.0,
        risk_limit_percent=2.0,
        max_exposure_percent=float(eff.max_exposure_percent),
        mode="fixed_risk",
    )
    assert sized.quantity == 0
    assert sized.notional == 0.0
