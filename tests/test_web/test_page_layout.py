"""Where things sit on the Strategy lab page, and what has to be true of the arrangement.

Structural assertions on the template, which this project has no browser runner to do better
for. They are worth having because the two failures they catch are silent: a panel that drifts
into the wrong column is a panel nobody finds, and a duplicated id (from moving one) leaves two
elements answering to the same name while every test that reads the file keeps passing.

The subject is the strategy bar. Its row has been rearranged twice — the signals joined the rules
they read, and the Trading panel that used to fill the left slot left the page altogether for the
Session monitor — so where the halves sit is worth pinning rather than remembering.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
LOG_JS = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")


def _function_of(source: str, name: str) -> str:
    """The body of ``name``, balanced out to its closing brace."""
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces after {name} in the source")


def test_the_tick_table_renders_a_bar_s_notes_with_a_warning_mark():
    """A note exists to be SEEN, so the tick table is where it has to land.

    ``src.data.quality`` reports an odd bar without refusing it, which means nothing else in
    the system will ever mention it — if this cell is missing, the note is written to disk and
    read by nobody. The ⚠ and its own colour are what keep it distinguishable at a glance from
    the ordinary reason text beside it in the same row.
    """
    assert "notesCell(tick.notes, tick.at)" in LOG_JS, "the tick row renders the notes"
    assert '"notes"' in LOG_JS, "and the table declares the column"

    # Sliced to the NEXT function rather than to a character count. It used to be a fixed 700-char
    # window, which ran past the end of ``notesCell`` into ``money()`` beside it — so the em-dash
    # assertion below was satisfied by ``money``'s ``return "—"`` and would have kept passing if
    # the empty-notes branch were deleted. Adding a helper between the two is what exposed it.
    start = LOG_JS.index("function notesCell")
    helper = LOG_JS[start:LOG_JS.index("function money(", start)]
    assert "⚠" in helper, "marked, so it is not mistaken for the tick's reason"
    # Two classes, two jobs: ``warn`` is its colour, ``prose`` is what lets the sentence be opened
    # instead of running off the panel (see test_log_page's prose-cell test). It goes through the
    # shared helper rather than writing its own cell, so the notes open on the same click as every
    # other sentence in these tables.
    assert 'proseCell(`⚠ ${list.join("; ")}`' in helper
    assert '<td>—</td>' in helper, "an em dash when there is nothing to say, not a blank cell"
    assert ".lg-table td.warn" in CSS, "the marker has its own colour (amber, not red)"


# ---------------------------------------------------------------------------
# the strategy bar's row
# ---------------------------------------------------------------------------
def test_the_signals_sit_under_the_rules_they_read():
    """Asked for as the signals under the Rules section — the reading directly beneath the rules
    that produce it, in the left half of the row.

    A signal nobody can trace back to a rule is a curiosity, and the rule it came from must not be
    on the other side of the row. So the signals are the SECOND child of the slot that holds the
    rules, and the risk settings keep the other half. The Trading panel used to fill this slot
    beside them; it and the switch left for the Session monitor, so what is left is one half about
    what the strategy would do and one about what it would risk.
    """
    rules = HTML.index('id="strategy-rules-slot"')
    signals = HTML.index('id="signals"')
    other = HTML.index('class="strategy-slot"', signals)

    assert rules < signals < other, "in the rules' half of the row, not the risk settings' half"
    # One element, one slot: a signals section left OUTSIDE the slot (after the row, say) would
    # still satisfy the index order above and read as a third column.
    assert '<section id="signals" class="strategy-signals"></section>' in HTML, (
        "an empty host, filled whole by renderSignals — nothing else in it to move by hand"
    )
    assert HTML.count('id="signals"') == 1
    assert HTML[signals:other].count("</section>") == 2, "its own close, then the slot's"


def test_the_slot_separates_its_two_halves_with_a_hairline_and_owns_the_box():
    """One box per slot, a hairline between the things inside it.

    The slot is the box: a section that brought its own border would be a box inside a box. What
    separates the things inside it is a rule along the top of the one BELOW — and the first child
    of either slot has to drop it, or the rule hangs a few pixels under the slot's own edge with
    nothing above it to separate from.
    """
    assert ".strategy-slot {" in CSS, "the section box is the slot's"
    rule = CSS[CSS.index(".strategy-signals,\n.strategy-rules-section {") :]
    rule = rule[: rule.index("}")]
    assert "border-top: 1px solid" in rule and "margin-top: 12px" in rule
    assert "background" not in rule and "border: 1px" not in rule, (
        "the box belongs to the slot; this rule is the joint"
    )
    first = CSS[CSS.index(".strategy-slot > .strategy-signals:first-child") :]
    first = first[: first.index("}")]
    assert "border-top: 0;" in first and "margin-top: 0;" in first
    assert "padding-top: 0;" in first


def test_the_buy_and_the_sell_rules_are_separated_in_the_summary():
    """The summary lists the BUY rules first and the SELL ones under them: two lists in one
    section, so the joint between them needs a gap — with only the line height, the first SELL
    rule reads as one more BUY rule.

    The gap belongs at the JOINT and nowhere else. A margin between every pair of lines is just a
    looser list, which is what the line height is for, so the renderer marks the side on each line
    and the stylesheet spaces the one adjacent pair. Both halves are asserted, because either one
    alone can be undone by adding a blanket rule to the other.
    """
    assert "line.dataset.side = r.side" in APP_JS, "the line has to say which side it is"

    joint = '.strategy-rule-line[data-side="BUY"] + .strategy-rule-line[data-side="SELL"]'
    assert joint in CSS, "the space goes between the two groups"
    block = CSS[CSS.index(joint):]
    assert "margin-top: 10px" in block[:block.index("}")], "and it is a real gap"

    assert ".strategy-rule-line + .strategy-rule-line" not in CSS, "not between every pair"
    assert ".strategy-rule-line { display: block; margin" not in CSS, (
        "and not baked into the line itself, which would space every rule from every other"
    )


def test_the_two_signal_toggles_live_in_the_price_history_panel():
    """Asked for: the toggles move out of the signals panel and into Price History, because
    between them they decide what is DRAWN on that chart — the markers, and whether the
    unexecuted ones are drawn at all. A control belongs on the thing it changes.

    Checked in both directions, because deleting them from the signals render is the easy half:
    the ids have to be in the chart card, and the signals panel must not emit them any more (two
    elements answering to one id is exactly the drift this file exists to catch).
    """
    chart = HTML.index('id="chart"')
    canvas = HTML.index('id="chart-canvas"')

    for control in ("signals-toggle", "exec-signals-toggle"):
        at = HTML.index(f'id="{control}"')
        assert chart < at < canvas, f"{control} belongs in the Price History panel"
        assert f'id="{control}"' not in APP_JS, (
            "the signals renderer must not emit a second copy of it"
        )
        assert f'$("{control}")' in APP_JS, "the state is still synced onto it"

    assert "syncSignalToggles()" in APP_JS, "synced from the render state, not re-rendered"
    assert ".chart-head .signal-toggles {" in CSS, "on one row in there, not a stacked column"


def test_the_strategy_combo_box_says_what_each_strategy_reads():
    """Asked for: the "GPRO · 5m" chip beside the combo box goes, and instead each OPTION
    carries the history window and the bar size.

    The chip described the ACTIVE strategy — the one case where the values are already on screen
    in the panels below — while the choice in the list is really a choice between two datasets,
    and the name is only the author's shorthand for them. So the label is built from each
    strategy's own configuration, read through the same two formatters the header uses.
    """
    assert 'id="strategy-context"' not in HTML, "the chip is gone"
    assert "strategy-context" not in APP_JS and ".strategy-chip {" not in CSS

    body = _function_of(APP_JS, "strategyOptionLabel")
    assert "HISTORICAL_LOOKBACK" in body and "HISTORICAL_BAR_SIZE" in body
    assert "periodLabel(" in body and "barSizeLabel(" in body, "the same wording as the header"
    assert "strategyOptionLabel(n," in APP_JS, "every option is labelled, not just the active one"


def test_every_id_in_the_page_appears_once():
    """A moved block is a duplicated block until the original goes, and two elements with one
    id fail silently: the browser resolves the first and the other is unreachable."""
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    assert duplicates == [], f"duplicated id(s): {duplicates}"


def test_a_collapsed_panel_hides_every_control_but_its_toggle():
    """A shut panel is a title and a +, not a toolbar.

    Driven by the body's own ``hidden`` instead of a class something has to remember to set:
    ``expandCard`` and ``ensureRulesOpen`` open a panel from code, and a panel whose controls
    stayed pressable while its body was shut would be the bug this replaces.
    """
    collapsed = ".card.collapsible:has(.collapse-body[hidden])"

    assert f"{collapsed} .settings-actions > *:not(.keep-visible)" in CSS, "the act not the group"
    assert f"{collapsed} .bt-actions > *:not(.keep-visible)" in CSS
    block = CSS[CSS.index(collapsed):]
    assert "display: none" in block[:block.index("}")], "hidden, not merely moved"

    # The FOUR cards that carry header controls and fold on their own are all on that pattern,
    # so the rule reaches every one of them: Configuration, Rules, Risk Management and Backtest.
    # The strategy bar folds a whole row instead of a card, and does it with its own toggle, so
    # it is deliberately not one of these.
    for card in ("config-card", "rules-card", "risk-card", "backtest"):
        at = HTML.index(f'id="{card}"')
        tag = HTML[HTML.rindex("<", 0, at):HTML.index(">", at)]
        assert "card collapsible" in tag, f"{card} must opt into the collapse pattern"


def test_one_toggle_folds_the_whole_strategy_row():
    """Asked for as a toggle to the left of "Strategy", folding everything below that line.

    One toggle per subject: the rules, the signals they produce and the risk they are traded
    under are three views of the same strategy, so they fold together. The Trading panel that
    used to sit in this row had its own toggle once; that went when the row's toggle arrived —
    two buttons hiding the same panel from different places is how a section ends up unfindable —
    and the panel itself has since left the page for the Session monitor.
    """
    # To the LEFT of the label it folds from: same row, before the title.
    toggle = HTML.index('id="strategy-collapse"')
    assert HTML.index("strategy-bar-main") < toggle < HTML.index(">Strategy<"), (
        "the button sits at the head of the Strategy row"
    )
    assert "toggleStrategyBody()" in HTML and "toggleStrategyBody" in APP_JS

    row = HTML.index('id="strategy-body"')
    assert row > toggle, "and the row it folds is the one below"
    assert not APP_JS.count("function toggleLivePanel"), "no second toggle hides the row"
    assert 'id="toggle-live"' not in HTML

    # The row is a flex container, so `[hidden]` alone would leave it on screen — the trap this
    # project has hit before, and the reason every other hidden-by-attribute element here has its
    # own rule.
    assert ".strategy-rules-row[hidden] { display: none; }" in CSS


def test_the_backtest_keeps_its_run_and_report_buttons_when_collapsed():
    """Run and Open report act on the stored run, which is readable with the panel shut; the
    body it hides is the result, not the controls."""
    card = HTML[HTML.index('id="backtest"'):HTML.index('id="bt-panel"')]
    actions = card[card.index('class="bt-actions"'):]

    for kept in ('id="bt-run"', 'id="bt-report"'):
        at = actions.index(kept)
        tag = actions[actions.rindex("<", 0, at):actions.index(">", at)]
        assert "keep-visible" in tag, f"{kept} must stay visible when the panel is collapsed"


# ---------------------------------------------------------------------------
# the Backtest panel collapses
# ---------------------------------------------------------------------------
def test_the_backtest_panel_is_collapsible():
    card = HTML[HTML.index('id="backtest"'):HTML.index('id="bt-panel"')]

    assert 'class="card collapsible"' in card, "the card has to opt into the pattern"
    assert "card-head" in card and "toggleCollapse(this)" in card
    assert "card-toggle" in card, "the +/- label toggleCollapse updates"


def test_the_backtest_body_is_one_collapse_body():
    """``toggleCollapse`` finds the body with ``card.querySelector(".collapse-body")``, which
    takes the FIRST match — a second one inside the card would be toggled instead."""
    # Sliced to the next card in the document (the configuration card) rather than to a character
    # count. A bounds element that moves ABOVE the backtest makes the slice empty, and the count
    # below would then fail for the wrong reason — nothing found, rather than two found.
    card = HTML[HTML.index('id="backtest"'):HTML.index('id="config-card"')]
    assert card.count('class="collapse-body"') == 1


def test_the_backtest_toggle_starts_open():
    """Collapsible, not collapsed: it is the action area, and it opens expanded exactly as it
    did before the toggle existed."""
    card = HTML[HTML.index('id="backtest"'):HTML.index('id="bt-panel"')]
    assert '<span class="card-toggle">−</span>' in card


def test_the_backtest_actions_do_not_toggle_the_panel():
    """Pressing Run is not a request to fold the panel shut."""
    card = HTML[HTML.index('id="backtest"'):HTML.index('id="bt-panel"')]
    actions = card[card.index('class="bt-actions"'):]
    assert "event.stopPropagation()" in actions


def test_running_a_backtest_opens_the_panel():
    """A result that lands inside a shut panel looks like nothing happened."""
    body = APP_JS[APP_JS.index("async function runBacktest()"):]
    body = body[:body.index("\nfunction ")]
    assert 'expandCard($("backtest"))' in body


# ---------------------------------------------------------------------------
# a stale server says so
# ---------------------------------------------------------------------------
def test_a_missing_endpoint_explains_that_the_server_is_old():
    """The page is served from disk on every request while the routes are frozen at import, so a
    server left running across a change answers 404 for endpoints the page was built against. It
    happened, and "404: Not Found" said nothing about why.

    Both scripts say it, and both blame the SERVER rather than the page: it is the process that is
    running old code, and the reader is looking at it from a browser that has no idea which code
    it is talking to.
    """
    for source in (APP_JS, LOG_JS):
        assert "res.status === 404" in source
        assert "the server is running older code than" in source
