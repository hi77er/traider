"""The live store — where the loop's output goes, and what it must survive.

Two things here are load-bearing rather than cosmetic:

* the state file is keyed by **(strategy, env)**, because it used to be keyed by the
  INSTRUMENT alone — so two strategies on the same symbol kept ONE position between them,
  and flipping paper/live reconciled against the wrong account;
* the day key is the **market's** day, so one session is one log file rather than two halves
  either side of midnight UTC.

And one that is about not losing data: a reader must tolerate a torn final line, because the
loop may be appending while the dashboard reads.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.config import artifacts
from src.config.settings import Settings
from src.execution import store
from src.strategy.config import StrategyConfig
from src.strategy.engine import StrategyEngine
from src.strategy.live import LiveDriver


def _s(tmp_path, **kwargs) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        market_timezone="America/New_York",
    )
    values.update(kwargs)
    return Settings(**values)


def _record(settings, strategy="Alpha", env="paper", action="decided", **kw):
    return store.tick_record(strategy=strategy, env=env, action=action, settings=settings, **kw)


def _driver(settings, **kwargs) -> LiveDriver:
    engine = StrategyEngine(StrategyConfig.from_settings(settings, slippage=0.0, commission=0.0))
    return LiveDriver(settings=settings, engine=engine, generator=None, broker=None, **kwargs)


# ---------------------------------------------------------------------------
# where it lives, and what it is keyed by
# ---------------------------------------------------------------------------
def test_the_tree_is_one_per_strategy_under_live_results(tmp_path):
    settings = _s(tmp_path)
    assert store.live_root(settings).name == "live_results"
    assert store.strategy_dir(settings, "Alpha").name == "Alpha"
    assert store.latest_path(settings, "Alpha").name == "latest.json"
    assert store.index_path(settings, "Alpha").name == "index.json"
    assert store.orders_path(settings, "Alpha").name == "orders.jsonl"
    assert store.trades_path(settings, "Alpha").name == "trades.jsonl"
    assert store.ticks_dir(settings, "Alpha").name == "ticks"


def test_one_tree_per_strategy_and_not_per_environment(tmp_path):
    """The environment is a field on each record, so paper and live share a directory.

    Splitting them would break the paper-vs-live comparison that is the point of paper
    trading, move history on promotion, and scatter one strategy's timeline.
    """
    settings = _s(tmp_path)
    assert store.latest_path(settings, "Alpha") == store.latest_path(settings, "Alpha")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Alpha", "Alpha"),
        ("Delta – NVDA - 1h", "Delta-NVDA-1h"),
        ("My Strategy", "My-Strategy"),
        ("Alpha/AAPL", "Alpha-AAPL"),
        ("Ünïcode Strat", "Unicode-Strat"),
    ],
)
def test_the_directory_uses_the_shared_slug_not_a_second_sanitiser(tmp_path, name, expected):
    """Two sanitisers that disagree about a space put one strategy in two directories.

    The state file used to be named by ``live._safe`` while the store names directories by
    ``artifacts.slug``; the two produced ``My_Strategy`` and ``My-Strategy`` for the same
    input, so the state and the results would have landed apart.
    """
    settings = _s(tmp_path)
    assert store.strategy_dir(settings, name).name == expected
    assert artifacts.slug(name) == expected


def test_the_state_file_is_keyed_by_strategy_and_environment(tmp_path):
    settings = _s(tmp_path)
    # Different environment -> different ACCOUNT -> must not share a state file.
    assert store.state_path(settings, "Alpha", "paper").name == "state-paper.json"
    assert store.state_path(settings, "Alpha", "live").name == "state-live.json"
    assert store.state_path(settings, "Alpha", "paper") != store.state_path(settings, "Alpha", "live")
    # Different strategy -> different position, even on the same instrument.
    assert store.state_path(settings, "Alpha", "paper") != store.state_path(settings, "Beta", "paper")


def test_two_strategies_on_one_instrument_get_separate_state(tmp_path):
    """The bug this keying fixes.

    The state file used to be ``strategy_state_<instrument>.json``, so two strategies on
    AAPL shared ONE position: switching between them carried the position across, and
    neither could reconcile.
    """
    settings = _s(tmp_path)
    alpha = _driver(settings, name="Alpha")
    beta = _driver(settings, name="Beta")

    assert alpha.state_path != beta.state_path
    assert alpha.state_path.name == "state-paper.json"
    assert alpha.state_path.parent.name == "Alpha" and beta.state_path.parent.name == "Beta"


def test_the_driver_keeps_paper_and_live_state_apart(tmp_path):
    paper = _driver(_s(tmp_path, execution_env="paper"), name="Alpha")
    live = _driver(_s(tmp_path, execution_env="live"), name="Alpha")
    assert paper.state_path == store.state_path(paper.settings, "Alpha", "paper")
    assert live.state_path == store.state_path(live.settings, "Alpha", "live")
    assert paper.state_path != live.state_path


def test_an_explicit_live_dir_wins_over_the_data_dir(tmp_path):
    settings = _s(tmp_path, live_dir=str(tmp_path / "elsewhere"))
    assert store.live_root(settings) == tmp_path / "elsewhere"


# ---------------------------------------------------------------------------
# the day key
# ---------------------------------------------------------------------------
def test_the_day_is_the_markets_day_not_utc(tmp_path):
    """01:00 UTC is still the previous evening in New York — one session, one file."""
    settings = _s(tmp_path)
    assert store.trading_day(settings, "2026-09-16T01:00:00+00:00") == "2026-09-15"
    assert store.trading_day(settings, "2026-09-16T18:00:00+00:00") == "2026-09-16"
    # A naive stamp is read as UTC rather than refused.
    assert store.trading_day(settings, datetime(2026, 9, 16, 18, 0)) == "2026-09-16"


def test_a_tick_lands_in_its_market_days_file(tmp_path):
    settings = _s(tmp_path)
    at = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)          # 15th in New York
    path = store.append_tick(settings, "Alpha", _record(settings, at=at), when=at)
    assert path.name == "2026-09-15.jsonl"
    assert store.read_ticks(settings, "Alpha", when=at)[0]["day"] == "2026-09-15"


def test_an_unknown_timezone_dates_in_utc_instead_of_failing(tmp_path):
    settings = _s(tmp_path, market_timezone="Mars/Olympus")
    assert store.trading_day(settings, "2026-09-16T01:00:00+00:00") == "2026-09-16"


# ---------------------------------------------------------------------------
# writing and reading
# ---------------------------------------------------------------------------
def test_latest_round_trips_and_is_none_when_never_run(tmp_path):
    settings = _s(tmp_path)
    assert store.load_latest(settings, "Alpha") is None, "never run is not the same as empty"

    store.save_latest(settings, "Alpha", _record(settings, signal="BUY"))

    loaded = store.load_latest(settings, "Alpha")
    assert loaded["signal"] == "BUY" and loaded["action"] == "decided"
    assert store.load_latest(settings, "Beta") is None


def test_latest_is_rewritten_so_a_no_op_tick_still_moves_the_heartbeat(tmp_path):
    """A quiet day must still advance it, or a live loop and a dead loop look identical."""
    settings = _s(tmp_path)
    first = _record(settings, action="decided", at=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc))
    store.save_latest(settings, "Alpha", first)
    second = _record(settings, action="noop", reason="no new bar", at=datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc))
    store.save_latest(settings, "Alpha", second)

    loaded = store.load_latest(settings, "Alpha")
    assert loaded["action"] == "noop" and loaded["reason"] == "no new bar"
    assert loaded["at"] != first["at"], "the heartbeat advanced"
    assert store.latest_path(settings, "Alpha").read_text().count('"at"') == 1, "rewritten, not appended"


def test_the_logs_append_rather_than_rewrite(tmp_path):
    """An array rewritten in place loses its tail on a crash — exactly when it is wanted."""
    settings = _s(tmp_path)
    for i in range(3):
        store.append_tick(settings, "Alpha", _record(settings, signal=f"S{i}"))
    store.append_order(settings, "Alpha", {"client_order_id": "c1", "status": "filled"})
    store.append_order(settings, "Alpha", {"client_order_id": "c2", "status": "rejected"})

    assert [r["signal"] for r in store.read_ticks(settings, "Alpha")] == ["S0", "S1", "S2"]
    assert [r["client_order_id"] for r in store.read_orders(settings, "Alpha")] == ["c1", "c2"]
    assert len(store.read_ticks(settings, "Alpha", limit=2)) == 2, "limit keeps the newest"


def test_a_torn_last_line_does_not_hide_the_rest(tmp_path):
    """The loop may be mid-append while the dashboard reads, so this is normal, not corrupt."""
    settings = _s(tmp_path)
    store.append_tick(settings, "Alpha", _record(settings, signal="BUY"))
    with store.tick_log_path(settings, "Alpha").open("a", encoding="utf-8") as fh:
        fh.write('{"at": "2026-09-16T14:00:00+00:00", "act')      # killed mid-write

    rows = store.read_ticks(settings, "Alpha")
    assert len(rows) == 1 and rows[0]["signal"] == "BUY"


def test_the_index_merges_a_day_and_lists_in_order(tmp_path):
    settings = _s(tmp_path)
    store.upsert_day(settings, "Alpha", "2026-09-16", ticks=2)
    store.upsert_day(settings, "Alpha", "2026-09-16", ticks=3, last="late")   # same day
    store.upsert_day(settings, "Alpha", "2026-09-15", ticks=1)

    index = store.load_index(settings, "Alpha")
    assert [e["day"] for e in index] == ["2026-09-15", "2026-09-16"], "sorted, one entry per day"
    assert index[1]["ticks"] == 3 and index[1]["last"] == "late", "merged, not duplicated"


def test_every_reader_survives_a_missing_tree(tmp_path):
    """A deleted or never-used tree must degrade the explanation, never the screen."""
    settings = _s(tmp_path)
    assert store.load_latest(settings, "Ghost") is None
    assert store.load_index(settings, "Ghost") == []
    assert store.read_ticks(settings, "Ghost") == []
    assert store.read_orders(settings, "Ghost") == []
    assert store.read_trades(settings, "Ghost") == []


def test_a_corrupt_index_is_ignored_rather_than_fatal(tmp_path):
    settings = _s(tmp_path)
    path = store.index_path(settings, "Alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.load_index(settings, "Alpha") == []

    path.write_text('"just a string"', encoding="utf-8")
    assert store.load_index(settings, "Alpha") == []


def test_a_record_of_numpy_and_timestamp_values_still_writes(tmp_path):
    """These are the values a trading record is actually made of."""
    settings = _s(tmp_path)
    record = _record(
        settings,
        signal=np.str_("BUY"),
        intents=[{"price": np.float64(101.25), "stop": np.int64(99)}],
        adopted={"at": pd.Timestamp("2026-09-16T13:59:00Z"), "price": np.float64(97.5)},
    )
    store.save_latest(settings, "Alpha", record)
    store.append_tick(settings, "Alpha", record)

    loaded = store.load_latest(settings, "Alpha")
    assert loaded["intents"][0]["price"] == 101.25
    assert loaded["adopted"]["at"].startswith("2026-09-16"), loaded["adopted"]["at"]


def test_a_tick_record_carries_the_core_keys_and_the_day(tmp_path):
    settings = _s(tmp_path)
    record = _record(settings, bar="2026-09-16T13:00:00-04:00", signal="BUY", order_ids=["o1"])
    for key in (
        "at", "day", "strategy", "env", "action", "reason", "bar", "signal",
        "intents", "adopted", "position", "order_ids",
    ):
        assert key in record, key
    assert record["day"] == "2026-09-16" and record["env"] == "paper"
    assert record["adopted"] is None and record["intents"] == []


# ---------------------------------------------------------------------------
# the pre-(strategy, env) state file
# ---------------------------------------------------------------------------
def test_a_legacy_state_file_is_moved_aside_and_never_adopted(tmp_path):
    """It is keyed by instrument alone, so it cannot be attributed — and guessing is worse
    than starting flat, because the broker is asked what is actually held."""
    settings = _s(tmp_path)
    legacy = tmp_path / "data" / "strategy_state_AMZN.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"position": {"entry_price": 100.0}}', encoding="utf-8")

    assert [p.name for p in store.legacy_state_files(settings)] == ["strategy_state_AMZN.json"]
    moved = store.retire_legacy_state(settings)

    assert [p.name for p in moved] == ["strategy_state_AMZN.json"]
    assert not legacy.exists(), "moved out of the way so nothing mistakes it for current"
    assert moved[0].read_text() == '{"position": {"entry_price": 100.0}}', "preserved, not deleted"
    assert moved[0].parent.name == "_legacy"


def test_a_legacy_state_file_is_not_read_by_the_driver(tmp_path):
    """The driver starts flat. Reconciliation against the broker is what decides the truth."""
    settings = _s(tmp_path)
    legacy = tmp_path / "data" / "strategy_state_AMZN.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"position": {"entry_price": 100.0, "raw_entry_price": 100.0, "short": false, "stop": 98.0, "take": 104.0, "weight": 1.0, "entry_index": 0}}', encoding="utf-8")

    driver = _driver(settings, name="Alpha")
    driver.load_state()

    assert driver.state.position is None, "a legacy file is never adopted"
    assert legacy.exists(), "and it is left alone for a human to look at"
    assert driver.state_path != legacy


def test_retiring_legacy_state_does_nothing_when_there_is_none(tmp_path):
    settings = _s(tmp_path)
    assert store.retire_legacy_state(settings) == []
    assert not (store.live_root(settings) / "_legacy").exists()
