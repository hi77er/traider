"""The "The Loop" panel: the countdown, the gates, and what the last tick did.

The panel exists to answer the two questions a quiet bot raises — "is it going to do anything,
and when?" and "if not, what is it stuck behind?" — from files the loop already writes. So the
things worth pinning are not the markup: they are the COUNTDOWN's arithmetic (its scale changes
by three orders of magnitude with the bar size) and the agreement between the pipeline the page
renders and the gates the loop actually walks.

That agreement is checked three ways at once, because it is a cross-language invariant and each
half can drift on its own:

* ``TICK_STAGES`` in the orchestrator — the pipeline as data, next to the code that walks it;
* the ``stage=`` each ``finish(...)`` in ``tick()`` stamps, in the ORDER the code reaches them;
* the ``GATES`` list the page renders, and the verdict-to-gate fallback for records written
  before stages existed.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOG_JS = ROOT / "src" / "web" / "static" / "log.js"
LOG_HTML = ROOT / "src" / "web" / "templates" / "log.html"
ORCHESTRATOR = ROOT / "src" / "scheduler" / "orchestrator.py"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the countdown"
)


# ---------------------------------------------------------------------------
# the pipeline: one list, three places, checked against each other
# ---------------------------------------------------------------------------
def _tick_finish_calls() -> list:
    """Every ``finish(...)`` inside ``tick()``, in the order the source reaches them."""
    tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    tick = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "tick"
    )
    calls = [
        node for node in ast.walk(tick)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "finish"
    ]
    return sorted(calls, key=lambda node: node.lineno)


def _literal(call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.literal_eval(keyword.value)
    return None


def _declared_stages() -> list:
    """``TICK_STAGES``, read out of the orchestrator without importing it."""
    src = ORCHESTRATOR.read_text(encoding="utf-8")
    match = re.search(r"^TICK_STAGES = \((.*?)^\)", src, re.S | re.M)
    assert match, "TICK_STAGES is gone from the orchestrator"
    return re.findall(r'"([a-z]+)"', match.group(1))


def _js_constants() -> dict:
    """The two tables the page renders from, lifted out of ``log.js`` verbatim."""
    src = LOG_JS.read_text(encoding="utf-8")
    gates = re.search(r"const GATES = \[(.*?)\n  \];", src, re.S)
    fallback = re.search(r"const STAGE_BY_ACTION = \{(.*?)\};", src, re.S)
    assert gates and fallback, "the gate tables are no longer in log.js where the tests look"
    return {
        "gates": re.findall(r'id: "([a-z]+)"', gates.group(1)),
        "actions": re.findall(r"([a-z]+):", fallback.group(1)),
    }


def test_every_finish_call_names_a_gate():
    """A verdict alone is not enough: ``refused`` covers six different gates, and "which one" is
    the question that follows every quiet tick."""
    unnamed = [call.lineno for call in _tick_finish_calls() if not _literal(call, "stage")]
    assert not unnamed, f"finish() without a stage at line(s) {unnamed}"


def test_the_gates_the_code_walks_are_the_gates_the_constant_names():
    """In order, and the order is the point: a tick that stopped at ``clock`` never reached
    ``sync``, and a reader is owed that distinction rather than a list of what ran."""
    seen = []
    for call in _tick_finish_calls():
        stage = _literal(call, "stage")
        if stage not in seen:
            seen.append(stage)

    assert seen == _declared_stages(), "the pipeline constant no longer matches the code's order"


def test_the_page_renders_that_pipeline_in_that_order():
    assert _js_constants()["gates"] == _declared_stages()


def test_the_fallback_covers_every_verdict_a_tick_can_end_with():
    """Records written before ``stage`` existed (and hand-built ones) are placed in the pipeline
    from their verdict. An action the map does not know would render a pipeline with no current
    step, which is the one thing this card must not do."""
    actions = {_literal(call, "") for call in _tick_finish_calls()}
    actions = {action for action in actions if action}
    assert actions <= set(_js_constants()["actions"]), (
        f"the page cannot place {sorted(actions - set(_js_constants()['actions']))} in the pipeline"
    )


def test_the_panel_sits_under_the_account_and_carries_the_day_inside_it():
    """One panel for the PROCESS: when it next wakes, the gates it walks, and what it did on the
    last one. Everything under it is read from the loop's own files, so it is one panel rather
    than three — and it stays directly under the account it is about."""
    html = LOG_HTML.read_text(encoding="utf-8")
    assert html.index("Account") < html.index("The Loop")
    assert html.index("The Loop") < html.index("Positions and working orders")
    for element in ("lg-countdown", "lg-next-when", "lg-gates"):
        assert f'id="{element}"' in html, element
    # The "trading is armed but no loop is running" line that used to sit in the Account card is
    # gone with the rest of the Trading status block: whether a process is honouring the claim is
    # what the countdown below says, in the same words, one panel down.
    assert 'id="lg-loop-warning"' not in html

    # The day's ticks are a SECTION of it, under the gates, rather than a card of their own —
    # and the section heading still carries the day being shown.
    gates = html.index("The gates the last tick walked")
    did = html.index("What the loop did")
    assert gates < did < html.index('id="lg-ticks"')
    assert "What the loop did <span id=\"lg-day\"" in html
    assert html.index('id="lg-ticks"') < html.index("Positions and working orders")


def test_the_two_sections_under_the_strip_explain_themselves_or_are_gone():
    """Asked for removal: the paragraph under the gates, and the one under the ticks header.

    Both were prose about the panel's own shape — what a tick walks, why a row can be absent,
    which file is the heartbeat — read once and skipped forever after, and both pushed the thing
    the reader came for further down. The roadmap says the order of the gates by looking like an
    order, so the paragraph explaining that order has nothing left to explain, and the empty
    lines the tables print ("nothing was decided on this day") still say why a table has no rows.
    """
    html = LOG_HTML.read_text(encoding="utf-8")
    loop = html[html.index('id="loop-body"') : html.index('id="positions-body"')]

    assert loop.count('class="muted note"') == 0, "the section's own prose is gone"
    assert "eight possible causes" not in html
    assert "the heartbeat is per strategy" not in html
    assert "latest.json" not in loop, "including the file named in the second one"
    # What must NOT go with them: the tables' own empty lines, which are output rather than
    # description — the one thing that says why there is nothing to read.
    assert "nothing was decided on this day" in LOG_JS.read_text(encoding="utf-8")


def test_the_panel_folds_away_without_folding_away_the_answer():
    """Collapsible because the day's ticks live in it: the head keeps the COUNTDOWN, so folding it
    hides the tables rather than the one number the panel exists for.

    The same mechanism as the dashboard's panels — a ``.card-head`` that toggles a
    ``.collapse-body``, with `.keep-visible` marking what survives the collapse — rather than a
    second convention for the same gesture.
    """
    html = LOG_HTML.read_text(encoding="utf-8")

    assert 'class="card collapsible"' in html
    assert 'onclick="toggleCard(event)"' in html
    assert 'class="collapse-body" id="loop-body"' in html

    head = html.index('<h2>The Loop</h2>')
    body = html.index('id="loop-body"')
    countdown = html.index('id="lg-countdown"')
    assert head < countdown < body, "the clock is in the HEAD, so it survives the collapse"
    # The clock rides in a `keep-visible` WRAPPER, which is what the collapse rule spares — the
    # panel's own ↻ is a sibling of it and folds away, because what it re-reads is off screen. See
    # tests/test_web/test_panel_refresh.py for that half.
    assert 'class="lg-clock-block keep-visible"' in html
    assert html.index('class="lg-clock-block keep-visible"') < countdown, "and the clock is in it"
    assert 'class="lg-clock-label"' in html, "the digits are labelled with what they count to"
    assert 'id="lg-next-when"' in html, "and the sentence under it"
    # Everything that folds away is the tables: the gates and the day's ticks.
    assert html.index('id="lg-gates"') > body and html.index('id="lg-ticks"') > body


def test_nothing_else_on_the_page_claims_the_same_toggle():
    """One panel, one id per element: a duplicate id is invisible until it is not, and a body with
    two owners folds under whichever button was pressed first.

    Every collapsible card on the page carries its own head and its own toggle button, and both
    call the SAME handler — which is why the count below is per panel rather than one: the head
    covers the whole row, the button is what a keyboard reaches.
    """
    html = LOG_HTML.read_text(encoding="utf-8")
    bodies = ("chart-body", "loop-body", "positions-body", "orders-body", "trades-body",
              "lg-gainers-body", "lg-smallcaps-body")
    for element in bodies:
        assert html.count(f'id="{element}"') == 1, element
    assert html.count('class="card collapsible"') == len(bodies), "one per foldable panel"
    assert html.count('onclick="toggleCard(event)"') == len(bodies) * 2, (
        "the head and its button both toggle it"
    )


# ---------------------------------------------------------------------------
# the countdown
# ---------------------------------------------------------------------------
# The real block, from the timer's own constants down to the loop-state renderer. It brings the
# page's own `$`, `esc`, `empty` and `setIfChanged` with it — the point is to exercise those too,
# so the harness fakes the BROWSER (elements, clock, timers) and nothing else. `state` is declared
# above the marker in the real file, which is why the harness supplies it.
START_MARKER = "const COUNTDOWN_MS"
END_MARKER = "\n  function renderLoopState()"

HARNESS = r"""
// ---- fakes: the document, the clock, and the timers ------------------------
const els = {};
function make(id) { els[id] = { id: id, innerHTML: "" }; return els[id]; }
function text(id) { return els[id].innerHTML; }

global.document = {
  hidden: false,
  getElementById: function (id) { return els[id] === undefined ? null : els[id]; },
};

// Timers are CAPTURED, never waited on: only one may run, and clearing it must release it.
let activeTimer = null;
global.setInterval = function (cb, ms) { activeTimer = { cb: cb, ms: ms }; return activeTimer; };
global.clearInterval = function (t) { if (activeTimer === t) activeTimer = null; };

const state = { loop: null };
// The page declares this above the marker, next to `params` — the only helper the block expects
// to find already there.
const $ = (id) => document.getElementById(id);

let NOW = Date.parse("2026-09-19T21:00:00+00:00");
Date.now = () => NOW;

make("lg-countdown");
make("lg-next-when");
make("lg-gates");

const out = { formats: {}, countdown: {}, naming: {} };

// 1. The face at every scale, from a 1-minute bar to a weekly one. Always hours:minutes:seconds,
// so the seconds always move — which is what shows the clock is alive, and why the separators do
// not blink as well.
out.formats.seconds = clockText(42 * 1000);
out.formats.minutes = clockText((17 * 60 + 5) * 1000);
out.formats.hours = clockText((15 * 3600 + 41 * 60) * 1000);
out.formats.days = clockText((3 * 86400 + 4 * 3600) * 1000);
out.formats.markup = /[<>]/.test(clockText(1000));

// 2. Tick it: the number is LIVE, and it is a local timer rather than a poll.
syncCountdown(new Date(NOW + 90 * 1000).toISOString(), "running");
out.countdown.first = text("lg-countdown");
out.countdown.interval = activeTimer ? activeTimer.ms : null;
NOW += 1000;
activeTimer.cb();
out.countdown.afterOneSecond = text("lg-countdown");
out.countdown.when = text("lg-next-when");
NOW += 120 * 1000;
activeTimer.cb();
out.countdown.pastTheBoundary = text("lg-countdown");
out.countdown.pastTheBoundaryWhen = text("lg-next-when");

// 3. Nothing scheduled, and the two reasons for that are NOT the same state.
stopCountdown();
syncCountdown(null, "stopped");
out.naming.stopped = { clock: text("lg-countdown"), when: text("lg-next-when") };
out.naming.stoppedTimer = activeTimer === null;

syncCountdown(null, "overdue");
out.naming.overdue = text("lg-next-when");

// 4. Running, but the first tick has not committed to a boundary yet.
syncCountdown(null, "running");
out.naming.starting = { clock: text("lg-countdown"), when: text("lg-next-when") };

// 5. A boundary it cannot parse is the same as no boundary, not a zero countdown.
syncCountdown("not a timestamp", "stopped");
out.naming.unparseable = text("lg-countdown");

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def countdown(tmp_path_factory) -> dict:
    src = LOG_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    block = src[start:src.index(END_MARKER, start)]
    script = tmp_path_factory.mktemp("countdown") / "countdown.js"
    script.write_text(block + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "countdown harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_face_reads_as_a_clock_at_every_scale_a_bar_can_have(countdown):
    """Hours, minutes and seconds, always all three — so the seconds always move, whatever the bar
    size. A countdown that moves only when the minutes do is one you cannot tell from a stopped
    clock."""
    assert countdown["formats"] == {
        "seconds": "00:00:42",
        "minutes": "00:17:05",
        "hours": "15:41:00",
        # Total hours rather than a day count: `76:00:00` reads as a timer, while a `3d 4h` face
        # would have to give up the seconds to stay short.
        "days": "76:00:00",
        # Plain text: the separators do not blink any more, so there is nothing to mark up. One
        # moving part is a clock; a blinking separator AND ticking seconds is a power light.
        "markup": False,
    }


def test_the_countdown_is_a_local_clock_and_it_moves(countdown):
    """No request per second: the boundary comes from the loop, the arithmetic is the page's —
    and the seconds are what show it is alive."""
    assert countdown["countdown"]["first"] == "00:01:30"
    assert countdown["countdown"]["interval"] == 1000
    assert countdown["countdown"]["afterOneSecond"] == "00:01:29"


def test_only_one_thing_on_the_face_moves(countdown):
    """Either the dots blink or the seconds tick — not both. The seconds win because they also say
    HOW LONG, and a blinking mark beside moving digits reads as a power light rather than a clock."""
    assert "lg-clock-dots" not in LOG_JS.read_text(encoding="utf-8")
    assert "blink" not in LOG_JS.read_text(encoding="utf-8").split("trading_switch")[0]
    assert countdown["formats"]["markup"] is False, "the face is text, not marked-up digits"


def test_past_the_boundary_the_clock_stands_at_zero(countdown):
    """The loop woke for the bar it committed to and is inside the tick, so the clock has nothing
    left to count — and the line under it says what is happening instead of inventing a negative
    countdown."""
    assert countdown["countdown"]["pastTheBoundary"] == "00:00:00"
    assert countdown["countdown"]["pastTheBoundaryWhen"].startswith("The bar closed")


def test_the_line_under_the_clock_is_a_sentence(countdown):
    """It reads as a sentence under a number now: a capital to start it, and a full stop, because
    two clauses in a row are read as one line rather than as two labels."""
    when = countdown["countdown"]["when"]
    assert when.startswith("The bar closes ")
    assert " · The loop wakes" in when, "the second sentence is capitalised too"
    assert when.endswith(".")


def test_no_loop_and_a_starting_loop_do_not_read_the_same(countdown):
    """A loop holds the lease BEFORE it has ticked, and the boundary is written by that first
    tick. Calling that "nothing scheduled" is the same lie as the warning this page carries."""
    naming = countdown["naming"]
    assert naming["stopped"]["clock"] == "—"
    assert "no loop is running" in naming["stopped"]["when"]
    assert naming["starting"]["clock"] == "starting…"
    # Case-insensitively: the sentence is capitalised now, and the CLOCK is what carries the
    # lowercase word.
    assert "starting" in naming["starting"]["when"].lower()
    assert naming["starting"]["when"] != naming["stopped"]["when"]


def test_a_dead_claim_is_named_rather_than_counted_down_to(countdown):
    """An ``overdue`` lease names a boundary a process committed to before it died. Counting
    down to that would promise a tick nothing is going to make."""
    assert "gone" in countdown["naming"]["overdue"]


def test_nothing_scheduled_means_no_timer_at_all(countdown):
    """The page already stops its poll when there is nothing to watch; a one-second timer
    redrawing a dash is the same waste with a smaller period."""
    assert countdown["naming"]["stoppedTimer"] is True


def test_a_boundary_that_cannot_be_read_is_treated_as_no_boundary(countdown):
    """Something that is not a timestamp must not become a countdown: "NaN" or a negative
    number both read as a schedule, which is worse than saying there is none."""
    assert countdown["naming"]["unparseable"] == "—"
