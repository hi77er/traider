"""The trading log: the day's ticks and submitted orders, and the page that shows them.

The case that matters most is the one nobody tests by accident — a log that is not there. The
page exists to answer "what happened", and it is most needed precisely when something went
wrong; if a deleted or never-written log renders as an error, the page fails exactly when it
is wanted. So the absent-file behaviour is pinned here rather than assumed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.config.trading_state import write_state
from src.execution import store
from src.web.app import app

client = TestClient(app)
LOG_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "log.js"


def _settings(tmp_path, **kwargs) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        live_dir=str(tmp_path / "data" / "live_results"),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
    )
    values.update(kwargs)
    return Settings(**values)


@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    settings = _settings(tmp_path)
    write_state(settings, {"on": True, "strategy": "Alpha", "env": "paper"})
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.clear()


def _tick(settings, *, action="decided", reason="", when=None, order_ids=None, name="Alpha"):
    at = when or datetime.now(timezone.utc)
    record = store.tick_record(
        strategy=name, env="paper", action=action, reason=reason, settings=settings, at=at,
        order_ids=order_ids or [], bar="2026-09-17T17:30:00+00:00",
    )
    store.append_tick(settings, name, record, when=at)
    store.save_latest(settings, name, record)
    store.upsert_day(settings, name, record["day"], events=1, decided=1)
    return record


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------
def test_a_log_that_was_never_written_renders_as_an_empty_day(api_settings):
    """The never-run case: an empty day, not a 404 and not an error."""
    body = client.get("/api/v1/log").json()

    assert body["ok"] is True
    assert body["strategy"] == "Alpha"
    assert body["day"], "a day is always named, even with nothing in it"
    assert body["ticks"] == [] and body["orders"] == [] and body["days"] == []


def test_a_log_that_was_DELETED_renders_as_an_empty_day(api_settings):
    """Someone removed the log. The page has to keep working — that is when it is read.

    The broker half is unaffected, which is exactly why the page leads with it.
    """
    settings = api_settings
    _tick(settings, action="decided")
    ticks_dir = store.ticks_dir(settings, "Alpha")
    for path in ticks_dir.glob("*.jsonl"):
        path.unlink()
    store.index_path(settings, "Alpha").unlink()

    body = client.get("/api/v1/log").json()

    assert body["ok"] is True
    assert body["ticks"] == [] and body["days"] == []
    assert body["day"], "still names a day, so the picker has something to show"


def test_the_day_returns_ticks_newest_first(api_settings):
    settings = api_settings
    early = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    _tick(settings, action="refused", reason="stale bar", when=early)
    _tick(settings, action="decided", when=late)

    body = client.get("/api/v1/log").json()

    assert [tick["action"] for tick in body["ticks"]] == ["decided", "refused"], "newest first"
    assert body["day"] == "2026-09-17"


def test_the_orders_the_bot_submitted_come_from_the_loop_s_own_rows(api_settings):
    """The local half: what the machine tried to do, with the ids that join it to Alpaca."""
    settings = api_settings
    store.append_order(
        settings, "Alpha",
        store.order_record(
            settings=settings, strategy="Alpha", env="paper", at=datetime.now(timezone.utc),
            bar="2026-09-17T17:30:00+00:00",
            intent={"intent": "open", "status": "filled", "price": 101.5, "expected": 101.0,
                    "order_id": "ord-9", "client_order_id": "traider-Alpha-b3"},
        ),
    )

    body = client.get("/api/v1/log").json()

    assert len(body["orders"]) == 1
    assert body["orders"][0]["client_order_id"] == "traider-Alpha-b3"
    assert body["orders"][0]["order_id"] == "ord-9"
    assert body["orders"][0]["intent"] == "open"


def test_a_refused_order_carries_the_brokers_reason_to_the_page(api_settings):
    """A refusal only ever exists in this table: the broker refused the order, so there is
    no such order in Alpaca's list and no position to show in the panels above — the loop's
    own row is the one place it can be recorded, and it has to say why."""
    settings = api_settings
    store.append_order(
        settings, "Alpha",
        store.order_record(
            settings=settings, strategy="Alpha", env="paper", at=datetime.now(timezone.utc),
            bar="2026-09-17T17:30:00+00:00",
            intent={"intent": "open", "reason": "signal", "status": "rejected",
                    "detail": "Alpaca refused the request with 403 [40310000] insufficient "
                              "buying power",
                    "client_order_id": "traider-Alpha-b4"},
        ),
    )

    body = client.get("/api/v1/log").json()

    assert len(body["orders"]) == 1
    assert body["orders"][0]["status"] == "rejected"
    assert body["orders"][0]["order_id"] is None, "nothing was created at the broker"
    assert "insufficient buying power" in body["orders"][0]["detail"]


def test_another_day_can_be_asked_for(api_settings):
    settings = api_settings
    _tick(settings, action="off", reason="trading is OFF", when=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc))
    _tick(settings, action="decided", when=datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc))

    body = client.get("/api/v1/log?day=2026-09-16").json()

    assert body["day"] == "2026-09-16"
    assert [tick["action"] for tick in body["ticks"]] == ["off"]
    assert body["days"] == ["2026-09-17", "2026-09-16"], "the day menu, newest first"


def test_the_log_endpoint_never_fails_on_a_strategy_with_no_files(api_settings, monkeypatch):
    """A brand-new strategy, or a tree that was moved: an empty answer, not a 500."""
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    body = client.get("/api/v1/log?day=2026-09-17").json()

    assert body["ok"] is True and body["ticks"] == [] and body["orders"] == []


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
def test_the_page_is_served_with_its_own_placeholders(api_settings):
    response = client.get("/log")

    assert response.status_code == 200
    html = response.text
    for element in ("lg-state", "lg-days", "lg-positions", "lg-ticks", "lg-orders", "lg-trades"):
        assert f'id="{element}"' in html, element
    assert "/static/log.js" in html


def test_the_page_puts_the_account_before_the_local_record(api_settings):
    """Not decoration: the broker is the truth and the local rows are the explanation.

    A page that led with its own files would let a stale or deleted log pass for the state of
    the account, which is the one mistake this screen must not make.
    """
    html = client.get("/log").text

    assert html.index("Account") < html.index("What the loop did")
    assert html.index("What the loop did") < html.index("Trades closed")


def test_the_ticks_table_says_which_account_each_tick_ran_for_and_shows_only_one(api_settings):
    """A tick log outlives the switch that produced it: paper and live ticks carry the same
    strategy, the same bars and the same wording in ``reason``, so a day of paper ticks reads
    exactly like a day of live ones.

    It happened on this machine — the only tick ever written was a PAPER one, shown under a
    ``live`` header. The record had carried ``env`` all along, so the column names it now. The
    ROWS are filtered to the account in play as well, and that is the part that matters: naming a
    row is a label a reader can skip, while a row that is not there cannot be misread. The empty
    line names the other account and counts what it dropped, so an empty table cannot pass for a
    day the loop never ran.
    """
    from pathlib import Path

    log_js = (Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "log.js").read_text(
        encoding="utf-8"
    )
    start = log_js.index("function renderTicks()")
    body = log_js[start:log_js.index("function renderOrders()", start)]

    assert '"account"' in body, "the column exists"
    assert "accountCell(tick.env)" in body, "and it comes from the record, not the mode in play"
    assert "all.filter(mine)" in body, "the rows are the in-play account's"
    assert "table(\n      [\"when\", \"account\"" in body and "      ticks," in body, (
        "and the filter is what the table renders, not a count beside it"
    )
    assert "otherEnv()" in body, "and the empty line names whose rows were dropped"
    assert "nothing for the ${inPlayEnv()} account on this day" in body

    # Which account is "in play" comes from the loop (the same source the strategy line uses),
    # falling back to the accounts payload, and it stays empty rather than guessing when neither
    # could be read — an unknown mode must not filter rows out of a page.
    helper = log_js[log_js.index("function inPlayEnv("):log_js.index("function mine(")]
    assert "state.loop.env" in helper and "state.accounts.env" in helper
    assert '|| ""' in helper

    # A record with no ``env`` at all counts as this account's: it cannot be shown to be the other
    # one's, and dropping it would hide a row that is probably this page's.
    scoping = log_js[log_js.index("function mine("):log_js.index("function otherEnv(")]
    assert "if (!inPlay) return true" in scoping
    assert "!env || env === inPlay" in scoping


# ---------------------------------------------------------------------------
# one account, end to end
# ---------------------------------------------------------------------------
# The real renderers, run against data holding BOTH accounts — which is the state the loop's files
# are actually in, since they are written per strategy and only the ``env`` on each record separates
# the two. A source-string test can see that a filter is written; only running the code shows it
# works, and for a rule stated as "no paper data on a live page" that difference is the whole point.
SCOPING_START = "const COUNTDOWN_MS"
SCOPING_END = "\n  async function loadLog("

SCOPING_HARNESS = r"""
// ---- fakes: the elements the renderers fill, and the page's own state ----------
const els = {};
function make(id) { els[id] = { id: id, innerHTML: "" }; return els[id]; }
global.document = {
  hidden: false,
  getElementById: (id) => (els[id] === undefined ? null : els[id]),
};
const $ = (id) => document.getElementById(id);

const state = {
  loop: { env: "live", last_tick: null }, trading: null, log: null,
  positions: null, accounts: null, orders: null, trades: null,
};
["lg-ticks", "lg-day", "lg-orders", "lg-trades", "lg-accounts", "lg-env", "lg-protection",
 "lg-positions", "lg-working"].forEach(make);

// One day's records: both accounts' rows in the same files, exactly as the loop writes them.
const tick = (env, at, reason) => ({
  at, env, action: "decided", reason, bar: "2026-09-18T14:20:00+00:00", signal: "HOLD",
  order_ids: [], notes: [],
});
const order = (env, clientId) => ({
  at: "2026-09-18T14:31:00+00:00", env, intent: "entry", status: "filled", price: 1,
  expected: 1, bar: "2026-09-18T14:20:00+00:00", order_id: "b-" + env,
  client_order_id: clientId, detail: "",
});
const trade = (env, reason) => ({
  at: "2026-09-18T15:00:00+00:00", env, direction: "long", entry_price: 1, exit_price: 2,
  ret: 0.01, weight: 1, bars: 3, reason,
});
const position = (env, symbol) => ({
  env, known: true, count: 1,
  positions: [{ symbol, qty: 10, avg_entry_price: 1, market_value: 10, unrealized_pl: 1 }],
});

state.accounts = { env: "live", accounts: [
  { env: "paper", known: true, account: "****PAPR", equity: 100000, day_pl: 0, day_pl_pct: 0,
    cash: 1, buying_power: 2, status: "ACTIVE" },
  { env: "live", known: true, account: "****LIVE", equity: 25000, day_pl: 0, day_pl_pct: 0,
    cash: 1, buying_power: 2, status: "ACTIVE" },
] };
state.positions = {
  instrument: "GPRO", env: "live",
  positions: [position("paper", "PAPERCO"), position("live", "LIVECO")],
};
state.orders = { ok: true, open: [], resting: [], closed: [], protection: { state: "none" } };
state.log = {
  day: "2026-09-18",
  ticks: [tick("paper", "2026-09-18T14:29:51+00:00", "paper tick"),
          tick("live", "2026-09-18T15:29:51+00:00", "live tick")],
  orders: [order("paper", "c-paper"), order("live", "c-live")],
};
// The trades exist for ONE account only. In the other mode the panel has to be empty AND say whose
// trade it is — "nothing has been closed" would be the lie this test exists to prevent.
state.trades = { trades: [trade("paper", "paper trade")] };

const shot = () => {
  renderAccount();   // the accounts panel, the page header, and the positions panel
  renderTicks();
  renderOrders();
  renderTrades();
  return {
    env: els["lg-env"].textContent,
    accounts: els["lg-accounts"].innerHTML,
    positions: els["lg-positions"].innerHTML,
    ticks: els["lg-ticks"].innerHTML,
    orders: els["lg-orders"].innerHTML,
    trades: els["lg-trades"].innerHTML,
  };
};

const out = { live: shot() };
state.loop.env = "paper";
state.accounts.env = "paper";
out.paper = shot();

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def scoping(tmp_path_factory) -> dict:
    source = LOG_JS.read_text(encoding="utf-8")
    start = source.index(SCOPING_START)
    script = tmp_path_factory.mktemp("scoping") / "scoping.js"
    script.write_text(source[start:source.index(SCOPING_END, start)] + SCOPING_HARNESS,
                      encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "scoping harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_live_page_shows_only_live_data(scoping):
    """The rule, run: with live in play, not one row of the paper account's reaches the page —
    not its equity, not a position, not a tick, not an order, not a trade.

    This is the state the mix was found in: the switch had been moved to live, the live account had
    no credentials to read, and the only numbers on the page belonged to the idle paper account.
    Every panel is checked, because each of them reads a different source — the broker for the
    figures and the positions, and the loop's own per-strategy files for the records, where both
    accounts' rows sit side by side.
    """
    live = scoping["live"]

    assert live["env"] == "live · GPRO", "the header names the account the page is scoped to"
    assert "****LIVE" in live["accounts"] and "****PAPR" not in live["accounts"]
    assert "LIVECO" in live["positions"] and "PAPERCO" not in live["positions"]
    assert "live tick" in live["ticks"] and "paper tick" not in live["ticks"]
    assert "c-live" in live["orders"] and "c-paper" not in live["orders"]
    assert "paper trade" not in live["trades"], "the paper round trip is not this account's"

    # And the empty panel says whose trade it is, with the count — "nothing has been closed" would
    # be false, and an empty table with no explanation reads as exactly that.
    assert "no round trip has been closed for the live account" in live["trades"]
    assert "1 trade from the paper account" in live["trades"]


def test_the_paper_page_shows_only_paper_data(scoping):
    """The mirror image, which is the case that catches a filter hard-coded to "hide paper": with
    paper in play the live account's rows are the ones that must not be there."""
    paper = scoping["paper"]

    assert paper["env"] == "paper · GPRO"
    assert "****PAPR" in paper["accounts"] and "****LIVE" not in paper["accounts"]
    assert "PAPERCO" in paper["positions"] and "LIVECO" not in paper["positions"]
    assert "paper tick" in paper["ticks"] and "live tick" not in paper["ticks"]
    assert "c-paper" in paper["orders"] and "c-live" not in paper["orders"]
    assert "paper trade" in paper["trades"], "this account's own trade is the one that IS shown"
    assert "from the live account" not in paper["trades"], "no note about a filter that dropped none"


def test_the_way_back_sits_at_the_top_of_the_day_menu(api_settings):
    """Leaving is the same gesture on both screens, and it does not depend on the header.

    The report page already puts its back link at the top of its menu; this page carries the
    same one in the same place rather than a second convention in the header.
    """
    html = client.get("/log").text

    menu = html.index('class="card report-menu"')
    back = html.index('href="/">← Back to dashboard')
    assert menu < back < html.index("<h2>Days</h2>"), "at the top of the menu, above the days"
    assert back < html.index('id="lg-days"'), "and before the list it belongs to"


def test_the_account_card_carries_the_switch_and_not_what_is_held(api_settings):
    """Two questions, two panels: what the account IS — what it is worth, and whether the bot is
    trading it — and what it HOLDS. The switch is part of the first, because "which account" and
    "is it on" are one question; the holdings are not, because equity and a position are different
    reads of the broker and one of them is empty most of the time."""
    html = client.get("/log").text

    accounts = html.index('id="lg-accounts"')
    loop = html.index("The Loop")
    for control in ("lg-state", "lg-strategy", "lg-trading-toggle", "lg-refresh", "lg-loop-warning"):
        assert accounts < html.index(f'id="{control}"') < loop, control


def test_positions_and_working_orders_have_their_own_panel_under_the_loop(api_settings):
    """One panel, two sections: a working order is not a position, so they cannot share a table —
    but they are the same question asked of the same place, so they cannot be two cards either."""
    html = client.get("/log").text

    card = html.index("Positions and working orders")
    records = html.index("Orders the bot submitted")
    assert html.index("The Loop") < card, "the account's panel, then the loop, then this"
    assert card < html.index('id="lg-protection"') < html.index('id="lg-positions"') < records
    assert html.index('id="lg-positions"') < html.index('id="lg-working"') < records


def test_the_log_page_runs_the_shared_switch_not_a_copy(api_settings):
    """One confirmation for both pages: this one loads the dashboard's switch, and before its
    own script, which is what uses it."""
    html = client.get("/log").text

    assert 'id="lg-trading-toggle"' in html
    assert "onclick=\"toggleTrading()\"" in html
    assert html.index("/static/trading_switch.js") < html.index("/static/log.js")
    # ...and a dialog to ask with, since the switch refuses to start without one.
    for element in ("lg-confirm-backdrop", "lg-confirm-ok", "lg-confirm-cancel", "toast"):
        assert f'id="{element}"' in html, element


# ---------------------------------------------------------------------------
# the bar's notes (7.4): reported, never acted on
# ---------------------------------------------------------------------------
def test_the_notes_about_an_odd_bar_come_back_with_the_tick(api_settings):
    """The note is part of the record, so the page renders it rather than re-deriving it.

    What was odd about a bar was decided when the bar was decided (``src.data.quality``), and
    a page that recomputed it later from today's data would describe a different bar.

    The day is ASKED FOR rather than left to the endpoint's default. Without ``day`` the
    reader falls back to the newest day in the strategy's index, and then to the exchange's
    today — and ``append_tick`` does not write an index entry, so a tick written for a fixed
    date would be looked for in today's file instead.
    """
    settings = api_settings
    at = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    record = store.tick_record(
        strategy="Alpha", env="paper", action="decided", settings=settings, at=at,
        bar="2026-09-17T17:30:00+00:00", notes=["the bar has a volume of 0"],
    )
    store.append_tick(settings, "Alpha", record, when=at)

    body = client.get("/api/v1/log", params={"day": "2026-09-17"}).json()

    assert body["ticks"][0]["notes"] == ["the bar has a volume of 0"]


def test_a_tick_written_before_notes_existed_is_still_readable(api_settings):
    """Old logs stay readable. The field is new, the file is append-only, and a page that
    assumed it was there would show an empty table for every day before it existed.

    The day is asked for, for the reason given in the test above: an index entry is not
    written here, so the endpoint's default would look in today's file.
    """
    settings = api_settings
    at = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)
    old = {
        "at": at.isoformat(), "day": "2026-09-17", "strategy": "Alpha", "env": "paper",
        "action": "decided", "reason": "", "bar": "2026-09-17T17:30:00+00:00",
    }
    store.append_tick(settings, "Alpha", old, when=at)

    body = client.get("/api/v1/log", params={"day": "2026-09-17"}).json()

    assert [tick["action"] for tick in body["ticks"]] == ["decided"]
    assert "notes" not in body["ticks"][0], "the row is passed through as it was written"
