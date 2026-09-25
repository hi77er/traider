"""The gates as a ROADMAP: the shape says the order, the hover says the question.

A tick walks the gates in order and STOPS at the first that says no, so everything after it never
ran. Eight rows of three columns never said that — it drew a stopped tick and a complete one in
the same rectangle of text, and the only thing distinguishing them was the word "not reached"
somewhere down a list. A strip of chevrons pointing into each other says it by SHAPE: the step it
stopped at is coloured by its verdict, and the steps after it are hollow.

What each gate asks moved from a column into the step's hover label. The label is an ELEMENT, not
a `title` attribute — the embedded browser renders no native tooltip, so a title-only explanation
is one nobody ever sees (this project has already paid for that lesson once) — and the steps are
focusable, so the keyboard reaches the same text.

These run the shipped ``renderGates`` in node against a fake document, because the thing under
test is the MARKUP it builds: a source-level assertion would pass just as happily on a renderer
that is never called.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOG_JS = ROOT / "src" / "web" / "static" / "log.js"
CSS = ROOT / "src" / "web" / "static" / "style.css"

# The same span the countdown harness lifts: from the page's own constants down to the loop-state
# renderer, which brings `esc`, `empty`, `setIfChanged`, `mine`, `inPlayEnv`, the GATES table and
# `renderGates` itself with it.
START_MARKER = "const COUNTDOWN_MS"
END_MARKER = "\n  function renderLoopState()"

HARNESS = r"""
const els = {};
function make(id) { els[id] = { id: id, innerHTML: "" }; return els[id]; }
global.document = {
  hidden: false,
  getElementById: function (id) { return els[id] === undefined ? null : els[id]; },
};
const $ = (id) => document.getElementById(id);
const state = { loop: { env: "paper" }, accounts: null, log: null };

make("lg-gates");

function gatesFor(tick) {
  els["lg-gates"].innerHTML = "";
  renderGates(tick);
  return els["lg-gates"].innerHTML;
}

const decided = gatesFor({
  at: "2026-09-21T16:30:00+00:00", env: "paper", action: "decided", reason: "",
  stage: "decide", bar: "2026-09-21T16:25:00+00:00",
});
const closed = gatesFor({
  at: "2026-09-21T13:00:00+00:00", env: "paper", action: "closed",
  reason: "the exchange is closed", stage: "clock",
});
const noop = gatesFor({
  at: "2026-09-21T16:30:00+00:00", env: "paper", action: "noop",
  reason: "the bar was already decided", stage: "decide",
});
const otherAccount = gatesFor({
  at: "2026-09-21T16:30:00+00:00", env: "live", action: "decided", reason: "", stage: "decide",
});
const noTick = gatesFor(null);
const switched = gatesFor({
  at: "2026-09-21T16:30:00+00:00", env: "paper", action: "switched",
  reason: "instrument automation: GPRO has dropped out of the top 10 — AAA", stage: "instrument",
});

process.stdout.write(JSON.stringify({
  decided: decided, closed: closed, noop: noop, switched: switched,
  otherAccount: otherAccount, noTick: noTick,
}));
"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    src = LOG_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    block = src[start : src.index(END_MARKER, start)]
    script = tmp_path_factory.mktemp("gates") / "gates.js"
    script.write_text(block + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "gate roadmap harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the roadmap"
)


def test_the_gates_are_a_strip_of_steps_and_not_a_table(rendered):
    """The shape is the point: a table was what could not say "the tick stopped here"."""
    assert "<table" not in rendered["decided"]
    assert 'class="lg-roadmap"' in rendered["decided"]
    assert rendered["decided"].count('class="lg-step ') == 9, "one step per gate"
    # The old third column is gone with the table, and the questions are no longer columns.
    assert "what it asks" not in rendered["decided"]
    assert "<th>" not in rendered["decided"]


def test_every_step_carries_its_own_question_as_a_hover_label(rendered):
    """The question is what you want while looking at ONE step, so it travels with the step."""
    markup = rendered["decided"]
    for question in (
        "Is trading on",
        "Should the instrument be a different one",
        "Could an order be placed at all",
        "Is the exchange open",
        "Is the dataset synced to now",
        "Did the strategy decide, and act",
    ):
        assert question in markup, question
    assert markup.count('class="lg-step-help"') == 9
    assert markup.count('class="lg-step-asks"') == 9
    # Readable without a pointer: the steps take focus, so the label has to open on focus too.
    assert markup.count('tabindex="0"') == 9
    assert "<span class=\"lg-step-name\">decide</span>" in markup


def test_every_description_reads_as_a_sentence(rendered):
    """The label is prose — a question and its answer — so each starts with a capital.

    Held as an invariant over all eight rather than as a list of strings: the point is the SHAPE
    of the text, and a ninth gate added in lower case should fail here. The tick's own reason
    after the dash is quoted, not rewritten, and keeps the case the loop wrote it in.
    """
    markup = rendered["closed"]
    asks = re.findall(r'<span class="lg-step-asks">([^<]*)</span>', markup)
    answers = re.findall(r'<span class="lg-step-answer">([^<]*)</span>', markup)

    assert len(asks) == 9 and len(answers) == 9
    for text in asks + answers:
        assert text[0].isupper(), text
    assert "Passed" in answers
    assert "Not reached" in answers, "including the ones the tick never got to"
    assert "Closed — the exchange is closed" in answers, "the action, then the loop's words"


def test_the_description_is_smaller_than_the_step_it_explains():
    """A gloss on the name inside the chevron, not a second label beside it."""
    css = CSS.read_text(encoding="utf-8")

    help_rule = css[css.index(".lg-step-help {") :][:700]
    assert "font-size: 11px;" in help_rule
    step_label = css[css.index(".lg-step-body {") :][:700]
    assert "font-size: 12px;" in step_label, "the name it glosses, which is bigger"


def test_a_tick_that_decided_shows_a_full_green_strip_and_no_caption(rendered):
    """Everything passed, so there is nothing to explain: the colour IS the answer."""
    markup = rendered["decided"]
    assert markup.count("lg-step passed") == 8, "every gate before the last one"
    assert markup.count("lg-step good") == 1, "and the one it acted on"
    assert "unreached" not in markup
    assert "lg-roadmap-why" not in markup, "a caption would restate the strip"


def test_the_step_that_stopped_the_tick_is_the_only_coloured_one(rendered):
    """A closed exchange: switch, armed and execution passed, the clock said no, and the four
    after it never ran. That is the whole story of the tick, in one look."""
    markup = rendered["closed"]
    assert markup.count("lg-step passed") == 4
    assert markup.count("lg-step bad") == 1
    assert markup.count("lg-step unreached") == 4
    # The failed step is the fourth one, in the order the loop walks them.
    assert markup.index("lg-step bad") > markup.index("lg-step-name\">execution")
    assert markup.index("lg-step bad") < markup.index("lg-step-name\">sync")


def test_the_step_that_stopped_it_says_why_without_being_hovered(rendered):
    """Its verdict is the news on this panel — everything else is a gate the tick sailed through —
    so it is printed under the strip in the loop's own words rather than hidden in a label."""
    markup = rendered["closed"]
    assert 'class="lg-roadmap-why bad"' in markup
    assert "clock · closed — the exchange is closed" in markup


def test_a_switch_is_shown_as_something_the_tick_DID(rendered):
    """An instrument switch ends its tick at the gate it acted on, the way a decision ends one at
    ``decide`` — so the strip must not read it as a failure. Two gates passed, the instrument step
    is the green one, and the six after it never ran; the loop's own words go under the strip,
    because on this panel that IS the news."""
    markup = rendered["switched"]

    assert markup.count("lg-step passed") == 2, "switch and armed"
    assert markup.count("lg-step good") == 1
    assert "lg-step bad" not in markup, "a switch is not a refusal"
    assert markup.count("lg-step unreached") == 6
    assert markup.index("lg-step good") < markup.index('lg-step-name">execution'), \
        "the instrument step, in the order the loop walks them"
    assert 'class="lg-roadmap-why good"' in markup
    assert "instrument · switched — instrument automation" in markup


def test_the_hover_label_is_not_inside_the_clipped_shape(rendered):
    """The one thing the shape must not do to the label.

    ``clip-path`` clips everything INSIDE the element that carries it, so a label nested in the
    chevron is a label nobody can see — while its computed style cheerfully reports
    ``visibility: visible; opacity: 1``. That was this panel's first version: the roadmap looked
    right and hovering it did nothing. The shape is an inner span now, and the label is its
    sibling.
    """
    markup = rendered["decided"]
    css = CSS.read_text(encoding="utf-8")

    assert markup.count('class="lg-step-body"') == 9, "one shape per step"
    assert markup.count('class="lg-step-help"') == 9

    # Structurally: every tooltip's PARENT is the step itself, never the shape.
    parser = _StepTree()
    parser.feed(markup)
    assert len(parser.help_parents) == 9
    for parent in parser.help_parents:
        assert parent == "lg-step" or parent.startswith("lg-step "), (
            f"the label hangs off {parent!r}, not off the step"
        )

    # And in the stylesheet: the clip is on the shape, not on the step that holds the label.
    shape = css[css.index(".lg-step-body {") :][:700]
    assert "clip-path: polygon(" in shape
    step = css[css.index(".lg-step {") : css.index(".lg-step + .lg-step")]
    assert "clip-path" not in step, "the hover target must not be the clipped box"


class _StepTree(HTMLParser):
    """Records the class of each tooltip's parent — a DOM question, answered without a browser."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list = []
        self.help_parents: list = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if cls == "lg-step-help" and self.stack:
            self.help_parents.append(self.stack[-1])
        self.stack.append(cls)

    def handle_endtag(self, tag):  # noqa: D102 - HTMLParser hook
        if self.stack:
            self.stack.pop()


def test_the_steps_meet_instead_of_floating_apart(rendered):
    """The point of one step is the notch of the next: without the negative margin the strip is
    eight separate badges, which reads as a list rather than as a road."""
    css = CSS.read_text(encoding="utf-8")

    assert ".lg-step + .lg-step { margin-left: -11px; }" in css
    assert "gap: 4px" not in css[css.index(".lg-roadmap {") :][:200], (
        "a gap would hold the arrows apart again"
    )
    assert 'class="lg-step passed"' in rendered["decided"]


def test_the_strip_spans_the_panel_it_sits_in(rendered):
    """Every step takes an EQUAL share of the panel's width.

    The strip is the whole road a tick walked, and eight content-width chevrons bunched at the
    left of a wide panel read as a legend rather than as a road. Growing them equally is also
    what keeps the notches meeting: steps of different widths would put the point of one beside
    the notch of the next rather than inside it.
    """
    css = CSS.read_text(encoding="utf-8")

    step = css[css.index(".lg-step {") : css.index(".lg-step + .lg-step")]
    assert "flex: 1 1 0;" in step, "each step grows to its share of the panel"
    assert "min-width:" in step, "and stops shrinking where a label would not fit"
    assert "\n  width:" not in step, "a fixed width would be a strip that stops short again"

    body = css[css.index(".lg-step-body {") :][:700]
    assert "flex: 1;" in body, "the chevron fills the step it sits in"
    assert "justify-content: center;" in body, "and its label sits in the middle of it"

    # No leftover gap or alignment from the content-width strip: the negative margin is the only
    # thing between two steps.
    roadmap = css[css.index(".lg-roadmap {") :][:200]
    assert "gap:" not in roadmap
    assert 'class="lg-step passed"' in rendered["decided"]


def test_a_tick_that_simply_had_nothing_to_do_is_amber_not_red(rendered):
    """"Noop" is the loop working: the bar was already decided. Only a refusal or a closed market
    is a failure, and the colour has to keep the three apart."""
    markup = rendered["noop"]
    assert markup.count("lg-step muted") == 1
    assert "lg-step bad" not in markup
    assert 'lg-roadmap-why muted' in markup
    assert "the bar was already decided" in markup


def test_a_tick_for_the_OTHER_account_is_still_not_drawn(rendered):
    """The heartbeat is one file per strategy, so it can hold the other account's tick — and a
    pipeline read from a paper tick under a live header is the mixing this page refuses."""
    assert "lg-roadmap" not in rendered["otherAccount"]
    assert "nothing has ticked for the paper account yet" in rendered["otherAccount"]
    assert "live" in rendered["otherAccount"]


def test_no_tick_at_all_says_so(rendered):
    assert "nothing has ticked yet" in rendered["noTick"]
    assert "lg-roadmap" not in rendered["noTick"]
