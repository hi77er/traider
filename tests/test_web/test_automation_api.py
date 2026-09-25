"""The Instrument Automation panel: its endpoints, and the page that drives them.

What is pinned here is the shape of the contract rather than the arithmetic (that is
``tests/test_instrument_automation.py``): the criteria go to their own file beside the data, the
preview the panel shows is the tick's own decision, and — the one rule that had to be decided
rather than derived — **the panel stays writable while trading is ON**, because turning the
automation off is how you stop it. Every other strategy-config write on this page is refused then.

``tests/test_web/conftest.py`` points the app at a throwaway data root, so these write nothing
that belongs to the developer's machine.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import automation as automation_mod
from src.config import session as session_mod
from src.data import instrument_automation as list_mod
from src.web.app import app

ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _rows() -> list:
    return [
        {"symbol": "AAA", "name": "Best", "price": 10.0, "change_percent": 9.0,
         "volume": 4_000_000, "market_cap": 900_000_000, "sector": "Tech"},
        {"symbol": "BBB", "name": "Busiest", "price": 50.0, "change_percent": 2.0,
         "volume": 9_000_000, "market_cap": 1_500_000_000, "sector": "Energy"},
    ]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The screener is a provider call: a test asks for it, never lets it happen."""
    monkeypatch.setattr(list_mod, "screen", lambda enter, **kw: list_mod.rank_rows(
        __import__("pandas").DataFrame(_rows()), int(enter.get("size") or 2)
    ))


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------
def test_the_panel_reads_the_criteria_the_list_and_the_verdict(client):
    body = client.get("/api/v1/automation").json()

    assert body["on"] is False, "nothing changes the instrument until it is asked to"
    assert body["criteria"]["on"] is False
    assert body["criteria"]["enter"]["market_cap_min"] == 300_000_000
    assert body["criteria"]["enter"]["market_cap_max"] == 2_000_000_000
    assert [f["name"] for f in body["criteria"]["enter_fields"]][:2] == [
        "market_cap_min", "market_cap_max"]
    assert any(m["value"] == "not_in_list" for m in body["criteria"]["switch_modes"])
    assert body["list"]["rows"] == []
    assert "no list" in body["list"]["stale"]
    assert body["preview"]["switch"] is False
    assert "off" in body["preview"]["skip"]
    assert body["strategy"], "the page is strategy-scoped, like every other panel"


def test_the_criteria_are_their_own_file_beside_the_data(client, _tmp_data_root):
    saved = client.post("/api/v1/automation", json={
        "on": True, "enter": {"size": 4, "min_price": 7.5}, "switch": {"mode": "not_first"},
    }).json()

    assert saved["ok"] is True
    stored = client.get("/api/v1/automation").json()
    assert stored["on"] is True
    assert stored["criteria"]["enter"]["size"] == 4
    assert stored["criteria"]["enter"]["min_price"] == 7.5
    assert stored["criteria"]["switch"]["mode"] == "not_first"

    path = automation_mod.locate(_tmp_data_root, stored["strategy"])
    assert path.exists() and path.parent == Path(_tmp_data_root.historical_data_dir).parent
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["on"] is True and document["enter"]["size"] == 4


def test_criteria_that_cannot_mean_anything_are_refused_with_the_field(client):
    body = client.post("/api/v1/automation", json={
        "on": True, "enter": {"size": 0, "market_cap_min": 2_000_000_000,
                              "market_cap_max": 300_000_000},
    }).json()

    assert body["ok"] is False and body["errors"]
    assert any("List size" in e for e in body["errors"])
    assert any("cap floor" in e for e in body["errors"])
    assert client.get("/api/v1/automation").json()["on"] is False, "and nothing was stored"


def test_the_panel_can_be_turned_OFF_while_trading_is_ON(client, _tmp_data_root):
    """The rule this panel needs and no other write here has.

    Every strategy-config route sits behind ``require_trading_off``, and if this one did, the
    automation could only be disarmed while the thing it drives was stopped.
    """
    from src.config import state_files

    state_files.write_json(
        automation_mod.locate(_tmp_data_root, "any").parent / "trading.json",
        {"on": True, "strategy": "Alpha", "env": "paper"},
    )

    body = client.post("/api/v1/automation", json={"on": False, "enter": {"size": 5}}).json()

    assert body["ok"] is True, "the switch has to work while a loop is trading"
    assert body["payload"]["on"] is False


def test_the_refresh_endpoint_screens_and_caches_the_list(client, _tmp_data_root):
    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2}})
    body = client.post("/api/v1/automation/refresh").json()

    assert body["ok"] is True and body["rows"] == 2
    rows = body["payload"]["list"]["rows"]
    assert [row["symbol"] for row in rows] == ["AAA", "BBB"]
    assert rows[0]["rank"] == 1 and rows[0]["dollar_volume"] == 40_000_000
    assert body["payload"]["list"]["at"], "the list says when it was screened"
    assert list_mod.screen is not None


def test_the_panel_preview_is_the_tick_s_own_decision(client, _tmp_data_root, monkeypatch):
    """The screen and the loop must not be able to disagree about what the criteria mean, so the
    preview asks the same function the tick calls with the same list.

    The clock is pinned open here: the session gate is the other half of that agreement and has
    its own test, and this one is about the DECISION being shared rather than re-implemented.
    """
    monkeypatch.setattr(session_mod, "is_open_at", lambda settings, when=None: True)
    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2}})
    client.post("/api/v1/automation/refresh")

    body = client.get("/api/v1/automation").json()
    verdict = list_mod.decide(
        automation_mod.read(_tmp_data_root, body["strategy"]),
        body["instrument"],
        body["list"]["rows"],
    )

    assert body["preview"]["switch"] == verdict["switch"]
    assert (body["preview"]["reason"] or body["preview"]["skip"]) == (
        verdict.get("reason") or verdict.get("skip") or "")


def test_the_panel_judges_the_list_from_its_own_session(client, _tmp_data_root, monkeypatch):
    """The panel shows both halves of the session rule, because the tick acts on them: which
    session the list is an answer for, and why it stops being usable when that changes.

    A badge that said "fresh" for a list the loop would re-screen before judging it would be the
    panel disagreeing with the thing it exists to show.
    """
    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2}})
    body = client.post("/api/v1/automation/refresh").json()["payload"]

    assert body["list"]["session"] == body["session"], "stamped with the session it was taken in"
    assert body["list"]["stale"] is None, "screened just now, in this session"
    assert body["in_session"] in (True, False), "and the panel says whether that session is now"

    # One session later the same document is not judged, whatever its age: the screener's numbers
    # are the last completed session's, which is what makes the session — not the clock — the bound.
    monkeypatch.setattr(session_mod, "stamp", lambda settings, when=None: "2099-01-01 in session")
    later = client.get("/api/v1/automation").json()

    assert "another session" in later["list"]["stale"]


def test_the_panel_says_when_the_numbers_are_not_this_session_s(client, _tmp_data_root,
                                                                 monkeypatch):
    """A list screened before the bell is FRESH and YESTERDAY'S at the same time — the screener
    reports the last completed regular session, so its age says seconds while its change % is the
    previous close's. The age cannot say that; the session stamp can, and both pages say it.
    """
    # Both stamps are taken from the REAL function before either patch goes in — patching
    # ``stamp`` first would make the second call return the first value.
    morning = datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc)     # 07:00 ET
    midday = datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)      # 12:00 ET
    out_of_session = session_mod.stamp(_tmp_data_root, morning)
    in_session = session_mod.stamp(_tmp_data_root, midday)
    assert "out of session" in out_of_session and "in session" in in_session

    monkeypatch.setattr(session_mod, "stamp", lambda settings, when=None: out_of_session)
    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2}})
    body = client.post("/api/v1/automation/refresh").json()["payload"]

    assert body["list"]["session"] == out_of_session
    assert body["list"]["screened_in_session"] is False, "the panel can tell the reader"

    # Screened inside the session, the same payload says the opposite...
    monkeypatch.setattr(session_mod, "stamp", lambda settings, when=None: in_session)
    body = client.post("/api/v1/automation/refresh").json()["payload"]
    assert body["list"]["screened_in_session"] is True

    # ...and the lab renders the words, from that flag rather than from parsing the stamp. The
    # session monitor carries the same caveat about the screener's session, but not the flag: its
    # two lists are the market screener's panels, which report no session of their own.
    assert 'id="automation-list-when"' in INDEX, "the lab's own label, beside its ↻"
    log_html = (ROOT / "src" / "web" / "templates" / "log.html").read_text(encoding="utf-8")
    log_js = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")
    assert "lg-auto-at" not in log_html, "the monitor's lists are not the automation's"
    assert "screened_in_session" not in log_js, "and it has no list with a session to report"
    assert "screened_in_session === false" in APP_JS, "the flag decides the words"
    assert "last session's close" in APP_JS
    assert "last COMPLETED regular session" in APP_JS, "and the tooltip says what that means"
    assert "last COMPLETED regular session" in log_js, "as does the monitor's, for its own lists"


def test_outside_the_session_the_panel_promises_nothing(client, _tmp_data_root, monkeypatch):
    """The tick refuses to judge outside the session, so the preview must refuse too: a panel that
    said "the next tick would switch to AAA" while the loop would not look at the list at all is
    the one thing this preview exists to prevent."""
    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2}})
    client.post("/api/v1/automation/refresh")
    monkeypatch.setattr(session_mod, "is_open_at", lambda settings, when=None: False)

    body = client.get("/api/v1/automation").json()

    assert body["preview"]["active"] is True, "the automation is on; the session is what is closed"
    assert body["preview"]["switch"] is False
    assert "outside the 09:30–16:00 America/New_York session" in body["preview"]["skip"]


def test_a_criteria_change_drops_the_list_it_invalidated(client, _tmp_data_root):
    """A list screened for another universe is an answer to another question, so it goes rather
    than being judged against the new criteria."""
    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2}})
    client.post("/api/v1/automation/refresh")

    client.post("/api/v1/automation", json={"on": True, "enter": {"size": 2, "min_price": 11.0}})

    assert client.get("/api/v1/automation").json()["list"]["rows"] == []


# ---------------------------------------------------------------------------
# the panel
# ---------------------------------------------------------------------------
def test_the_panel_sits_below_risk_management_and_reads_top_to_bottom():
    """The order is the feature: the switch, what may ENTER the list, the list itself, then what
    makes the tick ACT on it."""
    card = INDEX[INDEX.index('id="automation-card"') :]
    card = card[: card.index("</section>")]

    for element in ("automation-on", "automation-enter", "automation-list", "automation-switch",
                    "automation-save", "automation-msg"):
        assert f'id="{element}"' in card, element

    order = [card.index(f'id="{element}"') for element in
             ("automation-on", "automation-enter", "automation-list", "automation-switch")]
    assert order == sorted(order), "toggle, entering criteria, list, switch criteria"
    assert INDEX.index('id="risk-card"') < INDEX.index('id="automation-card"')


def test_the_panel_is_controls_and_a_list_and_almost_no_prose():
    """Asked for, and asked for twice: the panel is read by someone who already knows what it is.

    Nothing but the card head comes before the switch, neither criteria block is introduced by a
    sentence, and the list carries no commentary under it — what is left is the control, the
    fields, the rows and the two headings that separate them.
    """
    card = INDEX[INDEX.index('id="automation-card"') :]
    card = card[: card.index("</section>")]
    body = card[card.index('id="automation-body"') :]

    above_switch = body[: body.index('id="automation-on"')]
    assert "<p" not in above_switch, "not one sentence above the switch"
    assert "muted" not in above_switch, "not even an empty message element"

    for gone in ("automation-preview", "automation-list-meta", "Entering criteria"):
        assert gone not in card, f"{gone} was removed from the panel"
    assert "matching</h3>" in card, "the list is headed by two words and the size"
    assert 'class="muted note"' not in body, "no explanatory paragraphs left in the panel"

    # ...and the renderer agrees: no preview line, no note under the table.
    assert "automation-preview" not in APP_JS
    assert "Ranked by the SUM" not in APP_JS


def test_the_switch_is_the_shared_slider_and_not_a_squashed_copy():
    """``input[type=checkbox].switch`` is a 40x22 slider with a 16px knob: a panel that sizes the
    box itself gets a knob filling it, which reads as a broken control."""
    assert ".automation-switch input.switch" not in CSS
    assert 'class="switch"' in INDEX[INDEX.index('id="automation-card"') :], \
        "the toggle wears the shared class"
    assert ".automation-switch {" in CSS


def test_the_page_wires_every_control_the_panel_carries():
    for call in ("saveAutomation()", "refreshAutomationList()", "onAutomationToggle()",
                 "toggleAutomationPanel(event)"):
        assert call in INDEX, call
    # The panel is read at boot and re-read on the page's slow cadence, so the loader is called
    # from the script rather than from the markup.
    assert APP_JS.count("loadAutomation()") >= 2
    for name in ("loadAutomation", "renderAutomation", "renderAutomationList", "saveAutomation",
                 "refreshAutomationList", "onAutomationToggle", "collectAutomation"):
        assert f"function {name}(" in APP_JS, name
    assert 'api("/api/v1/automation")' in APP_JS
    assert 'api("/api/v1/automation/refresh"' in APP_JS


def test_the_panel_renders_the_servers_criteria_rather_than_its_own_copy():
    """The criteria are described by the PYTHON side (``ENTER_FIELDS``/``SWITCH_FIELDS``), so a
    criterion added there appears in the panel with no second edit — and the panel cannot offer a
    field the loop does not read."""
    assert "enter_fields" in APP_JS and "switch_fields" in APP_JS
    assert "fieldInput(automationField(" in APP_JS
    # ...and it renders the list the server screened, not one it composed itself.
    assert "automation-list" in APP_JS and "rank_change" in APP_JS


def test_the_panel_is_styled_like_the_rest_of_this_sidebar():
    assert ".automation-fields" in CSS and "grid-template-columns" in CSS
    assert "#automation-list .lg-table" in CSS, "the list reuses the page's table"
    assert ".automation-state.on" in CSS, "and the one line under the switch says which it is"
