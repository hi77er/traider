"""The trading window as a bound on DECISIONS, not just on the live poll.

``TRADING_START_HOUR``/``TRADING_END_HOUR`` used to affect only whether the live poll
was allowed to fetch, and whether today's daily bar was final for the delta. Nothing
in the signal path read them, so a backtest — and any live tick — would happily act on
a pre-market, after-hours or overnight bar. The rules are now applied in the one place
signals are produced, so the backtest, the chart and the live entry point share them:

* an intraday bar is decided on only when its own timestamp is inside the window,
* a calendar bar (1d or coarser) is always eligible, because it *is* a session: its
  decision is taken at that session's close and filled at the next session's open.

Everything is exchange-local, so the check is a comparison of clock times with no
conversion to the machine's timezone.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config import session
from src.config.settings import Settings
from src.data.dataset import bar_in_trading_window, is_intraday
from src.data.live import is_market_open
from src.model import rules as rules_mod
from src.model.simple_model import HOLD, RuleBasedSignalGenerator

NY = "America/New_York"


def _settings(tmp_path, **kw) -> Settings:
    base = dict(
        _env_file=None,
        instrument="AAPL",
        market_timezone=NY,
        trading_start_hour="09:30",
        trading_end_hour="16:00",
        historical_bar_size="1h",
        feature_sma_enabled=False,
        feature_ema_enabled=False,
        feature_macd_enabled=False,
        feature_rsi_enabled=False,
        feature_atr_enabled=False,
        feature_bollinger_enabled=False,
        feature_momentum_enabled=False,
        feature_volatility_enabled=False,
        feature_vwap_enabled=False,
        feature_volume_enabled=False,
        feature_volume_abs_enabled=False,
        features_min_lookback=2,
    )
    base.update(kw)
    return Settings(**base)


def _frame(times, prices=None) -> pd.DataFrame:
    """Candles at the given (naive, exchange-local) timestamps."""
    prices = prices or [100.0 + i for i in range(len(times))]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1000] * len(times),
        },
        index=pd.DatetimeIndex([pd.Timestamp(t) for t in times]),
    )


# A rule that fires on EVERY bar: "close > 0". So any bar that is not HOLD was
# decided on, and the mask is what the test is measuring.
def _always_buy() -> rules_mod.RuleSet:
    return rules_mod.RuleSet(
        name="always",
        rules=[
            rules_mod.Rule(
                side="BUY",
                conditions=[rules_mod.RuleCondition(feature="close", op=">", value=0.0)],
            )
        ],
    )


def _generator(settings, ruleset) -> RuleBasedSignalGenerator:
    return RuleBasedSignalGenerator(settings=settings, rules=list(ruleset.rules))


# ---------------------------------------------------------------------------
# the window itself
# ---------------------------------------------------------------------------
def test_the_window_is_inclusive_of_its_own_edges(tmp_path):
    s = _settings(tmp_path)
    tz = ZoneInfo(NY)
    tuesday = date(2026, 9, 15)
    for hhmm, expected in (("09:29", False), ("09:30", True), ("12:00", True),
                           ("16:00", True), ("16:01", False)):
        h, m = (int(x) for x in hhmm.split(":"))
        when = datetime(tuesday.year, tuesday.month, tuesday.day, h, m, tzinfo=tz)
        assert is_market_open(s, when) is expected, hhmm


def test_the_window_is_closed_at_the_weekend(tmp_path):
    s = _settings(tmp_path)
    tz = ZoneInfo(NY)
    saturday = datetime(2026, 9, 19, 12, 0, tzinfo=tz)
    assert is_market_open(s, saturday) is False
    assert bar_in_trading_window(s, pd.Timestamp("2026-09-19 12:00")) is False


def test_a_window_that_crosses_midnight_is_not_read_as_closed(tmp_path):
    """Unlikely for equities, but a session whose end is before its start would
    otherwise be permanently shut."""
    s = _settings(tmp_path, trading_start_hour="22:00", trading_end_hour="02:00")
    tz = ZoneInfo(NY)
    assert is_market_open(s, datetime(2026, 9, 15, 23, 0, tzinfo=tz)) is True
    assert is_market_open(s, datetime(2026, 9, 15, 1, 0, tzinfo=tz)) is True
    assert is_market_open(s, datetime(2026, 9, 15, 12, 0, tzinfo=tz)) is False


def test_the_live_poll_and_the_decision_filter_share_one_rule(tmp_path):
    """They must agree: fetching a bar the bot then refuses to decide on wastes the
    call, and deciding on one it would not fetch is worse."""
    s = _settings(tmp_path)
    for hhmm in ("09:29", "09:30", "16:00", "16:01"):
        stamp = pd.Timestamp(f"2026-09-15 {hhmm}")
        live = is_market_open(s, stamp.tz_localize(NY).to_pydatetime())
        assert bar_in_trading_window(s, stamp) == live, hhmm


# ---------------------------------------------------------------------------
# per-bar eligibility
# ---------------------------------------------------------------------------
def test_only_bars_inside_the_window_are_eligible(tmp_path):
    s = _settings(tmp_path)
    for stamp, expected in (("2026-09-15 09:30", True), ("2026-09-15 15:30", True),
                            ("2026-09-15 08:30", False), ("2026-09-15 16:30", False),
                            ("2026-09-15 03:00", False)):
        assert bar_in_trading_window(s, pd.Timestamp(stamp)) is expected, stamp


def test_a_narrower_window_makes_fewer_bars_eligible(tmp_path):
    s = _settings(tmp_path, trading_start_hour="10:00", trading_end_hour="15:00")
    assert bar_in_trading_window(s, pd.Timestamp("2026-09-15 09:30")) is False
    assert bar_in_trading_window(s, pd.Timestamp("2026-09-15 10:30")) is True
    assert bar_in_trading_window(s, pd.Timestamp("2026-09-15 15:30")) is False


def test_a_calendar_bar_is_always_eligible(tmp_path):
    """A daily bar IS the session: its decision is taken at that session's close and
    filled at the next session's open, both inside the window. Comparing the bar's
    midnight date against 09:30-16:00 would exclude every daily bar there is."""
    for bar in ("1d", "1W", "1M"):
        s = _settings(tmp_path, historical_bar_size=bar)
        assert is_intraday(bar) is False
        assert bar_in_trading_window(s, pd.Timestamp("2026-09-15")) is True
        # ...and a weekend date does not matter either, for the same reason.
        assert bar_in_trading_window(s, pd.Timestamp("2026-09-19")) is True


# ---------------------------------------------------------------------------
# the mask on the decision series
# ---------------------------------------------------------------------------
DAY = ["2026-09-15 08:30", "2026-09-15 09:30", "2026-09-15 10:30",
       "2026-09-15 15:30", "2026-09-15 16:30"]


def test_signals_are_suppressed_outside_the_window(tmp_path):
    s = _settings(tmp_path)
    gen = _generator(s, _always_buy())
    frame = gen.evaluate_frame(_frame(DAY))
    got = list(frame["signal"])
    assert got == [HOLD, "BUY", "BUY", "BUY", HOLD]


def test_a_suppressed_bar_says_why(tmp_path):
    s = _settings(tmp_path)
    frame = _generator(s, _always_buy()).evaluate_frame(_frame(DAY))
    reason = frame["reason"].iloc[0]
    assert "outside the trading window" in reason
    assert "09:30–16:00" in reason and NY in reason


def test_narrowing_the_window_changes_the_decisions(tmp_path):
    """The proof that the setting reaches the signal series and is not decoration."""
    wide = _generator(_settings(tmp_path), _always_buy()).evaluate_frame(_frame(DAY))
    narrow = _generator(
        _settings(tmp_path, trading_start_hour="10:00", trading_end_hour="15:00"),
        _always_buy(),
    ).evaluate_frame(_frame(DAY))
    assert list(wide["signal"]) == [HOLD, "BUY", "BUY", "BUY", HOLD]
    # 10:00-15:00 keeps only the 10:30 bar: 09:30 is before the start, 15:30 is after
    # the end. The bar that falls between two in-window bars is not "in" by
    # interpolation — each bar is judged on its own timestamp.
    assert list(narrow["signal"]) == [HOLD, HOLD, "BUY", HOLD, HOLD]


def test_a_narrowed_window_suppresses_an_earlier_bar_the_wide_one_allowed(tmp_path):
    s = _settings(tmp_path, trading_start_hour="10:00")
    frame = _generator(s, _always_buy()).evaluate_frame(_frame(DAY))
    assert frame["signal"].iloc[1] == HOLD, "09:30 is before the 10:00 start"
    assert "outside the trading window" in frame["reason"].iloc[1]


def test_the_live_entry_point_is_gated_too(tmp_path):
    """``evaluate_latest`` is what a live tick reads, and it goes through the same
    frame — so it cannot return a decision for an off-hours bar."""
    s = _settings(tmp_path)
    gen = _generator(s, _always_buy())
    assert gen.evaluate_latest(_frame(["2026-09-15 14:30"])).signal == "BUY"
    assert gen.evaluate_latest(_frame(["2026-09-15 18:30"])).signal == HOLD
    assert "outside the trading window" in gen.evaluate_latest(_frame(["2026-09-15 18:30"])).reason


def test_the_warmup_rule_still_wins_inside_the_window(tmp_path):
    """Both filters are "no decision", and a bar can trip either. Inside the window a
    short-of-warmup bar is still HOLD, for the features' reason rather than the
    window's."""
    s = _settings(tmp_path, features_min_lookback=50)
    frame = _generator(s, _always_buy()).evaluate_frame(_frame(DAY))
    inside_warmup = frame["reason"].iloc[2]
    assert "outside the trading window" not in inside_warmup


def test_features_are_still_computed_for_suppressed_bars(tmp_path):
    """Only the DECISION is masked. Dropping the bar instead would break the windows
    of every indicator computed after it, so the frame keeps one row per bar."""
    s = _settings(tmp_path, feature_sma_enabled=True, features_sma_periods="3")
    frame = _generator(s, _always_buy()).evaluate_frame(_frame(DAY))
    assert len(frame) == len(DAY)
    assert list(frame.index) == [pd.Timestamp(t) for t in DAY]


# ---------------------------------------------------------------------------
# the backtest itself
# ---------------------------------------------------------------------------
def test_a_backtest_makes_no_trade_on_out_of_window_bars(tmp_path):
    """End to end through the engine: a window that excludes every bar of the
    dataset must produce no position at all, not a position opened off-hours."""
    import src.backtest.engine as engine
    from src.data.dataset import save_dataset

    day = pd.Timestamp("2026-09-15")
    times = [day + pd.Timedelta(minutes=15 * i) for i in range(0, 40)]
    df = _frame([t.strftime("%Y-%m-%d %H:%M") for t in times],
                prices=[100.0 + (i % 5) for i in range(len(times))])

    # A window that contains none of the bars (all of them are 00:00-09:45).
    s = _settings(tmp_path, historical_data_dir=str(tmp_path / "hist"),
                  trading_start_hour="12:00", trading_end_hour="13:00")
    s.strategy_rules_file = str(tmp_path / "store.json")
    store = rules_mod.StrategyStore(
        active="always", strategies={"always": _always_buy()}
    )
    rules_mod.save_store(s, store)
    save_dataset(s, df, s.instrument, s.historical_bar_size)

    report = engine.run_backtest(s)
    assert report.get("error") is None, report.get("error")
    assert report["trades"] == []
    assert report["signals"].get("BUY", 0) == 0

    # The same data with the window OPEN over those bars does trade, so the zero
    # above is the window and not a broken fixture.
    open_s = _settings(tmp_path, historical_data_dir=str(tmp_path / "hist"),
                       trading_start_hour="00:00", trading_end_hour="09:45")
    open_s.strategy_rules_file = str(tmp_path / "store.json")
    open_report = engine.run_backtest(open_s)
    assert len(open_report["trades"]) >= 1
    assert open_report["signals"].get("BUY", 0) > 0


def test_session_helpers_are_pure(tmp_path):
    s = _settings(tmp_path, trading_start_hour="09:30", trading_end_hour="16:00")
    assert session.describe(s) == f"09:30–16:00 {NY}"
    assert session.window(s) == (session.time(9, 30), session.time(16, 0))
    assert session.last_weekday(date(2026, 9, 19)) == date(2026, 9, 18)
    assert session.last_weekday(date(2026, 9, 15)) == date(2026, 9, 15)
    with pytest.raises(ValueError):
        session.parse_hhmm("25:00")
