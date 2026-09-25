"""The ↻ in each panel's head: re-read THAT panel now, instead of waiting out the poll.

Asked for, panel by panel: the Loop, Positions and working orders, Orders the bot submitted and
Trades closed. The page already had one ↻ — the Account card's, which re-reads everything — and
these are deliberately not that: a reader looking at the last tick should not have to make the
page ask the broker about positions, or redraw the session chart, to find out what it decided.

Two halves, and both matter:

* the WIRING — a button in each of the four heads, folded away with the panel it re-reads (the
  countdown in the Loop's head is the exception, and stays), stopping the click before the head's
  own collapse handler sees it, and reachable from the markup it is written in;
* the BEHAVIOUR — what each handler actually reads and re-renders, run for real under node, and
  what it does when a read fails: reported, with the panel left as it was rather than emptied into
  a "nothing happened" that never happened.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOG_HTML = (ROOT / "src" / "web" / "templates" / "log.html").read_text(encoding="utf-8")
LOG_JS = ROOT / "src" / "web" / "static" / "log.js"

#: The four panels, by the heading in the head the button has to sit in.
PANELS = [
    ("The Loop", "refreshLoop", "renderTicks()"),
    ("Positions and working orders", "refreshPositions", "renderAccount()"),
    ("Orders the bot submitted", "refreshOrders", "renderOrders()"),
    ("Trades closed", "refreshTrades", "renderTrades()"),
]

#: The chart's ↻, which is the one that DOES redraw a chart — its own. The render slot is the chart
#: redraw itself, so the "a panel refresh must not redraw the chart" rule below (a rule about the
#: TABLE panels) is kept on ``PANELS`` and this one is checked separately.
CHART = ("Session chart", "refreshChart", "renderSession(")

#: Every head that carries one, for the wiring checks.
HEADS = PANELS + [CHART]

#: Handlers whose read+render pair lives in a helper they share with another caller: the day's
#: round trips are re-read WITH the day (``loadLog`` does it too, so the table and the account's
#: pane answer for the session being shown), and the render goes with them.
DELEGATED = {"refreshTrades": "loadTrades"}

START_MARKER = "async function reloadDayRecords() {"
END_MARKER = "\n  window.refreshLoop = refreshLoop;"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the panel handlers"
)


# ---------------------------------------------------------------------------
# the source, as the browser gets it
# ---------------------------------------------------------------------------
def refresh_block() -> str:
    """The four handlers and the reader they share, lifted verbatim out of ``log.js``.

    Stopped before the ``window.`` exports on purpose: the harness keeps them as plain functions,
    and their reachability from the markup is asserted separately — that is a fact about the file,
    not about what they do.
    """
    src = LOG_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    return src[start : src.index(END_MARKER, start)]


def handler(name: str) -> str:
    """One handler's body, out of the block they all live in."""
    block = refresh_block()
    start = block.index(f"async function {name}(")
    ends = [i for i in (block.find("\n  async function ", start + 1),
                        block.find("\n  window.", start + 1)) if i != -1]
    return block[start : min(ends) if ends else len(block)]


def head_of(heading: str) -> str:
    """A panel's head, from its ``<h2>`` to the body the fold hides."""
    head = LOG_HTML[LOG_HTML.index(f"<h2>{heading}</h2>") :]
    return head[: head.index("collapse-body")]


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("heading, name, _render", HEADS)
def test_every_panel_head_carries_a_button_for_its_own_panel(heading: str, name: str, _render: str):
    """The handler named in the markup is what makes the button re-read THIS panel: four buttons
    pointing at one handler would be four buttons doing the same thing."""
    head = head_of(heading)

    assert f'onclick="{name}()"' in head, f"the {heading} panel's button"
    assert "↻ Refresh" in head, "named for what it does, beside the panel's own title"
    # The head is the fold's toggle, so the button's wrapper has to stop the click on its way past.
    assert 'onclick="event.stopPropagation()"' in head, "or the click also folds the panel"


@pytest.mark.parametrize("_heading, name, _render", HEADS)
def test_each_handler_is_reachable_from_the_markup(_heading: str, name: str, _render: str):
    """``onclick="…"`` resolves on ``window``: a handler that is not exported is a button that does
    nothing at all, and nothing else in this file would say so."""
    assert f"window.{name} = {name};" in LOG_JS.read_text(encoding="utf-8")


def test_the_loop_panel_keeps_its_clock_when_it_is_folded_and_loses_its_button():
    """Folding the Loop hides the button that re-reads it — what it re-reads is off screen — but
    the countdown stays, because it is the reason to look at the panel at all.

    The rule that decides this hides every direct child of ``.settings-actions`` that is not marked
    ``keep-visible``, so the clock rides in a wrapper that is.
    """
    head = head_of("The Loop")

    assert 'class="lg-clock-block keep-visible"' in head
    button = head[head.index('<button class="ghost small" onclick="refreshLoop()"') :]
    assert "keep-visible" not in button[: button.index("</button>")]


@pytest.mark.parametrize("_heading, name, _render", PANELS)
def test_a_panel_refresh_never_reloads_the_page(_heading: str, name: str, _render: str):
    """The Account card's ↻ is the whole page (``loadAll``); a panel's is NOT — that is the point of
    having four of them. It must not drag a broker read or a chart redraw in with it."""
    body = handler(name)

    assert "loadAll(" not in body
    assert "renderSession(" not in body, "the chart under the reader is not redrawn"


@pytest.mark.parametrize("_heading, name, render", PANELS)
def test_a_panel_refresh_redraws_only_its_own_panel(_heading: str, name: str, render: str):
    """One render per handler: re-rendering a panel whose source was not read would be redrawing
    stale state over fresh."""
    body = handler(name)
    if name in DELEGATED:
        body += handler(DELEGATED[name])
    others = [r for *_x, r in PANELS if r != render]

    assert render in body
    for other in others:
        assert other not in body, f"{name} must not redraw with {other}"


def test_the_chart_button_redraws_the_chart_and_nothing_else_on_the_page():
    """The chart's ↻ is the one panel refresh that redraws a chart — its own, which is what it is
    for: the session grows while the page is open, and the reader watching a position work wants
    the bar that just closed without waiting out the poll.

    What it must still not do is re-read the whole page (``loadAll``), which would drag the broker
    and every table in with it — that is the Account card's button. The dataset is read with it
    because that is where the zone the bar labels are written in comes from.
    """
    body = handler("refreshChart")

    assert "loadAll(" not in body
    assert "renderSession(" in body, "the bars and the read-outs drawn over them"
    assert "readDataset()" in body, "and the dataset the bar labels' zone comes from"
    for other in ("renderTicks()", "renderOrders()", "renderTrades()", "renderAccount()"):
        assert other not in body, f"the chart's ↻ must not redraw {other}"


def test_the_ticks_reader_keeps_the_day_the_reader_was_looking_at():
    """A refresh from a past day must not jump the page forward to the newest one — the same trap
    the whole-page ↻ already documents, and the reason this reader takes the day from the state
    rather than from the URL."""
    body = handler("reloadDayRecords")

    assert "state.log && state.log.day" in body
    assert "?day=" in body


# ---------------------------------------------------------------------------
# the behaviour: what each button actually reads
# ---------------------------------------------------------------------------
HARNESS = r"""
// ---- fakes: record every read and every render ---------------------------
const calls = { api: [], renders: [], fails: [] };
const replies = {
  "/api/v1/log?day=2026-09-22": { day: "2026-09-22", ticks: [], today: "2026-09-22" },
  "/api/v1/trades?day=2026-09-22": { trades: [] },
  "/api/v1/positions": { positions: [] },
  "/api/v1/accounts": { accounts: [] },
  "/api/v1/orders": { open: [], resting: [] },
};

// A path mapped to "fail" answers with a rejection instead, for the failure cases.
function api(path) {
  calls.api.push(path);
  const reply = replies[path];
  if (reply === "fail") return Promise.reject(new Error("the server said no"));
  return Promise.resolve(reply === undefined ? {} : reply);
}
function fail(message) { calls.fails.push(message); }
// The real one; faked so the harness can see it asked and keep this about the reads.
function loadStatus() { calls.renders.push("loadStatus"); return Promise.resolve(); }
function renderTicks() { calls.renders.push("ticks"); }
function renderAccount() { calls.renders.push("account"); }
function renderOrders() { calls.renders.push("orders"); }
function renderTrades() { calls.renders.push("trades"); }
// The chart's two collaborators, faked the same way — and the DAY is recorded rather than
// dropped: passing the day the reader is on is the whole reason this handler is not ``loadAll``.
function readDataset() { calls.renders.push("dataset"); return Promise.resolve({}); }
function renderSession(day) { calls.renders.push("session:" + day); }

const state = { log: { day: "2026-09-22" }, trades: null, positions: null, accounts: null,
                orders: null };

function reset() { calls.api = []; calls.renders = []; calls.fails = []; }

(async function () {
  const out = {};

  reset();
  await refreshLoop();
  out.loop = { api: calls.api, renders: calls.renders };

  reset();
  await refreshPositions();
  out.positions = { api: calls.api, renders: calls.renders, accounts: state.accounts };

  reset();
  await refreshOrders();
  out.orders = { api: calls.api, renders: calls.renders };

  reset();
  await refreshTrades();
  out.trades = { api: calls.api, renders: calls.renders, stored: state.trades };

  // A read that fails: reported, and the panel it belongs to is NOT redrawn.
  reset();
  replies["/api/v1/trades?day=2026-09-22"] = "fail";
  await refreshTrades();
  out.tradesFailed = { renders: calls.renders, fails: calls.fails };

  reset();
  replies["/api/v1/orders"] = "fail";
  await refreshPositions();
  out.positionsFailed = { renders: calls.renders, fails: calls.fails };

  reset();
  await refreshChart();
  out.chart = { renders: calls.renders, api: calls.api };

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory) -> dict:
    """Run the four handlers for real, with recording fakes, and hand back what they did."""
    script = tmp_path_factory.mktemp("panel-refresh") / "panels.js"
    script.write_text(refresh_block() + "\n" + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "panel refresh harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_loop_button_reads_the_loop_and_the_day_it_is_showing(behaviour):
    """The lease (which the clock is written from), the switch, and the day's records — in that
    order, because the clock is redrawn before the table under it is re-rendered."""
    assert behaviour["loop"]["renders"] == ["loadStatus", "ticks"]
    assert behaviour["loop"]["api"] == ["/api/v1/log?day=2026-09-22"]


def test_the_positions_button_asks_the_broker_and_nothing_of_the_loop(behaviour):
    """It is the page's one broker read, and the accounts come with it: one reply carries what the
    boxes above the figures say as well, so asking twice would be two answers to one question."""
    assert behaviour["positions"]["renders"] == ["account"]
    assert behaviour["positions"]["api"] == [
        "/api/v1/positions", "/api/v1/accounts", "/api/v1/orders"
    ]
    assert behaviour["positions"]["accounts"] == {"accounts": []}, "stored, not just rendered"


def test_the_orders_button_reads_the_loops_records_and_redraws_the_orders(behaviour):
    """The orders come out of the loop's own log, so there is no broker read in it at all."""
    assert behaviour["orders"]["renders"] == ["orders"]
    assert behaviour["orders"]["api"] == ["/api/v1/log?day=2026-09-22"]


def test_the_trades_button_reads_the_closed_round_trips(behaviour):
    """...and only the DAY's, like the table and the account's pane they feed: a round trip from
    another session is not this one's, and the panel's ↻ must not pull one in."""
    assert behaviour["trades"]["renders"] == ["trades"]
    assert behaviour["trades"]["api"] == ["/api/v1/trades?day=2026-09-22"]
    assert behaviour["trades"]["stored"] == {"trades": []}


def test_the_chart_button_re_reads_the_dataset_and_redraws_THE_DAY_ON_SCREEN(behaviour):
    """Both halves in one: the dataset first (so the bar labels are stamped in the right zone),
    then the session for the day the reader picked — from the state, never from a fresh read, for
    the same reason the ticks reader takes it from there.

    Nothing else is asked of the server: the dataset and the bars are read by the two collaborators
    this harness fakes, and the chart's ↻ reaches no other endpoint.
    """
    assert behaviour["chart"]["renders"] == ["dataset", "session:2026-09-22"]
    assert behaviour["chart"]["api"] == []


def test_a_failed_read_is_reported_and_leaves_the_panel_alone(behaviour):
    """An emptied table would say "nothing has been closed", which is the one thing a refresh must
    never invent: the failure goes to the page's error strip and the rows stay on screen."""
    assert behaviour["tradesFailed"]["fails"] == ["could not read the trades: the server said no"]
    assert behaviour["tradesFailed"]["renders"] == []

    assert behaviour["positionsFailed"]["fails"] == [
        "could not read the account: the server said no"
    ]
    assert behaviour["positionsFailed"]["renders"] == []
