"""The instrument automation: the criteria, the list, and the one tick decision they drive.

Four things are tested here, in the order the feature works:

* the CRITERIA — stored beside the data rather than in the strategy config (they have to be
  editable while a loop is trading), coerced from whatever a form sends, and never able to stop
  the loop by being unreadable;
* the LIST — a screen ranked for performance AND for volume, cached with the criteria that made
  it, and refused when it was screened against different criteria or has aged out;
* the DECISION — the three switch rules, and the two guards (what is held, one a day) that can
  hold a switch back;
* the TICK — a switch ends the tick at its own gate, is logged with the loop's own words, and a
  switch that could NOT be made leaves the loop trading the instrument it has.

No test reaches a provider, an exchange or a broker: the screener, the positions read and the
backfill are all injected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.config import automation as automation_mod
from src.config import session as session_mod
from src.config import state_files
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.data import instrument_automation as list_mod
from src.model import rules as rules_mod
from src.scheduler import instrument as instrument_mod
from src.scheduler import orchestrator

NOW = datetime(2024, 1, 5, 16, 30, tzinfo=timezone.utc)


def _settings(tmp_path, **kw) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        strategy_rules_file=str(tmp_path / "store.json"),
        instrument="GPRO",
        execution_env="paper",
        historical_bar_size="1h",
        market_timezone="America/New_York",
        trading_start_hour="09:30",
        trading_end_hour="16:00",
    )
    values.update(kw)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return _settings(tmp_path)


def _frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _list_rows(spec) -> list:
    """Three candidates: the best performer, the biggest dollar volume, and neither."""
    return [
        {"symbol": "AAA", "name": "Best", "price": 10.0, "change_percent": 9.0, "volume": 4_000_000,
         "market_cap": 900_000_000, "sector": "Tech"},
        {"symbol": "BBB", "name": "Busiest", "price": 50.0, "change_percent": 2.0,
         "volume": 9_000_000, "market_cap": 1_500_000_000, "sector": "Energy"},
        {"symbol": "CCC", "name": "Neither", "price": 4.0, "change_percent": -3.0,
         "volume": 100_000, "market_cap": 400_000_000, "sector": "Health"},
    ]


def _cached(settings, rows=None, when: str = NOW.isoformat(), criteria=None,
            session: str = "same") -> dict:
    """A cached list, stamped with the session ``NOW`` falls in unless told otherwise."""
    document = {
        "at": when,
        "session": session_mod.stamp(settings, NOW) if session == "same" else session,
        "strategy": "Alpha",
        "criteria": dict(criteria if criteria is not None else automation_mod.read(settings, "Alpha").enter),
        "text": "the criteria",
        "rows": rows if rows is not None else list_mod.rank_rows(_frame(_list_rows(None)), 3),
    }
    automation_mod.write_list(settings, "Alpha", document)
    return document


def _arm(settings, on: bool = True, **switch) -> automation_mod.Automation:
    automation = automation_mod.from_payload({
        "on": on, "enter": {"size": 3}, "switch": {"mode": "not_in_list", **switch},
    })
    automation_mod.write(settings, "Alpha", automation)
    return automation


def _stub_screen(enter) -> list:
    """The provider, faked: the three candidates, ranked. Never reaches Yahoo."""
    return list_mod.rank_rows(_frame(_list_rows(None)), int(enter.get("size") or 3))


# ---------------------------------------------------------------------------
# the criteria
# ---------------------------------------------------------------------------
def test_the_defaults_are_the_small_cap_screen_and_automation_off():
    """Off until somebody turns it on, and pointed at small caps: a feature that changes what the
    bot trades must do nothing at all until it is asked to."""
    automation = automation_mod.from_payload(None)

    assert automation.on is False
    assert automation.enter["market_cap_min"] == 300_000_000
    assert automation.enter["market_cap_max"] == 2_000_000_000
    assert automation.enter["size"] == 10
    assert automation.mode == "not_in_list"
    assert automation.only_when_flat is True
    assert automation.max_age_minutes == 60


def test_the_criteria_survive_a_form_that_sends_everything_as_text():
    """A browser posts strings, and ``false`` is a truthy string: the coercion is by FIELD kind, so
    a checkbox that was switched off cannot be stored as ON."""
    automation = automation_mod.from_payload({
        "on": "true",
        "enter": {"size": "5", "min_price": "2.5", "market_cap_min": "300000000"},
        "switch": {"mode": "margin", "only_when_flat": "false", "margin": "7"},
    })

    assert automation.on is True and automation.size == 5
    assert automation.enter["min_price"] == 2.5
    assert automation.switch["only_when_flat"] is False
    assert automation.mode == "margin" and automation.margin == 7


def test_junk_in_a_criterion_falls_back_rather_than_raising():
    automation = automation_mod.from_payload({"on": "maybe", "enter": {"size": "ten"},
                                              "switch": {"mode": "nonsense"}})

    assert automation.on is False, "an unreadable switch is OFF, not ON"
    assert automation.size == 10, "an unreadable number keeps the default"
    assert automation.mode == "not_in_list", "an unknown mode is not a mode"


def test_the_criteria_round_trip_through_their_own_file(settings):
    """Their own file, beside the data: the strategy config is frozen while trading is on, and
    turning this OFF is how you stop it."""
    written = _arm(settings, on=True, once_per_day=False)
    path = automation_mod.locate(settings, "Alpha")

    assert path.exists() and path.name.startswith("automation-")
    stored = automation_mod.read(settings, "Alpha")
    assert stored.on is True and stored.once_per_day is False
    assert stored.enter == written.enter


def test_a_list_is_stale_when_it_answers_a_different_question(settings):
    """A criteria change invalidates the list outright rather than waiting out its age: rows
    screened for another universe are not an older answer, they are an answer to something else."""
    automation = _arm(settings, on=True)
    _cached(settings)
    session = session_mod.stamp(settings, NOW)

    assert automation_mod.list_is_stale(automation_mod.read_list(settings, "Alpha"), automation,
                                        moment=NOW, session=session) is None

    changed = automation_mod.from_payload({"on": True, "enter": {"size": 3, "min_price": 9.0}})
    reason = automation_mod.list_is_stale(automation_mod.read_list(settings, "Alpha"), changed,
                                          moment=NOW, session=session)
    assert "criteria have changed" in reason


def test_a_list_ages_out(settings):
    automation = _arm(settings, on=True)
    old = (NOW - timedelta(minutes=61)).isoformat()
    _cached(settings, when=old)

    reason = automation_mod.list_is_stale(automation_mod.read_list(settings, "Alpha"), automation,
                                          moment=NOW, session=session_mod.stamp(settings, NOW))
    assert "61 minutes old" in reason


def test_no_list_at_all_is_stale(settings):
    automation = _arm(settings, on=True)

    assert "no list" in automation_mod.list_is_stale(None, automation, moment=NOW, session=None)


def test_a_list_from_another_session_cannot_be_judged(settings):
    """THE session rule, and the screener's own numbers are what force it: they are the last
    COMPLETED regular session's, so a list screened before the bell is not this session's list.

    Age cannot catch this. A list screened at 09:00 and judged at 09:31 is half an hour old and
    still yesterday's ranking — measured against the provider, a pre-market screen returns the
    previous close's change %, volume and stamp, with the pre-market move in none of them.
    """
    automation = _arm(settings, on=True)
    out_of_session = session_mod.stamp(settings, datetime(2024, 1, 5, 13, 0, tzinfo=timezone.utc))
    _cached(settings, session=out_of_session)
    cached = automation_mod.read_list(settings, "Alpha")

    reason = automation_mod.list_is_stale(cached, automation, moment=NOW,
                                          session=session_mod.stamp(settings, NOW))

    assert reason and "another session" in reason
    assert out_of_session in reason and "in session" in reason, "both sides of the comparison"
    # ...and inside the session it WAS screened in, the same list is judged on its merits.
    assert automation_mod.list_is_stale(cached, automation, moment=NOW,
                                        session=out_of_session) is None


def test_a_list_that_does_not_say_which_session_it_belongs_to_cannot_be_acted_on(settings):
    """A stamp is not decoration: a list that cannot say which session it answers for cannot be
    shown to be this session's, and the loop asks for a fresh one instead of trusting it. This is
    also how a list cached by an older build heals itself."""
    automation = _arm(settings, on=True)
    _cached(settings, session="")

    reason = automation_mod.list_is_stale(automation_mod.read_list(settings, "Alpha"), automation,
                                          moment=NOW, session=session_mod.stamp(settings, NOW))
    assert "which session" in reason

    # A caller that cannot know the time judges on age and criteria alone — it must not invent
    # "another session" out of an unknown.
    assert automation_mod.list_is_stale(automation_mod.read_list(settings, "Alpha"), automation,
                                        moment=NOW, session=None) is None


def test_the_screener_stamps_the_session_it_screened_in(settings):
    """Whoever screens — the tick or the panel's ↻ — the cached document says which session the
    list is an answer for, because that is the only thing that can be compared later."""
    automation = _arm(settings, on=True)
    out_of_session = datetime(2024, 1, 5, 13, 0, tzinfo=timezone.utc)  # 08:00 ET

    outcome = list_mod.cache_and_screen(settings, "Alpha", automation, at=out_of_session)

    assert outcome["refreshed"] is True
    assert outcome["session"] == "2024-01-05 out of session"
    assert automation_mod.read_list(settings, "Alpha")["session"] == "2024-01-05 out of session"

    inside = list_mod.cache_and_screen(settings, "Alpha", automation, at=NOW)

    assert inside["refreshed"] is True, "a new session always screens a new list"
    assert inside["session"] == session_mod.stamp(settings, NOW)


def test_a_screening_in_a_new_session_replaces_the_old_list(settings):
    """The hole the age rule cannot see: the pre-market list of the SAME morning. Screened at
    09:00, judged at 09:31, it is half an hour old and yesterday's ranking — so the first tick of
    the session screens a new one rather than judging it."""
    automation = _arm(settings, on=True)
    before_the_bell = datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc)  # 09:00 ET
    list_mod.cache_and_screen(settings, "Alpha", automation, at=before_the_bell)

    outcome = list_mod.cache_and_screen(settings, "Alpha", automation, at=NOW)

    assert outcome["refreshed"] is True
    assert outcome["reason"] == ""
    assert automation_mod.read_list(settings, "Alpha")["session"] == session_mod.stamp(settings, NOW)


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------
def test_the_ranking_is_a_blend_of_performance_and_volume():
    """A single number would have to invent an exchange rate between percent and dollars, so the
    two places are summed: the best performer and the busiest name both lead, and a name that is
    neither is last."""
    ranked = list_mod.rank_rows(_frame(_list_rows(None)), 3)
    by_symbol = {row["symbol"]: row for row in ranked}

    assert ranked[0]["symbol"] in ("AAA", "BBB")
    assert by_symbol["AAA"]["rank_change"] == 1 and by_symbol["AAA"]["rank_volume"] == 2
    assert by_symbol["BBB"]["rank_change"] == 2 and by_symbol["BBB"]["rank_volume"] == 1
    assert by_symbol["AAA"]["score"] == by_symbol["BBB"]["score"] == 3
    assert by_symbol["CCC"]["score"] == 6 and ranked[-1]["symbol"] == "CCC"
    # Dollar volume, not share count: 9M shares of a $50 stock outranks 10M of a $2 one.
    assert by_symbol["BBB"]["dollar_volume"] > by_symbol["AAA"]["dollar_volume"]
    assert [row["rank"] for row in ranked] == [1, 2, 3]


def test_the_screen_asks_for_the_criteria_it_was_given(monkeypatch):
    """One request, and it carries the universe: the small-cap band, the price floor and the
    volume floor are the criteria's, not a second set invented here."""
    asked = {}

    def fake_run(spec, size=25, offset=0):
        asked["spec"] = spec
        asked["size"] = size
        return _frame(_list_rows(None))

    monkeypatch.setattr(list_mod.screener_data, "run_screen", fake_run)
    rows = list_mod.screen({"market_cap_min": 400_000_000, "market_cap_max": 1_000_000_000,
                            "min_price": 5.0, "min_volume": 250_000, "size": 2})

    assert asked["spec"].market_cap_min == 400_000_000
    assert asked["spec"].market_cap_max == 1_000_000_000
    assert asked["spec"].min_price == 5.0 and asked["spec"].min_volume == 250_000
    assert asked["spec"].us_only is True
    assert len(rows) == 2, "the list size is the criteria's"


def test_a_screen_is_cached_and_only_repeated_when_it_has_to_be(settings):
    calls = []

    def fake_screen(enter):
        calls.append(enter)
        return list_mod.rank_rows(_frame(_list_rows(None)), int(enter["size"]))

    automation = _arm(settings, on=True)
    first = list_mod.cache_and_screen(settings, "Alpha", automation, at=NOW, screen_call=fake_screen)
    second = list_mod.cache_and_screen(settings, "Alpha", automation, at=NOW, screen_call=fake_screen)

    assert first["refreshed"] is True and len(calls) == 1
    assert second["refreshed"] is False, "the cache is still current a moment later"
    assert second["rows"] == first["rows"]
    assert automation_mod.list_path(settings, "Alpha").exists()


def test_a_screener_that_refuses_keeps_the_list_it_had(settings):
    """Reported, not raised, and the previous list stays: a screener outage must not empty the
    panel, and it must not be mistaken for "no instruments qualify"."""
    automation = _arm(settings, on=True)
    _cached(settings)

    def refuse(enter):
        raise list_mod.screener_data.ScreenerError("rate limited")

    outcome = list_mod.cache_and_screen(settings, "Alpha", automation, force=True,
                                        at=NOW, screen_call=refuse)

    assert outcome["refreshed"] is False
    assert "rate limited" in outcome["error"]
    assert len(outcome["rows"]) == 3, "the list it already had"


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mode, current, expect_switch, needle",
    [
        ("not_in_list", "AAA", False, "already rank 1"),
        ("not_in_list", "CCC", False, "still in the list at rank 3"),
        ("not_in_list", "ZZZ", True, "dropped out of the top 3"),
        ("not_first", "AAA", False, "already rank 1"),
        ("not_first", "BBB", True, "now leads the list"),
        ("margin", "BBB", False, "under the margin"),
        ("margin", "ZZZ", True, "not on the list at all"),
    ],
)
def test_the_switch_criteria(mode, current, expect_switch, needle):
    rows = list_mod.rank_rows(_frame(_list_rows(None)), 3)
    automation = automation_mod.from_payload(
        {"on": True, "enter": {"size": 3}, "switch": {"mode": mode, "margin": 4}}
    )

    verdict = list_mod.decide(automation, current, rows)

    assert bool(verdict.get("switch")) is expect_switch
    assert needle in (verdict.get("reason") or verdict.get("skip") or "")
    if expect_switch:
        assert verdict["target"] == rows[0]["symbol"], "the number one on the list"


def test_the_margin_is_measured_between_blended_scores():
    rows = list_mod.rank_rows(_frame(_list_rows(None)), 3)   # AAA 3, BBB 3, CCC 6
    automation = automation_mod.from_payload(
        {"on": True, "enter": {"size": 3}, "switch": {"mode": "margin", "margin": 3}}
    )

    assert list_mod.decide(automation, "CCC", rows)["switch"] is True
    assert "by 3 points" in list_mod.decide(automation, "CCC", rows)["reason"]


# ---------------------------------------------------------------------------
# the gate the tick calls
# ---------------------------------------------------------------------------
def test_an_automation_that_is_off_costs_nothing(settings):
    """Not one read beyond its own file: no screener, no broker. The tick asks this gate on every
    bar, so "off" has to be free."""
    called = []
    automation_mod.write(settings, "Alpha", automation_mod.from_payload({"on": False}))

    decision = instrument_mod.evaluate(
        settings, "Alpha", at=NOW,
        refresh=lambda *a, **kw: called.append("refresh") or {},
    )

    assert decision["active"] is False and decision["switch"] is False
    assert called == []


def test_the_gate_screens_a_list_when_the_one_it_has_has_aged(settings):
    """The loop is the only process running when nobody is looking at the page, so the freshness
    the criteria ask for is the loop's to keep — through the same call the panel uses."""
    _arm(settings, on=True)
    called = []

    def refresh(s, strategy, automation=None, **kw):
        called.append(strategy)
        return {"rows": list_mod.rank_rows(_frame(_list_rows(None)), 3), "at": NOW.isoformat(),
                "text": "the criteria", "error": None, "refreshed": True}

    decision = instrument_mod.evaluate(settings, "Alpha", at=NOW, refresh=refresh)

    assert called == ["Alpha"]
    assert decision["active"] is True and decision["switch"] is True
    assert decision["target"] == "AAA"


def test_outside_the_session_the_gate_neither_screens_nor_switches(settings):
    """One rule with two effects, and the screener's own data is why.

    Its numbers are the last completed regular session's, so a list judged before the bell is
    yesterday's ranking wearing today's date — a switch made on it would spend the day's single
    switch on a session that has not started, and the next tick would judge the real thing again.
    Screening is skipped for the same reason and one more: nothing may be judged, so a provider
    call would buy nothing.
    """
    _arm(settings, on=True)
    called = []
    before_the_bell = datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc)  # 09:00 ET

    decision = instrument_mod.evaluate(
        settings, "Alpha", at=before_the_bell,
        refresh=lambda *a, **kw: called.append("refresh") or {},
    )

    assert called == [], "no provider call out of session"
    assert decision["active"] is True, "the automation IS on — it is the session that is closed"
    assert decision["switch"] is False
    assert "outside the 09:30–16:00 America/New_York session" in decision["skip"]


def test_the_first_tick_inside_the_session_judges_a_list_screened_inside_it(settings):
    """The whole point, end to end through the gate: the pre-market list is not judged, a new one
    is screened, and the switch that follows is made on this session's ranking."""
    automation = _arm(settings, on=False)  # a list exists before the session opens
    list_mod.cache_and_screen(
        settings, "Alpha", automation,
        at=datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),  # 09:00 ET, before the bell
    )
    _arm(settings, on=True)
    screens = []

    def refresh(s, strategy, criterion=None, **kw):
        screens.append(kw.get("at"))
        return list_mod.cache_and_screen(s, strategy, criterion, screen_call=_stub_screen, **kw)

    decision = instrument_mod.evaluate(settings, "Alpha", at=NOW, refresh=refresh)

    assert screens == [NOW], "the session change is what made the cached list unusable"
    assert decision["switch"] is True and decision["target"] == "AAA"
    assert decision["session"] == session_mod.stamp(settings, NOW)
    assert "AAA" in decision["reason"], "the switch says which name led the new list"


def test_a_position_in_the_way_holds_the_switch_back(settings, monkeypatch):
    """The guard that matters most: switching mid-position would hand a position opened under one
    instrument's rules to another. It waits, and it says what it is waiting for."""
    _arm(settings, on=True)

    class Held:
        known = True
        payloads = ({"symbol": "GPRO", "qty": "10"},)

    monkeypatch.setattr(instrument_mod.positions, "snapshot", lambda s, env, **kw: Held())
    monkeypatch.setattr(instrument_mod.positions, "describe", lambda states: "10 GPRO")

    decision = instrument_mod.evaluate(
        settings, "Alpha", at=NOW,
        refresh=lambda *a, **kw: {"rows": list_mod.rank_rows(_frame(_list_rows(None)), 3),
                                  "at": NOW.isoformat(), "error": None},
    )

    assert decision["switch"] is False
    assert "waiting until nothing is held" in decision["skip"]
    assert "10 GPRO" in decision["skip"]


def test_an_account_that_cannot_be_read_is_not_flat(settings, monkeypatch):
    _arm(settings, on=True)

    class Blind:
        known = False
        payloads = ()

    monkeypatch.setattr(instrument_mod.positions, "snapshot", lambda s, env, **kw: Blind())

    decision = instrument_mod.evaluate(
        settings, "Alpha", at=NOW,
        refresh=lambda *a, **kw: {"rows": list_mod.rank_rows(_frame(_list_rows(None)), 3),
                                  "at": NOW.isoformat(), "error": None},
    )
    assert decision["switch"] is False
    assert "could not be read" in decision["skip"]


def test_one_switch_a_day_is_read_from_the_day_s_log(settings):
    """The guard is not a counter this feature keeps: it reads the day's own tick log, so it
    cannot disagree with what the log says happened."""
    _arm(settings, on=True)

    class Rows:
        pass

    monkeypatch_rows = [{"action": "switched", "reason": "instrument automation: …"}]
    original = instrument_mod.last_switch_today
    instrument_mod.last_switch_today = lambda *a, **kw: monkeypatch_rows[0]
    try:
        decision = instrument_mod.evaluate(
            settings, "Alpha", at=NOW,
            refresh=lambda *a, **kw: {"rows": list_mod.rank_rows(_frame(_list_rows(None)), 3),
                                      "at": NOW.isoformat(), "error": None},
        )
    finally:
        instrument_mod.last_switch_today = original

    assert decision["switch"] is False
    assert "one switch a day" in decision["skip"]


def test_nothing_about_automation_can_raise_out_of_the_gate(settings):
    """A provider or a file that misbehaves is a reason to leave the instrument alone, never a
    reason for the loop to fail: automation that can stop trading by breaking is worse than none."""
    _arm(settings, on=True)

    def explode(*a, **kw):
        raise RuntimeError("the screener is on fire")

    decision = instrument_mod.evaluate(settings, "Alpha", at=NOW, refresh=explode)

    assert decision["switch"] is False
    assert "could not be evaluated" in decision["skip"]


# ---------------------------------------------------------------------------
# making it real
# ---------------------------------------------------------------------------
def _store(settings, name: str = "Alpha") -> None:
    store = rules_mod.load_store(settings)
    store.strategies[name] = rules_mod.empty_strategy(name, "GPRO")
    store.strategies[name].config = {"INSTRUMENT": "GPRO", "HISTORICAL_BAR_SIZE": "1h"}
    store.active = name
    rules_mod.save_store(settings, store)


def test_applying_a_switch_backfills_history_first_and_then_writes_the_instrument(settings):
    """History first, because a strategy pointed at an instrument with no dataset refuses every
    bar from then on: the order of these two writes is the difference between a switch and a
    stuck bot."""
    _store(settings)
    calls = []

    outcome = instrument_mod.apply(
        settings, "Alpha", "AAOI",
        backfill=lambda s, symbol: calls.append(("backfill", symbol)) or 120,
    )

    assert outcome["ok"] is True and outcome["from"] == "GPRO" and outcome["to"] == "AAOI"
    assert calls == [("backfill", "AAOI")]
    stored = rules_mod.load_store(settings).strategies["Alpha"]
    assert stored.config["INSTRUMENT"] == "AAOI"
    assert stored.instrument == "AAOI", "the mirror the panel and the resolver read"
    assert stored.config["HISTORICAL_BAR_SIZE"] == "1h", "and nothing else was touched"


def test_history_that_cannot_be_fetched_leaves_the_instrument_alone(settings):
    """The one failure this feature could introduce on its own: a switch without data would trade
    a working instrument for a stuck one."""
    _store(settings)

    def refuse(s, symbol):
        raise RuntimeError("the provider is down")

    outcome = instrument_mod.apply(settings, "Alpha", "AAOI", backfill=refuse)

    assert outcome["ok"] is False and "history could not be fetched" in outcome["reason"]
    stored = rules_mod.load_store(settings).strategies["Alpha"]
    assert stored.config["INSTRUMENT"] == "GPRO", "still trading what it can"


def test_a_store_that_cannot_be_written_abandons_the_switch(settings, monkeypatch):
    _store(settings)

    def refuse(*a, **kw):
        raise ValueError("the strategy store cannot be written")

    monkeypatch.setattr(instrument_mod.rules_mod, "set_strategy_config", refuse)
    outcome = instrument_mod.apply(settings, "Alpha", "AAOI", backfill=lambda s, sym: 10)

    assert outcome["ok"] is False and "could not be written" in outcome["reason"]


# ---------------------------------------------------------------------------
# and the tick itself
# ---------------------------------------------------------------------------
def test_a_switch_ends_the_tick_at_its_own_gate(tmp_path, monkeypatch):
    """The instrument is what every step below would be about, so the tick stops where it changed
    it: the next boundary re-reads the store and trades the new symbol."""
    from src.config.trading_state import write_state as arm_state

    monkeypatch.setattr(state_files, "state_path", lambda s, name: tmp_path / name)
    monkeypatch.setattr(orchestrator, "active_strategy_name", lambda: "Alpha")
    settings = _settings(tmp_path)
    arm_state(settings, {"on": True, "since": "2024-01-05T13:00:00+00:00",
                         "strategy": "Alpha", "env": "paper"})
    monkeypatch.setattr(orchestrator.instrument_mod, "evaluate",
                        lambda s, name, **kw: {"active": True, "switch": True, "target": "AAOI",
                                               "reason": "GPRO has dropped out of the top 10",
                                               "notes": [], "skip": ""})
    monkeypatch.setattr(orchestrator.instrument_mod, "apply",
                        lambda s, name, target, **kw: {"ok": True, "from": "GPRO", "to": target,
                                                       "rows": 120, "reason": ""})

    record = orchestrator.tick(settings, now=NOW, record=False)

    assert record["action"] == "switched"
    assert record["stage"] == "instrument"
    assert "instrument automation" in record["reason"] and "AAOI" in record["reason"]
    assert "120 bars of history" in record["reason"]
    assert record["intents"] == [] and record["trades"] == [], "nothing else ran"


def test_a_switch_that_could_not_be_made_does_not_stop_the_tick(tmp_path, monkeypatch):
    """It is recorded in the notes and the tick carries on with the instrument it has: the switch
    was due, not made, and the bot still has something it can trade."""
    from src.config.trading_state import write_state as arm_state

    monkeypatch.setattr(state_files, "state_path", lambda s, name: tmp_path / name)
    monkeypatch.setattr(orchestrator, "active_strategy_name", lambda: "Alpha")
    monkeypatch.setattr(orchestrator, "execution_status", lambda s: {"ok": True})
    settings = _settings(tmp_path)
    arm_state(settings, {"on": True, "since": "2024-01-05T13:00:00+00:00",
                         "strategy": "Alpha", "env": "paper"})
    monkeypatch.setattr(orchestrator.instrument_mod, "evaluate",
                        lambda s, name, **kw: {"active": True, "switch": True, "target": "AAOI",
                                               "reason": "GPRO has dropped out of the top 10",
                                               "notes": [], "skip": ""})
    monkeypatch.setattr(orchestrator.instrument_mod, "apply",
                        lambda s, name, target, **kw: {"ok": False,
                                                       "reason": "its history could not be fetched"})

    record = orchestrator.tick(
        settings, now=NOW, record=False,
        sync_call=lambda: None,
        clock_call=lambda: {"is_open": False},
    )

    assert record["action"] != "switched"
    assert record["stage"] == "clock", "it walked on to the next gate"
    assert any("instrument automation could not switch" in note for note in record["notes"])
    assert write_state is not None and arm_state is not None
