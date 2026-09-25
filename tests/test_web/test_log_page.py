"""The trading log: the day's ticks and submitted orders, and the page that shows them.

The case that matters most is the one nobody tests by accident — a log that is not there. The
page exists to answer "what happened", and it is most needed precisely when something went
wrong; if a deleted or never-written log renders as an error, the page fails exactly when it
is wanted. So the absent-file behaviour is pinned here rather than assumed.
"""

from __future__ import annotations

import json
import re
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
def test_the_page_says_which_strategy_it_is_showing():
    """There is no strategy picker on this page — it follows the lab — so the sidebar has to
    name the strategy every panel below it belongs to. Without that line the reader has to work
    out whose session the ticks, orders, trades and roadmap are, which is how a page ended up
    being read as the previous strategy's."""
    assert 'id="lg-strategy-name"' in client.get("/log").text

    body = LOG_JS.read_text(encoding="utf-8")
    assert "Strategy: ${name}" in body, "and it is filled from the payload's own name"
    assert "lg-strategy-name" in body
    # The empty roadmap names the strategy too: "nothing has ticked yet" over a panel that
    # belongs to a named strategy is a sentence about the wrong thing.
    assert "${name} has not ticked yet" in body


def test_a_log_that_was_never_written_renders_as_an_empty_day(api_settings):
    """The never-run case: an empty day, not a 404 and not an error.\n
    The day it is empty FOR is the exchange's today, and today is in the menu whether or not the
    loop has ever run: a new session starts empty, and a page that opened on the newest day with
    a log would show yesterday's session under today's date.
    """
    body = client.get("/api/v1/log").json()

    assert body["ok"] is True
    assert body["strategy"] == "Alpha"
    assert body["day"] == body["today"], "a day is always named, and it is today"
    assert body["ticks"] == [] and body["orders"] == []
    assert body["days"] == [body["today"]], "today is selectable even with nothing in it"


def test_the_log_follows_the_active_strategy_and_not_the_one_that_last_ran(tmp_path, monkeypatch):
    """The page is scoped to ONE strategy, and it is the strategy the lab has selected.

    The switch's stamp records which strategy the LAST run belonged to, and that is what the
    monitor used to read — so after the operator moved on to another strategy the page kept
    showing the previous one's day (its ticks, orders, trades and roadmap) beside the NEW
    strategy's charts and account. Half a page belonging to something else cannot be read at all.

    Trading is OFF here, which is exactly when the two can disagree: while it is on, the picker is
    locked and the stamp and the active strategy are the same one by construction.
    """
    from src.model import rules as rules_mod

    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    settings = _settings(tmp_path, strategy_rules_file=str(tmp_path / "store.json"))
    write_state(settings, {"on": False, "strategy": "Alpha", "env": "paper"})
    ran = _tick(settings, action="decided", name="Alpha")      # the day that ran

    def store(active, names):
        rules_mod.save_store(settings, rules_mod.StrategyStore(
            active=active,
            strategies={n: rules_mod.RuleSet(name=n) for n in names},
        ))

    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        store("Beta", ["Alpha", "Beta"])                       # Beta is selected, never ran
        beta = client.get("/api/v1/log").json()
        beta_trades = client.get("/api/v1/trades").json()

        store("Alpha", ["Alpha", "Beta"])                      # back to the one that ran
        alpha = client.get("/api/v1/log").json()
        alpha_trades = client.get("/api/v1/trades").json()
    finally:
        app.dependency_overrides.clear()

    assert beta["strategy"] == "Beta"
    assert beta["ticks"] == [], "Beta has no session to show"
    assert beta["days"] == [beta["today"]], "and its menu is today, the day being shown"
    assert beta["day"] == beta["today"], "and still names a day, so the page has something to draw"
    assert beta_trades["strategy"] == "Beta" and beta_trades["trades"] == []

    assert alpha["strategy"] == "Alpha"
    assert [t["action"] for t in alpha["ticks"]] == ["decided"], "the old session comes back"
    assert alpha["days"] == [ran["day"]], "and its day is listed again"
    assert alpha_trades["strategy"] == "Alpha"


def test_trading_on_keeps_the_monitor_on_the_ARMED_strategy(tmp_path, monkeypatch):
    """While trading is on, the log belongs to the RUN, so the switch's stamp wins.

    The store can be edited behind the switch's back (nothing stops a hand-edited file), and a
    monitor that followed such an edit would show an empty page for a strategy that is trading.
    """
    from src.model import rules as rules_mod

    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    settings = _settings(tmp_path, strategy_rules_file=str(tmp_path / "store.json"))
    write_state(settings, {"on": True, "strategy": "Alpha", "env": "paper"})
    _tick(settings, action="decided", name="Alpha")
    rules_mod.save_store(settings, rules_mod.StrategyStore(
        active="Beta", strategies={n: rules_mod.RuleSet(name=n) for n in ("Alpha", "Beta")}))

    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        body = client.get("/api/v1/log").json()
    finally:
        app.dependency_overrides.clear()

    assert body["strategy"] == "Alpha", "what is running is what its log is about"
    assert [t["action"] for t in body["ticks"]] == ["decided"]


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
    assert body["ticks"] == []
    assert body["days"] == [body["today"]], "still a menu, with the day being shown in it"
    assert body["day"], "still names a day, so the picker has something to show"


def test_the_day_returns_ticks_newest_first(api_settings):
    settings = api_settings
    early = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    _tick(settings, action="refused", reason="stale bar", when=early)
    _tick(settings, action="decided", when=late)

    # Asked for BY NAME: the page opens on today, so a day that is not today has to be named.
    body = client.get("/api/v1/log?day=2026-09-17").json()

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
    # The menu: today first (the day the page opens on, session or not), then the days it has.
    assert body["days"] == [body["today"], "2026-09-17", "2026-09-16"]


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
    for element in ("lg-accounts", "lg-refresh", "lg-days", "lg-positions", "lg-ticks",
                    "lg-orders", "lg-trades"):
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
    assert '["when", "account", "action"' in body, "the columns are named"
    assert "ticks.slice(from, from + TICKS_PER_PAGE)" in body, (
        "and what the table renders is a page of those filtered rows, not a count beside them"
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

// The three trading boxes are the shared module's. What this harness is about is the SCOPING of
// every panel, so they are stubbed to something identifiable here — their own contents are
// exercised in `test_live_refresh` and in the shared module's tests.
const TraiderSwitch = {
  tile: (label, value) => `<div class="bt-stat">${label} ${value}</div>`,
  envTile: (env) => `<div class="bt-stat">Mode ${env}</div>`,
  tradeTile: (payload) => `<div class="bt-stat">Trading ${payload ? "read" : "—"}</div>`,
  openTile: () => '<div class="bt-stat">Open ?</div>',
  flipEnv: () => Promise.resolve(false),
};

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

// One more pass with something actually WORKING, for the two columns the panel grew: an order
// resting for a symbol this run does not trade is still working in the account, and when it was
// submitted is what makes a past session's order visibly not today's.
state.orders.open = [
  { id: "o-1", symbol: "GPRO", side: "buy", type: "limit", qty: "10", status: "new",
    submitted_at: "2026-09-17T13:31:00+00:00" },
  { id: "o-2", symbol: "OTHERCO", side: "sell", type: "stop", qty: "5", status: "new",
    submitted_at: "2026-09-18T14:31:00+00:00" },
];
renderAccount();
out.working = els["lg-working"].innerHTML;

// The equity pane's own rule, and the one thing the trades table's new scope could have broken:
// the rows behind it are the strategy's WHOLE history, so a round trip closed on another day —
// or in the other account — must not switch the account's pane on for the session being drawn.
const dated = (day, env) => ({ at: "2026-09-18T15:00:00+00:00", env: env || "paper", day,
                               ret: 0.01, direction: "long" });
state.loop.env = "paper";
state.trades = { trades: [dated("2026-09-11")] };
out.equityOtherDay = equityWanted();
state.trades = { trades: [dated("2026-09-18")] };
out.equityThisDay = equityWanted();
state.trades = { trades: [dated("2026-09-11"), dated("2026-09-18")] };
out.equityEitherDay = equityWanted();
state.trades = { trades: [dated("2026-09-18", "live")] };
out.equityOtherAccount = equityWanted();
state.trades = { trades: [{ env: "paper", ret: 0.01 }] };
out.equityUndated = equityWanted();

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


# ---------------------------------------------------------------------------
# a prose cell: ONE line, and open until the reader says otherwise
# ---------------------------------------------------------------------------
# The helpers from ``esc`` down to the account scoping, which is everything ``proseCell`` and
# ``toggleProse`` touch and nothing that needs the page. Run in node for the reason the scoping
# harness exists: "which cells are open" is a fact about a Set and a render in sequence, and a
# source check cannot tell whether the second render remembers the first click.
PROSE_START = "const esc ="
PROSE_END = "/* The account this PAGE is about"

PROSE_HARNESS = r"""
// The three things toggleProse touches, and nothing else: the key it reads, the class it flips
// and the closest() it climbs to find the cell. ONE cell per key, as the DOM has: a second click
// lands on the cell the first one already opened, not on a fresh one.
const cells = {};
function fakeCell(key) {
  if (cells[key]) return cells[key];
  const classes = new Set();
  const cell = {
    dataset: { prose: key },
    classList: {
      contains: (name) => classes.has(name),
      toggle: (name) => (classes.has(name) ? (classes.delete(name), false) : (classes.add(name), true)),
    },
    closest: () => cell,
  };
  cells[key] = cell;
  return cell;
}

const out = {};
out.collapsed = proseCell("a reason that runs on and on and on", "tick:A:reason");
// A click on that cell, then the SAME row rendered again — which is what a poll does.
out.toggled = toggleProse(fakeCell("tick:A:reason"));
out.after_a_re_render = proseCell("a reason that runs on and on and on", "tick:A:reason");
out.another_row = proseCell("another row's reason", "tick:B:reason");
// A second click shuts it again.
toggleProse(fakeCell("tick:A:reason"));
out.after_a_second_click = proseCell("a reason that runs on and on and on", "tick:A:reason");
out.notes = notesCell(["the bar has a volume of 0", "and a high under its low"], "C");
out.no_notes = notesCell([], "C");
out.escaped = proseCell("<script>alert(1)</script>", "order:x");
// A click that lands somewhere else entirely: the page is full of them.
out.elsewhere = toggleProse({ closest: () => null });

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def prose(tmp_path_factory) -> dict:
    source = LOG_JS.read_text(encoding="utf-8")
    start = source.index(PROSE_START)
    script = tmp_path_factory.mktemp("prose") / "prose.js"
    script.write_text(source[start:source.index(PROSE_END, start)] + PROSE_HARNESS,
                      encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "prose harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_a_prose_cell_renders_collapsed_and_opens_ON_ITS_ROW(prose):
    """The cell the reader clicks, as it actually renders.

    Collapsed is a ``td.prose`` with the sentence in a span the CSS clips to one line and ends
    with the browser's ellipsis; open adds the ``open`` class. The KEY is what makes the state
    survive: the tables are rebuilt whenever a poll brings a row, and an expansion held only in the
    DOM would shut while it was being read.
    """
    collapsed = prose["collapsed"]
    assert collapsed.startswith('<td class="prose" data-prose="tick:A:reason">')
    assert '<span class="prose-text" tabindex="0">' in collapsed, "focusable, so the keyboard reaches it"
    assert "a reason that runs on and on and on" in collapsed

    assert prose["toggled"] is True
    opened = prose["after_a_re_render"]
    assert opened.startswith('<td class="prose open" data-prose="tick:A:reason">'), (
        "the row is still open after being rendered again"
    )
    # ...and ONLY that row: the other rows of the same table are untouched.
    assert prose["another_row"].startswith('<td class="prose" data-prose="tick:B:reason">')
    # A second click shuts it, or a cell could be opened and never closed.
    assert prose["after_a_second_click"].startswith('<td class="prose" data-prose="tick:A:reason">')


def test_the_notes_ride_in_the_same_cell_and_are_escaped(prose):
    """The loop's notes are prose too, so they open the same way — and every sentence is escaped
    on the way in, whatever the broker or the provider put in it."""
    notes = prose["notes"]
    assert notes.startswith('<td class="prose warn" data-prose="tick:C:notes">')
    assert "⚠" in notes and "the bar has a volume of 0" in notes
    assert prose["no_notes"] == "<td>—</td>", "an em dash when there is nothing to say"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in prose["escaped"]
    assert prose["elsewhere"] is False, "a click on anything else is not a click on a cell"


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
    # The FIGURES say which account is on the panel now that the name line above them is gone —
    # and they are the thing that must not be mixed: the idle account's equity is a number waiting
    # to be read as the traded one's, which is the failure this whole group exists for.
    assert "25000.00" in live["accounts"] and "100000.00" not in live["accounts"]
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
    assert "100000.00" in paper["accounts"] and "25000.00" not in paper["accounts"]
    assert "PAPERCO" in paper["positions"] and "LIVECO" not in paper["positions"]
    assert "paper tick" in paper["ticks"] and "live tick" not in paper["ticks"]
    assert "c-paper" in paper["orders"] and "c-live" not in paper["orders"]
    assert "paper trade" in paper["trades"], "this account's own trade is the one that IS shown"
    assert "from the live account" not in paper["trades"], "no note about a filter that dropped none"


def test_the_working_orders_table_names_the_symbol_and_when_each_order_was_placed(scoping):
    """The account's whole book, with the two columns that make it readable.

    A working order outlives the session that placed it, and it can belong to a symbol this run
    does not trade. Neither is a reason to hide it from a panel that answers "what is working",
    so the SYMBOL is a column — and so is the moment it was submitted, because an order resting
    from a past session has to be visibly not this one's.
    """
    working = scoping["working"]

    assert "<th>symbol</th>" in working and "<th>submitted</th>" in working
    assert "GPRO" in working, "the instrument this run trades"
    assert "OTHERCO" in working, "a symbol it does not trade is still working in the account"
    assert "o-1" in working and "o-2" in working, "every row of the book, not just today's"
    assert "2026" in working, "each order carries the moment it was submitted"


def test_the_accounts_pane_is_still_about_the_DAY_on_screen(scoping):
    """The rows behind it are the strategy's whole history now, so the pane picks its own day out
    of them: a curve under today's candles because a round trip closed last week would be a verdict
    on the wrong session.

    A row with no ``day`` at all counts — the same tolerance ``mine`` gives a row with no account:
    it cannot be shown to be another session's.
    """
    assert scoping["equityOtherDay"] is False
    assert scoping["equityThisDay"] is True
    assert scoping["equityEitherDay"] is True, "one of the day's own is enough"
    assert scoping["equityOtherAccount"] is False, "and still that account's round trips only"
    assert scoping["equityUndated"] is True


def test_the_page_script_never_declares_a_function_twice():
    """Two functions with one name, in one scope, is a silent overwrite — and the LAST one wins.

    Found the hard way: a helper added for the screener lists was called ``clockText``, which
    the countdown already used for its face. Function declarations hoist, so the new one took
    over EVERY call site above it, and the tick timer started printing an hour and minute read
    off a millisecond count instead of ``00:12:34``. Nothing in a browser console says so; the
    only symptom is a number that is quietly wrong.

    So the whole script is checked, not the two names: the same mistake with any other helper
    would be just as invisible.
    """
    import collections
    import re

    scripts = {
        "log.js": LOG_JS,
        "app.js": Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js",
    }
    for name, path in scripts.items():
        text = path.read_text(encoding="utf-8")
        declared = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", text, re.M)
        twice = [key for key, count in collections.Counter(declared).items() if count > 1]
        assert twice == [], f"{name} declares these functions more than once: {twice}"
        assert declared, f"{name}: the pattern found no functions at all, so it proves nothing"


def test_the_days_two_lists_sit_under_the_days_as_collapsible_references(api_settings):
    """The day's tape, under the day menu: the biggest gainers, and the small caps doing the most
    volume — the same two screens the Market page shows as panels.

    They are REFERENCE — nothing on this page screens anything to trade — so the only control each
    carries is the ↻ that screens it again, wearing the icon alone and sitting UNDER the title
    rather than beside it: the column is 240px wide and the table beside it is wider, so the head is
    the title and nothing else. The ↻ names the panel it screens, because two lists sit here and one
    handler has to know which of them was pressed.
    """
    html = client.get("/log").text

    menu = html.index('class="card report-menu"')
    days = html.index('id="lg-days"')
    gainers = html.index('id="lg-gainers-card"')
    smallcaps = html.index('id="lg-smallcaps-card"')
    assert menu < days < gainers < smallcaps, "in the day menu, gainers first, small caps under it"

    for card, body, title, key in (
        ("lg-gainers-card", "lg-gainers-body", "Top 10 gainers", "top_gainers"),
        ("lg-smallcaps-card", "lg-smallcaps-body", "Small caps by volume", "small_cap_volume"),
    ):
        head = html[html.index(f'id="{card}"') : html.index(f'id="{body}"')]
        assert f"<h2>{title}</h2>" in head
        assert 'onclick="toggleCard(event)"' in head, "collapsible, the way every panel here folds"
        assert "refreshMarketList" not in head, "the ↻ is below the title, not in the head"

        # ...and the controls come before the table, under that title.
        whole = html[html.index(f'id="{card}"') :]
        whole = whole[: whole.index("</section>")]
        slot = body.replace("-body", "")
        assert f'id="{slot}-refresh"' in whole and f'id="{slot}-at"' in whole
        assert whole.index(f'id="{slot}-refresh"') < whole.index(f'id="{slot}-at"')
        assert whole.index(f'id="{slot}-at"') < whole.index(f'id="{slot}-list"')
        assert 'class="muted note"' not in whole, "no description line above the table"

        # The ↻, and NOTHING else in it: the button's own text is the arrow, and its title carries
        # the sentence a caption would have needed.
        button = re.search(rf'id="{slot}-refresh"[^>]*>([^<]*)</button>', html, re.S)
        assert button is not None
        assert button.group(1).strip() == "↻", "the icon alone, no caption"
        assert "title=" in button.group(0), "so the title says what the arrow does"
        assert f"refreshMarketList('{key}')" in button.group(0), "and which of the two it screens"

    # Read-only on this page: nothing here trades from either list, and nothing edits one.
    for gone in ("screen-order", "screen-save", "tradeScreen", "saveScreen"):
        assert gone not in html, gone


def test_each_list_carries_the_market_panels_columns_behind_a_sideways_scroll():
    """The lists ARE the Market page's panels, so they carry the columns those panels rank by: the
    day's change and the volume, plus the cap that says how big whatever did it is.

    Three numbers and a rank fit a 240px menu only sideways — the column is scrolled to the reader's
    window, not narrowed to it — and the name, the price and the exchange, which there is no room
    for here, ride in each row's tooltip. The rows are in the screener's own order, which IS the
    ranking, so nothing on this page sorts them.
    """
    js = LOG_JS.read_text(encoding="utf-8")
    body = js[js.index("function renderScreen(list) {") :]
    body = body[: body.index("\n  }")]

    for column in ('<th class="num">chg %</th>', '<th class="num">volume</th>',
                   '<th class="num">cap</th>', "<th>symbol</th>"):
        assert column in body, column
    for cell in ("percentText(row.change_percent)", "compactNumber(row.volume)",
                 "compactNumber(row.market_cap)"):
        assert cell in body, cell
    assert "row.name" in body, "the name rides in the row's tooltip"
    assert "row.exchange_name" in body, "and so does the exchange"
    assert "index === 0" in body, "the first row is the leader, so it is marked"
    assert ".sort(" not in body, "the screener's order is the ranking; this page does not sort"

    css = (Path(__file__).resolve().parents[2] / "src" / "web" / "static"
           / "style.css").read_text(encoding="utf-8")
    rule = css[css.index(".lg-screen-list {") :]
    rule = rule[: rule.index("}")]
    assert "overflow: auto" in rule, "both ways: bounded vertically, scrolled sideways"
    assert "min-width: 340px" in css[css.index(".lg-screen-list .lg-table {"):][:120], \
        "a table that is allowed to be wider than the column it sits in"


def test_each_list_is_the_market_screeners_panel_read_once_and_never_edited():
    """The page reads each list from the Market page's own endpoint, so the two pages cannot
    disagree about what is up today — and it reads it ONCE: a panel reaches the provider, and a
    reload that re-screened every list would be provider traffic for numbers that only change when
    the session does. The ↻ is the one thing that reads again, and the label beside it is when that
    read happened rather than an age that would have to be re-rendered to stay honest.

    The numbers are the screener's, and it reports the last COMPLETED regular session, so the
    tooltip says which print this is — a list from this morning read at four in the afternoon must
    not read as the live tape. A failed read leaves the table standing: an emptied table would say
    "nothing matched", which is the one thing this page must not invent.
    """
    js = LOG_JS.read_text(encoding="utf-8")

    assert 'api(`/api/v1/market/panel/${list.key}?size=${SCREEN_WINDOW}`)' in js, "the market panel"
    assert "screenRows(payload)" in js and "slice(0, SCREEN_ROWS)" in js, \
        "the panel's order cut to ten, because the screener's own filters can shorten the window"
    assert 'key: "top_gainers"' in js and 'key: "small_cap_volume"' in js, "both panels, named"
    assert "loadScreens();" in js, "read on the page's ordinary reload"
    assert "if (readScreen(list)) renderScreenLabel(list);" in js, \
        "and a list already on screen is only re-stamped, never re-screened"
    assert "window.refreshMarketList = refreshMarketList;" in js, "the ↻, from the markup"
    assert "/api/v1/automation" not in js, "the automation's list is the lab's, not this page's"

    read = js[js.index("async function screenAgain(list) {") :]
    read = read[: read.index("\n  }")]
    assert "if (!host.innerHTML)" in read, "a failure says so only where there is no table yet"
    assert "state.screens[list.key] = { payload, at: Date.now() }" in read, \
        "what was read, and when — the label is that clock reading"

    label = js[js.index("function renderScreenLabel(list) {") :]
    label = label[: label.index("\n  }")]
    assert "readTime(entry.at)" in label, "when this page read it"
    assert "last COMPLETED regular session" in label, "and what print the numbers are"

    # The ↻ wears the working state while it screens and says a failure out loud; a successful one
    # goes through the same single read, so both paths leave the same entry behind.
    refresh = js[js.index("async function refreshMarketList(key) {") :]
    refresh = refresh[: refresh.index("\n  }")]
    assert "button.disabled = true" in refresh, "the screener reaches the provider, so it shows"
    assert 'at.textContent = "screening…"' in refresh
    assert "screening failed" in refresh, "and a failure is said out loud"
    assert "await screenAgain(list)" in refresh, "the one read, told which list to read"


def test_a_sentence_in_these_tables_is_ONE_LINE_until_it_is_opened(api_settings):
    """The cells that answer a QUESTION show their first line, and open on a click.

    The refusal the broker wrote is the reason someone opens this table — "equity 100000.00 at
    weight 0.001 affords $10.00 of NVDA at 226.89, less than one share — and a fractional order
    cannot carry a stop/take bracket …" runs past 150 characters. Wrapping it in place was the
    first fix and it was not enough: three lines of it under EVERY row is a table nobody can read
    across, and the question the cell answers is only asked sometimes. So the cell shows one line,
    the browser's own ellipsis says there is more, and a click opens it.

    The three parts are all load-bearing: the COLLAPSE (one line, clipped), the OPEN state
    (wrapped, in the same column), and the memory of WHICH cells are open — the tables are rebuilt
    whenever a poll brings a new row, and an expansion that lived only in the DOM would snap shut
    while it was being read. Anything the CSS gets wrong here is invisible to a source check, so
    what is pinned is the arrangement: the ellipsis on the collapsed state, no clipping on the open
    one, and nothing setting a minimum width, which is what pushed the table past the panel.
    """
    js = LOG_JS.read_text(encoding="utf-8")

    assert "function proseCell(" in js, "one helper, so a prose cell is the same everywhere"
    assert "proseCell(detail" in js, "the broker's refusal"
    assert "proseCell(tick.reason" in js, "and a tick's reason"
    assert "proseCell(trade.reason" in js, "and why a round trip closed"
    assert "proseCell(`⚠" in js, "and the loop's notes"
    assert 'class="warn">⚠' not in js and '${cell(tick.reason)}' not in js, "no nowrap leftovers"
    # The row KEY travels with every cell, or an opened paragraph shuts on the next poll.
    assert "openProse.has(key)" in js and "openProse.add(cell.dataset.prose)" in js
    assert "data-prose=" in js and "cell.classList.toggle(\"open\")" in js
    assert "tick:${tick.at}:reason" in js and "order:${key}" in js, (
        "keys are namespaced: a trade is booked at the tick's own moment, so a bare timestamp "
        "would let a click on one table open a cell in the other"
    )
    assert "window.getSelection" in js, "a click that ends a selection is not a click on the cell"

    css = (Path(__file__).resolve().parents[2] / "src" / "web" / "static"
           / "style.css").read_text(encoding="utf-8")
    prose = css[css.index(".lg-table td.prose {"):]
    prose = prose[: prose.index("}")]
    assert "white-space: normal" in prose
    assert "overflow-wrap: anywhere" in prose, "a long token still has to break"
    assert "max-width: 46ch" in prose, "the column, not the text, decides how wide this is"
    assert "min-width" not in prose, "no floor: the column takes what the numbers leave"
    assert "vertical-align: top" in prose

    collapsed = css[css.index(".lg-table td.prose .prose-text {"):]
    collapsed = collapsed[: collapsed.index("}")]
    assert "white-space: nowrap" in collapsed, "the whole point: one line"
    assert "overflow: hidden" in collapsed and "text-overflow: ellipsis" in collapsed, (
        "and the ellipsis has to be the browser's, so only a cell that really overflows shows one"
    )
    assert "cursor: pointer" in collapsed, "it is a control while it is clipped"

    opened = css[css.index(".lg-table td.prose.open .prose-text {"):]
    opened = opened[: opened.index("}")]
    assert "white-space: normal" in opened, "opened, the sentence wraps in the same column"
    assert "overflow: visible" in opened, "and is not clipped by the rule that made it collapsible"

    nowrap = css.index(".lg-table td.bad, .lg-table td:first-child { white-space: nowrap; }")
    assert nowrap < css.index(".lg-table td.prose {"), "declared after what it has to beat"

    # And nothing is silently cut when even that does not fit: the panel scrolls.
    assert "#lg-orders, #lg-ticks, #lg-trades { overflow-x: auto; }" in css


def test_the_way_back_sits_at_the_top_of_the_day_menu(api_settings):
    """Leaving is the same gesture on both screens, and it does not depend on the header.

    The report page already puts its back link at the top of its menu; this page carries the
    same one in the same place rather than a second convention in the header.
    """
    html = client.get("/log").text

    menu = html.index('class="card report-menu"')
    back = html.index('href="/">← Back to the Strategy lab')
    assert menu < back < html.index("<h2>Days</h2>"), "at the top of the menu, above the days"
    assert back < html.index('id="lg-days"'), "and before the list it belongs to"


def test_the_account_card_carries_the_switch_and_not_what_is_held(api_settings):
    """Two questions, two panels: what the account IS — what it is worth, and whether the bot is
    trading it — and what it HOLDS. The mode and the switch are part of the first, because "which
    account" and "is it on" are one question; the holdings are not, because equity and a position
    are different reads of the broker and one of them is empty most of the time.

    The three trading boxes are the dashboard's three, from the shared module: the mode, the switch
    and what is open. The separate "Trading status" block that used to hold a chip, a switch button
    and a warning under all of that is gone — it was the same three answers in a second style.
    """
    html = client.get("/log").text

    card = html.index("<h2>Account</h2>")
    accounts = html.index('id="lg-accounts"')
    loop = html.index("The Loop")
    assert card < html.index('id="lg-refresh"') < accounts < loop, (
        "the reload is in this card's own head, above the boxes it re-reads"
    )
    for gone in ("lg-state", "lg-strategy", "lg-loop-warning", "lg-trading-toggle"):
        assert f'id="{gone}"' not in html, f"{gone} went with the Trading status block"


def test_positions_and_working_orders_have_their_own_panel_under_the_loop(api_settings):
    """One panel, two sections: a working order is not a position, so they cannot share a table —
    but they are the same question asked of the same place, so they cannot be two cards either.

    The page reads top-down as sent, held, closed: what the bot submitted, then what the broker
    holds, then what the strategy closed out of it.
    """
    html = client.get("/log").text

    card = html.index("Positions and working orders")
    records = html.index("Orders the bot submitted")
    assert html.index("The Loop") < card, "the loop, then the tables that explain it"
    assert records < card < html.index("Trades closed"), (
        "what was sent, then what is held, then what came of it — the last panel on the page"
    )
    assert card < html.index('id="lg-protection"') < html.index('id="lg-positions"')
    assert html.index('id="lg-positions"') < html.index('id="lg-working"')


def test_the_security_card_is_a_place_for_the_auth_component_to_fill(api_settings):
    """The page owns the box and where it sits; auth.js owns what goes in it.

    It ships HIDDEN and there is no styling or text of its own, because with no PIN set the card is
    the one thing on this page that is about the portal rather than the account — and it must not
    flash "No PIN" at somebody for a frame before the status call comes back.
    """
    html = client.get("/log").text

    card = html.index('id="lg-security-card"')
    assert 'data-security hidden' in html[card - 40 : card + 200], "hidden until it knows"
    assert 'data-security-state' in html and 'data-security-note' in html
    assert 'data-security-actions' in html
    assert html.index('id="lg-accounts"') < card < html.index("The Loop"), (
        "high up: 'this portal is open' is worth seeing without going looking"
    )
    assert "/static/auth.css" in html and "/static/auth.js" in html, "and it must be able to style"


def test_every_panel_below_the_account_folds_on_the_same_gesture(api_settings):
    """Four panels, one mechanism, as asked for: the loop showed the pattern first and the three
    tables under it were given the same head rather than a second convention.

    What folds is the BODY: the head stays, which is what keeps a panel's title — and the loop's
    countdown — on screen while its tables are away. The button is the whole of the keyboard
    story, so each head has exactly one.
    """
    html = client.get("/log").text

    for body in ("loop-body", "positions-body", "orders-body", "trades-body"):
        assert f'class="collapse-body" id="{body}"' in html, body
    # The three tables are inside their own bodies, not floating under the head's.
    # Its slice runs to the next card's head, which is the trades panel — the last one on the page.
    card = html[html.index("Positions and working orders") :]
    card = card[: card.index("Trades closed")]
    for element in ("lg-protection", "lg-positions", "lg-working"):
        assert f'id="{element}"' in card, element
    # And the panels that fold are exactly the ones with a body: an unfoldable card with a head
    # that looks like the others is a click that does nothing.
    assert html.count('class="card collapsible"') == html.count('class="collapse-body"')
    assert html.count('onclick="toggleCard(event)"') == 2 * html.count('class="card collapsible"'), (
        "the head and its button both fold it"
    )


def test_the_log_page_runs_the_shared_switch_and_the_shared_boxes(api_settings):
    """One confirmation for both pages, and one set of boxes: this page loads the dashboard's
    shared module before its own script, which is what uses it."""
    html = client.get("/log").text

    assert "onclick=\"toggleTrading()\"" not in html, (
        "the switch button is gone: the box is built by the module, and its handler comes with it"
    )
    assert html.index("/static/trading_switch.js") < html.index("/static/log.js")
    # The page's own handlers, reachable from the markup the module renders.
    js = LOG_JS.read_text(encoding="utf-8")
    assert "window.toggleTrading = toggleTrading;" in js
    assert "window.flipMode = flipMode;" in js
    assert 'TraiderSwitch.tradeTile(state.trading, "toggleTrading()")' in js
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
