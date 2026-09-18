"""Three kinds of missing bar, three different things — and they must look it.

A bar the grid expects can be absent for reasons that are not interchangeable:

* **fetchable** — the provider HAS it and the file does not. A real gap; "Fetch bars"
  adds it, and until it is added a run is not trustworthy.
* **no_trades** — the provider traded that day but has no bar for that interval, so
  nobody traded in it. Nothing to fetch, ever. A thin symbol is mostly made of these:
  IMCC traded in 14 of 78 five-minute slots on 2026-09-01, so 64 of its "missing" bars
  were intervals the market simply never traded.
* **unconfirmed** — a normal slot of a session we hold bars for, and no provider check
  has answered for it yet.

Conflating them is what made an illiquid symbol report 959 missing bars, keep the tick
re-downloading 60 days of data, and hold the backtest disabled — for bars that exist
nowhere. The panel has to show all three (an operator looking at holes in a chart needs
to know which holes are theirs to fix) while never dressing one as another.

Pinned by RUNNING the shipped ``DELTA_CHIP`` table in node — a grep for the class names
would pass just as happily on a table that maps all three reasons to the same chip.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the delta chips"
)


def _chip_source() -> str:
    """The real ``DELTA_CHIP`` table plus the real ``deltaChip()``, verbatim."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("const DELTA_CHIP = {")
    end = src.index("\n}", src.index("function deltaChip(reason) {")) + len("\n}")
    return src[start:end] + "\n"


def _run(expr: str):
    """Evaluate ``expr`` against the shipped chip table and return its JSON."""
    program = _chip_source() + "\nconsole.log(JSON.stringify(" + expr + "));\n"
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_every_reason_gets_its_own_look():
    classes = _run(
        "[deltaChip('fetchable').cls, deltaChip('no_trades').cls,"
        " deltaChip('unconfirmed').cls]"
    )
    assert len(set(classes)) == 3, classes


def test_an_interval_nobody_traded_is_not_dressed_up_as_a_gap_to_fetch():
    """The two must not read as the same thing, in class OR in tooltip."""
    fetchable = _run("deltaChip('fetchable')")
    no_trades = _run("deltaChip('no_trades')")
    assert fetchable["cls"] != no_trades["cls"]
    assert fetchable["title"] != no_trades["title"]
    assert "Fetch bars will add it" in fetchable["title"]
    assert "nothing to fetch" in no_trades["title"]


def test_the_untraded_chip_has_a_style_of_its_own():
    """A class nothing styles is a class the operator never sees."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".chip.no-trades {" in css
    # ...and it is not the same rule as the "inferred, unconfirmed" chip.
    assert ".chip.inferred {" in css


def test_an_unknown_reason_is_treated_as_the_cautious_one():
    """A payload from a newer server must not look harmless by default."""
    assert _run("deltaChip('something-new').cls") == _run("deltaChip('unconfirmed').cls")


def test_the_reasons_match_the_data_layer():
    """The browser and the server name the same three states, or nothing lines up."""
    from src.data import delta as delta_mod

    assert sorted(_run("Object.keys(DELTA_CHIP)")) == sorted(
        [
            delta_mod.REASON_FETCHABLE,
            delta_mod.REASON_NO_TRADES,
            delta_mod.REASON_UNCONFIRMED,
        ]
    )


def test_a_synced_dataset_still_lists_the_intervals_nobody_traded():
    """The "all synced" branch must not swallow the list.

    ``synced`` is true when nothing is FETCHABLE, and an interval nobody traded is not
    fetchable — so gating that early branch on ``s.synced`` alone would hide the very bars
    this panel exists to explain. It has to consider the untraded count too.
    """
    assert "if (s.synced && !noTrades) {" in APP_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The "hide all bars missing due to no liquidity" toggle
# ---------------------------------------------------------------------------
def _toggle_source() -> str:
    """The shipped ``showsNoTradeToggle`` and ``visibleDeltaBars``, verbatim."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function showsNoTradeToggle(s) {")
    end = src.index("\n}", src.index("function visibleDeltaBars(s, hideNoTrades) {"))
    return src[start : end + len("\n}")] + "\n"


def _run_toggle(expr: str):
    program = _toggle_source() + "\nconsole.log(JSON.stringify(" + expr + "));\n"
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


PAYLOAD = (
    "{ missing_bars: ["
    " {reason: 'no_trades', date: '2026-09-01', time: '13:30'},"
    " {reason: 'fetchable', date: '2026-09-18', time: '10:25'},"
    " {reason: 'unconfirmed', date: '2026-09-18', time: '11:00'},"
    " {reason: 'no_trades', date: '2026-09-02', time: '11:05'}],"
    " missing_bars_total: 2, no_trades_bars_total: 2 }"
)


def test_the_toggle_is_offered_only_when_such_bars_exist():
    """A dataset with none draws no toggle — the control would have nothing to do."""
    assert _run_toggle("[showsNoTradeToggle({no_trades_bars_total: 0})]") == [False]
    assert _run_toggle("[showsNoTradeToggle({})]") == [False]
    assert _run_toggle("[showsNoTradeToggle(null)]") == [False]
    assert _run_toggle("[showsNoTradeToggle(" + PAYLOAD + ")]") == [True]


def test_hiding_untraded_bars_is_on_by_default():
    """They are listed for accounting, not as a to-do, and on a thin symbol they bury the
    handful of bars that can actually be fetched — so the panel starts with them hidden.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert "hideNoTrades: true," in src
    assert "hideNoTrades: false," not in src
    # ...and the checkbox reflects that state rather than being hardcoded off.
    assert '${state.hideNoTrades ? " checked" : ""}' in src


def test_hiding_removes_exactly_the_untraded_bars():
    """...and never a bar that can still be fetched."""
    got = _run_toggle(
        "[visibleDeltaBars(" + PAYLOAD + ", true).map(b => b.reason),"
        " visibleDeltaBars(" + PAYLOAD + ", false).map(b => b.reason)]"
    )
    assert got == [["fetchable", "unconfirmed"], ["no_trades", "fetchable", "unconfirmed", "no_trades"]]


def test_the_toggle_is_drawn_above_the_list_of_missing_bars():
    """It is the control for that list, so it belongs in a row directly above it.

    It used to share the panel header with the fetch button, which made a long label and a
    button fight for the same line.
    """
    src = APP_JS.read_text(encoding="utf-8")
    row_at = src.index('<div class="delta-toggle-row">')
    list_at = src.index("dayBlocks ||")
    assert row_at < list_at, "the toggle row must come before the list it filters"
    # ...and it is no longer composed into the header action row at all.
    calls = re.findall(r"setDeltaAction\((.*?)\);", src, re.S)
    assert calls, "expected the delta action calls"
    assert not any("deltaToggleHtml" in c for c in calls)


def test_the_toggle_is_labelled_and_styled():
    src = APP_JS.read_text(encoding="utf-8")
    # The label is built from the count and the current state — see the two tests below.
    assert "noTradeToggleLabel(noTrades, state.hideNoTrades)" in src
    assert "function noTradeToggleLabel(n, hidden) {" in src
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".delta-toggle-row {" in css
    assert ".delta-toggle {" in css
    # The switch look is no longer scoped to the settings panel's `.field`, or the
    # toggle in the delta panel would render as a bare checkbox.
    assert 'input[type="checkbox"].switch {' in css
    assert '.field input[type="checkbox"].switch {' not in css


# ---------------------------------------------------------------------------
# The one line of prose above the list
# ---------------------------------------------------------------------------
def _note_source() -> str:
    """The shipped ``deltaNoteText``, verbatim."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function deltaNoteText(s, shown, hidden) {")
    return src[start : src.index("\n}", src.index("return note;", start)) + len("\n}")] + "\n"


def _notes() -> list:
    """What the line says in every state the panel can be in."""
    program = (
        _note_source()
        + "\nconst mixed = { missing_bars_total: 2, no_trades_bars_total: 2, missing_bars: ["
        + " {reason: 'fetchable'}, {reason: 'no_trades'}, {reason: 'no_trades'},"
        + " {reason: 'unconfirmed'}] };"
        + "\nconst untraded = { missing_bars_total: 0, no_trades_bars_total: 2,"
        + " missing_bars: [{reason: 'no_trades'}, {reason: 'no_trades'}] };"
        + "\nconst fetchOnly = { missing_bars_total: 2, no_trades_bars_total: 0,"
        + " missing_bars: [{reason: 'fetchable'}, {reason: 'fetchable'}] };"
        + "\nconst unchecked = { missing_bars_total: 2, no_trades_bars_total: 0,"
        + " missing_bars: [{reason: 'unconfirmed'}, {reason: 'unconfirmed'}] };"
        + "\nconsole.log(JSON.stringify(["
        + " deltaNoteText(mixed, mixed.missing_bars, 0),"
        + " deltaNoteText(mixed, mixed.missing_bars.filter(b => b.reason !== 'no_trades'), 2),"
        + " deltaNoteText(untraded, untraded.missing_bars, 0),"
        + " deltaNoteText(fetchOnly, fetchOnly.missing_bars, 0),"
        + " deltaNoteText(unchecked, unchecked.missing_bars, 0),"
        + " deltaNoteText({ missing_bars: [], missing_bars_total: 0 }, [], 0),"
        + "]));"
    )
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_explanation_is_one_short_line():
    """One line, at most two short sentences — never a paragraph.

    It replaced two paragraphs, one of which explained the chip vocabulary at length on
    every render. The panel is read at a glance; the chips say the rest.
    """
    for note in _notes():
        assert len(note) <= 130, note
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", note.strip()) if s]
        assert len(sentences) <= 2, note


def test_the_explanation_says_what_this_list_actually_needs():
    """Each state gets its own line — and a list with nothing to explain says nothing."""
    mixed, mixed_hidden, untraded, fetch_only, unchecked, empty = _notes()
    assert "Fetch bars adds the rest" in mixed
    assert "hidden" in mixed_hidden
    assert "provider has these" in fetch_only
    assert "Not checked" in unchecked
    # A list of nothing but untraded chips gets no line: the struck chips and the
    # "✓ Nothing to fetch" headline already say it. The dropped sentence was
    # "Nothing traded in these intervals, so there is nothing to fetch."
    assert untraded == ""
    assert empty == "", "an empty list has nothing to explain"


def test_the_removed_untraded_line_is_gone_for_good():
    src = APP_JS.read_text(encoding="utf-8")
    assert "nothing traded in these intervals" not in src.lower()
    assert "there is nothing to fetch" not in src.lower()


# ---------------------------------------------------------------------------

# The label of the toggle
# ---------------------------------------------------------------------------


def _function_source(name: str) -> str:
    """One top-level ``function name(...) { ... }`` from the shipped app.js."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}", start) + len("\n}")] + "\n"


def _labels() -> list:
    """The toggle's label for a spread of counts, hidden and shown."""
    program = (
        _function_source("barWord")
        + _function_source("noTradeToggleLabel")
        + "\nconst cases = [[0, false], [1, false], [959, false], [1234, false],"
        + " [0, true], [1, true], [959, true], [1234, true]];"
        + "\nconsole.log(JSON.stringify(cases.map((c) => noTradeToggleLabel(c[0], c[1]))));"
    )
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_label_counts_the_bars_it_hides():
    """The count rides in the label — IMCC hides 959, and that number is the point.

    "Hide all bars missing due to no liquidity" reads like a maybe; "Hide all 959 bars"
    tells you at a glance how much of the panel is struck out, and why the few remaining
    chips are the only ones worth pressing Fetch for.
    """
    zero, one, thin, many, *rest = _labels()
    assert thin == "Hide all 959 bars missing due to no liquidity"
    assert many == "Hide all 1,234 bars missing due to no liquidity"
    # Four digits read as 1,234 — the separator is pinned, not left to the host locale.
    assert "," in many
    # And it still reads as English at the boundaries.
    assert one == "Hide all 1 bar missing due to no liquidity"
    assert zero == "Hide all 0 bars missing due to no liquidity"


def test_the_label_flips_to_show_once_they_are_hidden():
    """The control describes the way back: hidden bars offer to come back.

    A checkbox that still says "Hide all 959" while it is holding 959 bars back is
    describing the state it is already in — so the verb follows the state.
    """
    _, _, _, _, zero, one, thin, many = _labels()
    assert thin == "Show all 959 bars missing due to no liquidity"
    assert many == "Show all 1,234 bars missing due to no liquidity"
    assert one == "Show all 1 bar missing due to no liquidity"
    assert zero == "Show all 0 bars missing due to no liquidity"
    # Hide -> Show is the ONLY difference between the two states.
    hidden = [thin, many, one, zero]
    shown = [t.replace("Show", "Hide") for t in hidden]
    assert all(s.startswith("Hide all ") for s in shown)


def test_the_verb_comes_from_the_checked_state():
    """…and the state is the checkbox's own, so label and tick can never disagree."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "noTradeToggleLabel(noTrades, state.hideNoTrades)" in src
    assert "${state.hideNoTrades ? \" checked\" : \"\"}" in src


def test_the_hidden_count_is_the_same_number_as_the_label():
    """The message above the list counted the capped list; the label counted the total.

    IMCC: the list holds 500 rows (``_MAX_MISSING_BARS``) but 959 intervals were
    untraded, so the panel said "500 hidden" under a toggle that said "all 959". Both
    now read the payload's ``no_trades_bars_total``.
    """
    src = APP_JS.read_text(encoding="utf-8")
    helper = _function_source("hiddenNoTradeCount")
    assert "s.no_trades_bars_total" in helper
    assert "hideNoTrades ?" in helper
    # Nothing derives the hidden count from the list any more.
    assert "bars.length - shown.length" not in src
    assert "listed.length - shown.length" not in src
    # And both the note and the "all hidden" line are fed the same number.
    assert "deltaNoteText(s, shown, hiddenNoTrades)" in src
    assert "All ${hiddenNoTrades.toLocaleString()} intervals" in src


def test_hidden_no_trades_is_the_total_not_the_listed_rows():
    """Behavioural: 959 untraded of 964 gaps, 500 listed — the label and the line agree.

    This is the discrepancy the user hit: the toggle said 959 and the panel said 500.
    """
    program = (
        _function_source("barWord")
        + _function_source("hiddenNoTradeCount")
        + _function_source("noTradeToggleLabel")
        + "\nconst s = { missing_bars_total: 5, no_trades_bars_total: 959,"
        + " missing_bars: Array.from({length: 500}, () => ({reason: 'no_trades'})),"
        + " no_trades_bars_truncated: true };"
        + "\nconst hidden = hiddenNoTradeCount(s, true);"
        + "\nconsole.log(JSON.stringify({ shown: hiddenNoTradeCount(s, false), hidden,"
        + " label: noTradeToggleLabel(s.no_trades_bars_total, true) }));"
    )
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    got = json.loads(out.stdout)
    assert got["shown"] == 0, "nothing is hidden while the toggle is off"
    assert got["hidden"] == 959, "the total, not the 500 rows the list can hold"
    assert "959" in got["label"], "label and message must name the same number"
    assert "500" not in got["label"]



def test_the_count_is_the_same_one_the_panel_reports():
    """The label is fed from the payload's own total, so it cannot drift out of step."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "const noTrades = s.no_trades_bars_total || 0;" in src
    assert "deltaToggleHtml(noTrades)" in src
    # …and the toggle is only drawn when there is something to hide.
    assert "showsNoTradeToggle(s)" in src
