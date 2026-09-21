"""Where things sit on the dashboard, and what has to be true of the arrangement.

Structural assertions on the template, which this project has no browser runner to do better
for. They are worth having because the two failures they catch are silent: a panel that drifts
into the wrong column is a panel nobody finds, and a duplicated id (from moving one) leaves two
elements answering to the same name while every test that reads the file keeps passing.

The move that prompted this: the Trading panel left the right-hand column for a place under the
Backtest panel it belongs to.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
LOG_JS = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")


def _column(needle: str) -> str:
    """``left`` or ``right`` — which of the two columns ``needle`` appears in."""
    left = HTML.index('<section class="left">')
    aside = HTML.index('<aside class="right">')
    end = HTML.index("</aside>")
    at = HTML.index(needle)
    assert left <= at, f"{needle} is before the split"
    if at < aside:
        return "left"
    return "right" if at < end else "outside"


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
    assert "notesCell(tick.notes)" in LOG_JS, "the tick row renders the notes"
    assert '"notes"' in LOG_JS, "and the table declares the column"

    # Sliced to the NEXT function rather than to a character count. It used to be a fixed 700-char
    # window, which ran past the end of ``notesCell`` into ``money()`` beside it — so the em-dash
    # assertion below was satisfied by ``money``'s ``return "—"`` and would have kept passing if
    # the empty-notes branch were deleted. Adding a helper between the two is what exposed it.
    start = LOG_JS.index("function notesCell")
    helper = LOG_JS[start:LOG_JS.index("function money(", start)]
    assert "⚠" in helper, "marked, so it is not mistaken for the tick's reason"
    assert 'class="warn"' in helper
    assert "<td>—</td>" in helper, "an em dash when there is nothing to say, not a blank cell"
    assert ".lg-table td.warn" in CSS, "the marker has its own colour (amber, not red)"


def test_the_live_panel_says_when_arming_has_nothing_to_run_it():
    """Two facts, one switch — so the panel has to join them.

    Arming writes ``trading.json``; nothing ticks until a process runs the loop, and this
    dashboard never starts one (that is the two-process split). The switch and the loop are two
    different facts, so without a line joining the two, flipping the switch looks like it did
    nothing — which is exactly how it was read.

    A visible LINE rather than a ``title``: the embedded browser renders no native tooltip.
    """
    assert "Trading is armed, but no loop is running" in APP_JS
    assert "python -m src.main" in APP_JS, "and it says what to start"

    at = APP_JS.index("Trading is armed, but no loop is running")
    assert "lines.push(" in APP_JS[at - 40:at], "rendered as a visible line, not a tooltip"

    # The guard immediately above it has to require BOTH facts. Either one alone makes the note
    # a lie in the other case: with the switch off there is nothing armed to warn about, and
    # while the loop IS running there is nothing to alarm about.
    guard = APP_JS[APP_JS.rindex("if (", 0, at):at].split("\n", 1)[0]
    assert "armed" in guard, f"gated on the switch being ON: {guard}"
    assert 'loop.state === "stopped"' in guard and 'loop.state === "never"' in guard, (
        f"and on the loop not running: {guard}"
    )


def test_the_head_controls_obey_the_hidden_attribute():
    """``display`` from a class beats the browser's ``[hidden] { display: none }``, so both
    controls the JS hides with that attribute were visible whether or not they applied.

    The same trap is already re-asserted for ``#rp-delete`` and ``#bt-report``; these two were
    missed because the card holding them was hidden *with* them. Found by opening the page.
    """
    assert "#trading-off-btn[hidden]" in CSS
    assert "#trading-flatten-btn[hidden]" in CSS


# ---------------------------------------------------------------------------
# the Trading panel moved
# ---------------------------------------------------------------------------
def test_the_panels_are_named_uniquely_and_the_account_panel_is_trading():
    """The panel that reports the account and the loop is titled "Trading" — and only one is.

    There is no second card any more: the switch and the account share this one, so the name
    has to be unique for the opposite reason it used to be. (The element ids stay ``live-*``:
    they are internal, and renaming them would churn the CSS and the JS for nothing anyone can
    see.)
    """
    headings = re.findall(r"<h2>([^<]*)</h2>", HTML)

    assert headings.count("Trading") == 1, f"exactly one panel is 'Trading': {headings}"
    assert "Live" not in headings, "the account panel is not called Live any more"
    assert "Trading switch" not in headings, "and the switch is not a panel of its own"


def test_both_panels_report_the_account_being_traded_and_not_the_others_faults():
    """A 401 on the account this run never touches must not read like a fault in this run.

    Both panels answer about the account the trading MODE points at. The switch panel still
    lists the other account's POSITIONS — something open over there is real whatever mode we
    are in, and it is what stops the bot being armed on top of it — but not its unreadability.
    That verdict is not hidden: it stays on the credential badge, in the Account popup, and on
    the credential badge and the Account popup — and no account's WORTH is shown anywhere but
    the traded one's (see the log page's accounts panel).
    """
    assert "const traded = String((exec && exec.env)" in APP_JS
    assert "account.known === false && (!traded || env === traded)" in APP_JS
    assert "row.env === accounts.env || row.known" not in APP_JS, (
        "the account panel must not warn about the other environment any more"
    )


def test_the_trading_panel_sits_in_the_strategy_bar():
    """It moved into the bar's second row, in the slot the signals list used to have.

    The bar is what the dashboard is about — the strategy, what it says now, what it will do and
    whether it is trading — so the panel that reports the trading belongs beside the rules it
    acts on rather than in a column under the chart. Two equal slots, so the move did not quietly
    demote one of them: the trading panel leads, the signals and rules share the other.
    """
    bar = HTML.index('id="strategy-bar"')
    row = HTML.index('class="strategy-rules-row"')
    trading = HTML.index('id="strategy-trading"')
    live = HTML.index('id="live-card"')
    signals = HTML.index('id="signals"')

    assert bar < row < trading < live < signals, (
        "the trading slot is the first child of the row, the panel inside it, then the signals"
    )
    assert HTML.count('class="strategy-slot"') == 2, "two slots, both taking half the row"


def test_the_signals_and_the_rules_are_one_section_with_the_signals_first():
    """Asked for as the signals above the rules, merged into a single section.

    They answer one question between them — what is it saying now, and what will it do about it —
    and as two panels side by side they read as two unrelated ones. So one slot holds both, the
    signals first and a hairline between them, and the BOX belongs to the slot: leaving a border
    on each half would have been the merge in name only.
    """
    trading = HTML.index('id="strategy-trading"')
    slot = HTML.index('class="strategy-slot"', trading)
    signals = HTML.index('id="signals"')
    rules = HTML.index('class="strategy-rules-section"')

    assert trading < slot < signals < rules, "one slot, signals above the rules inside it"
    assert ".strategy-slot {" in CSS, "the section box is the slot's"
    assert ".strategy-signals,\n.strategy-rules-section {" not in CSS, (
        "and the per-panel boxes are gone — one section, not two boxes in one"
    )
    assert ".strategy-rules-section {\n  margin-top: 12px;\n  padding-top: 12px;\n" in CSS, (
        "a hairline is what separates the two halves"
    )


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


def test_the_live_panel_is_not_hidden_with_the_chart():
    """It reports the loop, the accounts and the exchange — none of which need a stored
    dataset. Inside ``#dashboard`` it would vanish with the chart when there is none, which is
    exactly when someone is looking for why nothing is happening.

    The bar sits above the whole two-column split, so the panel is now outside both the section
    that hides with the chart and the column that holds it.
    """
    dashboard = HTML.index('id="dashboard"')
    dashboard_end = HTML.index("</section>", dashboard)
    live = HTML.index('id="live-card"')

    assert not dashboard < live < dashboard_end, "not inside the section that hides with the chart"
    assert live < HTML.index('class="split"'), "and not in the column that holds it"


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


def test_the_trading_panel_does_not_repeat_the_strategy_or_the_switch():
    """Asked for: the "GPRO - 5m - 60d · paper" line goes (the bar above and the Mode box below
    both say it), and no text in the panel says "trading is ON/OFF" any more.

    The switch is a BOX now, so the words went: a state printed in a sentence as well as a box
    reads as two facts. The tick line's reason is the same case — a tick whose verdict came from
    the switch is reported as "off", and "off — trading is OFF" was that twice.
    """
    state = _function_of(APP_JS, "renderLive")
    panel = _function_of(APP_JS, "renderTradingPanel")

    assert "loop.strategy" not in state and "loop.env" not in state, (
        "the strategy bar names the strategy; the Mode box names the account"
    )
    assert 'fromSwitch = loop.last_tick ? loop.last_tick.stage === "switch"' in state
    assert 'loop.last_reason && !fromSwitch' in state
    assert "trading is OFF" not in panel and "trading is ON" not in panel
    assert 'TraiderSwitch.tradeTile(state.tradingPayload, "toggleTrading()")' in APP_JS, (
        "the box is what says it — built by the shared module, so the log page's reads the same"
    )


def test_every_id_in_the_page_appears_once():
    """A moved block is a duplicated block until the original goes, and two elements with one
    id fail silently: the browser resolves the first and the other is unreachable."""
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    assert duplicates == [], f"duplicated id(s): {duplicates}"


def test_the_panel_link_is_in_the_header_next_to_the_refresh():
    """Asked for at the top of the panel, to the right of the reload button — and no longer
    buried in the footnote it used to be at the bottom of."""
    head = HTML[HTML.index('id="live-card"'):HTML.index('id="live-body"')]
    actions = head[head.index('class="settings-actions"'):]

    assert "/log" in actions, "the link belongs in the header's action group"
    assert actions.index('id="live-refresh"') < actions.index("/log"), "to the right of ↻"
    # The reload used to be the glyph alone, which says nothing about what it does. Pressing it
    # re-reads the loop's own records AND the broker, so the label names the action.
    refresh = actions[actions.index('id="live-refresh"'):]
    assert "↻ Refresh" in refresh[:refresh.index("</button>")]

    body = HTML[HTML.index('id="live-body"'):HTML.index("</section>", HTML.index('id="live-body"'))]
    assert "/log" not in body, "and only there, not in both places"


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

    # The FOUR panels that carry header controls and fold on their own are all on that pattern,
    # so the rule reaches every one of them: Configuration, Rules, Risk Management and Backtest.
    # The Trading panel is deliberately not among them: it has no toggle, and the strategy bar's
    # own toggle folds it together with the signals and the rules below the strategy line.
    for card in ("config-card", "rules-card", "risk-card", "backtest"):
        at = HTML.index(f'id="{card}"')
        tag = HTML[HTML.rindex("<", 0, at):HTML.index(">", at)]
        assert "card collapsible" in tag, f"{card} must opt into the collapse pattern"


def test_one_toggle_folds_the_whole_strategy_row():
    """Asked for as a toggle to the left of "Strategy", folding everything below that line.

    One toggle per subject: the trading panel, the signals and the rules are three views of the
    same strategy, so they fold together. The Trading panel's own toggle went with it — two
    buttons hiding the same panel from different places is how a section ends up unfindable.
    """
    # To the LEFT of the label it folds from: same row, before the title.
    toggle = HTML.index('id="strategy-collapse"')
    assert HTML.index("strategy-bar-main") < toggle < HTML.index(">Strategy<"), (
        "the button sits at the head of the Strategy row"
    )
    assert "toggleStrategyBody()" in HTML and "toggleStrategyBody" in APP_JS

    row = HTML.index('id="strategy-body"')
    assert row > toggle, "and the row it folds is the one below"
    assert not APP_JS.count("function toggleLivePanel"), "the panel's own toggle is gone"
    assert 'id="toggle-live"' not in HTML

    # The row is a flex container, so `[hidden]` alone would leave it on screen — the trap this
    # project has hit before, and the reason every other hidden-by-attribute element here has its
    # own rule.
    assert ".strategy-rules-row[hidden] { display: none; }" in CSS


def test_folding_the_row_stops_the_broker_poll_and_unfolding_restarts_it():
    """A folded row has no reader, which is the same case as the collapsed panel this replaced:
    /api/v1/orders asks Alpaca, so the poll must not keep running behind it.

    Unfolding goes through ``refreshLiveNow()``, which is the full read plus the restart of the
    slow cadence — the same three effects this test pinned inline before they had a name.
    """
    body = APP_JS[APP_JS.index("function toggleStrategyBody("):]
    body = body[:body.index("\n}")]

    assert "stopLivePoll()" in body, "folding it stops the polling"
    assert "refreshLiveNow()" in body, "opening it asks the broker, now and on a cadence"

    helper = APP_JS[APP_JS.index("async function refreshLiveNow("):]
    helper = helper[:helper.index("\n}")]
    assert "loadLive(true)" in helper and "scheduleLivePoll()" in helper
    assert "_livePoll.lastSlow = Date.now()" in helper, "and the cadence restarts from this read"

    gate = APP_JS[APP_JS.index("function livePanelVisible("):]
    gate = gate[:gate.index("\n}")]
    assert "row.hidden" in gate, "and the gate the poll runs behind knows about the row"


def test_the_trading_panel_keeps_its_state_and_log_reachable():
    """Reading whether trading is on, and getting to the log, must not need anything opened first.

    The panel has no toggle now, so its head is simply permanent: the reload and the link to the
    log sit in it, plus the one control that is NOT the header's.

    The panel's own "Turn trading off" button was asked for removal: the header's master switch
    does that from outside every lock, and two buttons for one action made the panel look like it
    had two owners. The flatten button stays — it closes what is open as well, which the switch
    does not, and it is the only way to reach that once the switch is off.
    """
    head = HTML[HTML.index('id="live-card"'):HTML.index('id="live-body"')]
    actions = head[head.index('class="settings-actions"'):]
    actions = actions[:actions.index("</div>")]

    for present in ('id="live-refresh"', 'href="/log"'):
        assert present in actions, f"{present} belongs in the panel head"
    assert 'id="trading-off-btn"' not in HTML, "the master switch is the only way to stop trading"
    assert 'id="trading-off-btn"' not in APP_JS, "and nothing writes to an id that is gone"
    at = actions.index('id="trading-flatten-btn"')
    tag = actions[actions.rindex("<", 0, at):actions.index(">", at)]
    assert "hidden" in tag, "shown only while there is something to flatten"
    # `keep-visible` existed to keep a control on screen while its body was shut. Nothing shuts
    # now, so the class would be a decoration that means nothing.
    assert "keep-visible" not in head, "the head has no collapse to survive"


def test_the_armed_row_is_not_edged_in_green():
    """Asked for removal: the green edge on the trading section while trading is on.

    It marked the whole slot for a state the panel's own Trading box already reports — and that
    box pulses — so half the dashboard was outlined to say one word twice. ``body.trading-on``
    survives as the lock's hook (the switch still sets it); nothing styles the slot by it.
    """
    assert "body.trading-on" not in CSS
    assert ".strategy-slot > #live-card" in CSS, "the slot itself is still styled"


def test_the_loop_chip_is_gone_from_the_trading_panel():
    """Asked for removal: the little chip beside "Trading" that said "stopped".

    It reported the loop's state as one more badge between the panel's title and its buttons — a
    third reading of two facts the panel already carries, and "stopped" is what it said in the
    state the panel is in almost all of the time. Gone from the markup, the script and the
    stylesheet: an id nobody renders and a rule for an element that does not exist are how a
    removal ends up half done.
    """
    assert 'id="live-chip"' not in HTML
    assert "live-chip" not in APP_JS, "and nothing writes to the id that is gone"
    assert "#live-chip" not in CSS


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
    # Sliced to the next card in the document. This used to be bounded by ``id="live-card"``,
    # which now sits ABOVE the backtest in the file: a backwards slice is empty, and the count
    # below would then have failed for the wrong reason — nothing found, rather than two found.
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
# a stale dashboard says so
# ---------------------------------------------------------------------------
def test_a_missing_endpoint_explains_that_the_server_is_old():
    """The page is served from disk on every request while the routes are frozen at import, so
    a dashboard left running across a change answers 404 for endpoints the page was built
    against. It happened, and "404: Not Found" said nothing about why."""
    for source in (APP_JS, LOG_JS):
        assert "res.status === 404" in source
        assert "running older code" in source
