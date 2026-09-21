"""What the two polling loops are allowed to do.

These are text-level assertions on the static files, which this project has no runner to do
better for. They are here anyway because the invariants they hold cost MONEY when they break,
and a browser check cannot be run in CI: everything else about the polls is verified by hand.

The one that matters is the gate. ``/api/v1/orders`` and ``/api/v1/clock`` are broker calls,
so a poll that keeps running behind a collapsed panel or in a backgrounded tab is a recurring
cost with no reader — and a backgrounded tab is the normal state of a dashboard someone opened
this morning. The other is the log page's: a past day cannot gain rows, so refreshing one is
pure cost and a page that churns under a reader's cursor.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
LOG_JS = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")
# The box builders, the switch's wording and the mode write are the SHARED module's: both pages show
# the same three trading boxes, so the assertions about what a box looks like belong there.
SWITCH_JS = (ROOT / "src" / "web" / "static" / "trading_switch.js").read_text(encoding="utf-8")
LOG_HTML = (ROOT / "src" / "web" / "templates" / "log.html").read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    """The body of ``name``, balanced out to its closing brace.

    Deliberately not "slice to the next ``function`` keyword": when this helper first cut the
    corner it sliced to ``\\nfunction ``, the next declaration in the file was ``async
    function loadAll``, and the slice therefore ran *through* it. One assertion then passed
    because of code in a different function — which a mutation check caught, not review.
    """
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


# ---------------------------------------------------------------------------
# the Trading panel
# ---------------------------------------------------------------------------
def test_the_live_poll_is_gated_on_the_panel_being_open_and_the_tab_being_visible():
    """The panel has no toggle of its own any more, so what can hide it is the strategy bar's
    toggle, which folds the row it lives in."""
    body = _function(APP_JS, "livePanelVisible")

    assert "row.hidden" in body, "a folded row must not poll"
    assert "document.hidden" in body, "a backgrounded tab must not poll"


def test_the_live_poll_reschedules_itself_rather_than_firing_on_a_clock():
    """A self-scheduling timeout cannot stack requests behind a slow broker; a setInterval
    fires on the wall clock whatever the last one did."""
    body = _function(APP_JS, "scheduleLivePoll")

    assert "stopLivePoll()" in body
    assert "setTimeout(runLivePoll" in body
    assert "setInterval" not in body


def test_the_poll_stops_when_the_tab_is_hidden_and_when_the_page_goes_away():
    assert "visibilitychange" in APP_JS and "handleLiveVisibility" in APP_JS
    assert 'addEventListener("pagehide", stopLivePoll)' in APP_JS


def _constant(source: str, name: str) -> int:
    import re

    match = re.search(rf"const {name} = (\d+);", source)
    assert match, f"{name} is gone"
    return int(match.group(1))


def test_the_broker_half_is_polled_more_slowly_than_the_file_half():
    """``/loop`` is files, ``/orders`` and ``/clock`` are broker round trips. One interval for
    both would mean paying the expensive one at the cheap one's rate.

    Asserted as a RELATIONSHIP rather than as the numbers: retuning either cadence is a
    judgement call, and a test that fails over it is a test someone deletes.
    """
    assert _constant(APP_JS, "LIVE_SLOW_MS") > _constant(APP_JS, "LIVE_POLL_MS")
    assert "LIVE_SLOW_MS" in _function(APP_JS, "runLivePoll"), \
        "the slow cadence has to be what decides includeOrders"


def test_a_failing_poll_slows_down_instead_of_hammering():
    body = _function(APP_JS, "runLivePoll")

    assert "LIVE_MAX_FAILURES" in body and "return" in body
    assert "LIVE_BACKOFF_MS" in body


def test_an_unchanged_panel_is_not_re_rendered():
    """Re-rendering identical markup every few seconds resets the blinking chip mid-blink and
    makes the relative ages flicker while they are being read."""
    assert "function setIfChanged(" in APP_JS
    assert 'setIfChanged($("live-state")' in APP_JS
    assert 'setIfChanged(host' in APP_JS, "the detail rows go through it too"


def test_only_the_newest_response_is_rendered():
    """A slow poll landing after a fresher one would show the panel going backwards."""
    body = _function(APP_JS, "loadLive")

    assert "_livePoll.token" in body
    assert body.count("_livePoll.token") >= 2, "taken at the start, checked before rendering"


def test_the_market_line_is_kept_across_the_fast_polls():
    """The session moves at 09:30 and 16:00 and nowhere else, so the fast poll does not fetch
    it — which only works if the last answer is remembered."""
    assert "state.liveMarket" in APP_JS
    assert "marketLine" in APP_JS


# ---------------------------------------------------------------------------
# the log page
# ---------------------------------------------------------------------------
def test_the_log_page_has_a_refresh_control():
    assert 'id="lg-refresh"' in LOG_HTML
    assert "refreshLog()" in LOG_HTML, "and it is wired to the page's own refresh"


def test_the_refresh_control_keeps_the_day_in_view():
    """``loadAll()`` with no day falls back to the server's newest, so wiring the button
    straight to it would have a click from a past day jump the page forward — which is exactly
    what it did in the browser before this existed."""
    assert "window.refreshLog" in LOG_JS
    assert "loadAll(state.log && state.log.day)" in LOG_JS, \
        "it has to pass the day being looked at, not let the server pick one"


def test_the_log_page_polls_only_while_today_is_showing():
    """The gate has to run BEFORE the timer is created.

    A guard that only lives inside the callback still schedules the work, and a timer whose
    callback always returns early is still a timer firing every twenty seconds — the comment
    would be true and the cost would not.
    """
    body = _function(LOG_JS, "schedulePoll")

    assert body.index("isToday()") < body.index("setTimeout("), "gated before scheduling"
    assert body.index("document.hidden") < body.index("setTimeout(")

    is_today = _function(LOG_JS, "isToday")
    assert "state.log.today" in is_today, "today comes from the server, not the browser clock"
    assert "state.log.day" in is_today


def test_the_live_poll_is_decided_before_the_timer_is_created():
    """Same property on the Trading panel: the visibility check is what prevents the poll, not
    a callback that happens to return early."""
    body = _function(APP_JS, "scheduleLivePoll")

    assert body.index("livePanelVisible()") < body.index("setTimeout(")


def test_the_log_poll_reschedules_itself():
    body = _function(LOG_JS, "schedulePoll")

    assert "setTimeout" in body and "setInterval" not in body


def test_choosing_a_day_is_what_decides_whether_the_log_polls():
    """Switching to a past day has to stop the poll, not leave the old one running."""
    assert "schedulePoll();" in _function(LOG_JS, "loadLog")


# ---------------------------------------------------------------------------
# the market line itself
# ---------------------------------------------------------------------------
def test_an_unreadable_clock_does_not_render_as_a_closed_market():
    """The exact lie the clock service is built to avoid, one layer up: a 401 is not a
    session, and telling an operator the market is shut when nobody could look is worse than
    saying nothing."""
    body = _function(APP_JS, "marketLine")

    assert "market.ok" in body, "the payload's own verdict has to be consulted"
    assert "unknown" in body
    assert body.index("market.ok") < body.index("market.is_open"), \
        "the unknown branch has to come first, or a failed read falls through to 'closed'"


def test_the_market_line_names_the_boundary_that_is_coming():
    """Closed means "opens at", open means "closes at" — the opposite one is history."""
    body = _function(APP_JS, "marketLine")

    assert "market.next_open" in body and "market.next_close" in body
    assert "is_open ? market.next_close : market.next_open" in body


def test_the_exchange_time_is_read_from_the_clock_not_converted_from_it():
    """Alpaca's clock carries the exchange's own offset, so the digits in it are New York's
    wall time. Reformatting through Date would silently show the reader's zone as the
    market's — on a machine seven hours ahead, 09:30 would read as 16:30."""
    body = _function(APP_JS, "exchangeClock")

    assert "ET" in body
    assert "toLocaleTimeString" in body, "the reader's own time is offered alongside"
    assert "Date" in body, "and it is what the comparison is made against"


# ---------------------------------------------------------------------------
# the account numbers
# ---------------------------------------------------------------------------
def test_the_panel_fetches_the_accounts_only_on_the_slow_poll():
    """/api/v1/accounts is a broker call, so it belongs with the orders and the clock rather
    than with the five-second file read."""
    body = _function(APP_JS, "loadLive")
    accounts_at = body.index("/api/v1/accounts")
    assert body.index("if (includeOrders)") < accounts_at, "inside the slow branch"
    assert accounts_at < body.index("return true;"), "and awaited before the render"


def test_the_panel_keeps_the_accounts_across_the_fast_polls():
    """The fast poll does not fetch them, which only works if the last answer is remembered."""
    assert "state.liveAccounts" in APP_JS
    assert "state.liveAccounts" in _function(APP_JS, "renderLiveDetail")


def test_a_zero_balance_and_an_unreadable_account_render_differently():
    """The distinction the whole read is built around: "$0.00" is a claim about a balance and an
    unreadable account is the absence of one. So the numbers become boxes only for an account that
    WAS read; one that could not be read draws no figures at all — not a row of dashes, and not a
    zero.

    The sentence that named the reason went with the panel's bottom warnings block. It is not lost
    from the screen: which account the run is pointed at, and the credential failure behind it, are
    on the header pill, and the tick line prints the same broker message verbatim.
    """
    body = _function(APP_JS, "renderLiveDetail")

    assert "active.known" in body
    assert "active.reason" not in body, "the reason went with the bottom warnings block"
    assert body.index("active.known") < body.index("money(active.equity)"), \
        "the unknown case has to be decided before any number is formatted"


def test_the_panel_reports_only_the_account_being_traded():
    """The panel follows the trading MODE: the account orders would go to, and no other.

    It used to add a warning when the OTHER environment could not be read. Trading on paper
    then showed a 401 for the live account — a fault of an account the run never touches,
    which reads like a fault in this run. That verdict is not hidden: it is on the credential
    badge and in the Account popup. Its BALANCE is shown nowhere, on purpose: the two worth
    panels render the traded account alone (see the log page's accounts panel).
    """
    body = _function(APP_JS, "renderLiveDetail")

    assert "accounts.env" in body, "the row picked out is the environment being traded"
    assert "row.env === accounts.env || row.known" not in body, (
        "no warning about the other environment"
    )
    assert "for (const row of accounts.accounts" not in body, (
        "and the list is not walked for warnings at all"
    )


def test_the_switch_panel_reports_faults_only_for_the_account_being_traded():
    """Same rule in the panel that holds the switch: the other account's POSITIONS are listed
    (something open there is real whatever mode we are in, and it is what stops arming on top
    of it) but not its unreadability."""
    body = _function(APP_JS, "renderTradingPanel")

    assert "account.known === false && (!traded || env === traded)" in body
    assert "for (const p of account.positions || [])" in body, "its positions are still listed"


# ---------------------------------------------------------------------------
# the numbers are boxes now
# ---------------------------------------------------------------------------
def test_the_metrics_are_the_same_boxes_the_backtest_uses():
    """Asked for as "the same style of information boxes as the backtest panel": the same
    class, so they cannot drift into a second look — and now the same BUILDER as the log page's,
    which shows the same three trading boxes."""
    body = _function(SWITCH_JS, "tile")

    assert '<div class="bt-stat' in body
    assert 'class="label"' in body and 'class="value' in body


def test_a_box_can_carry_its_explanation():
    """``data-tip``, not ``title``: the embedded browser in the dashboard does not render a
    native tooltip, which is why the backtest boxes use the CSS one."""
    body = _function(SWITCH_JS, "tile")

    assert "data-tip" in body
    assert "title=" not in body


def test_the_mode_and_the_switch_are_boxes_that_flash_when_they_are_the_risky_setting():
    """The two settings the rest of the panel is read through, as boxes, first.

    Asked for explicitly: one box for the mode and one for whether trading is on, RED and pulsing
    when the box is set the dangerous way (a LIVE account, trading ARMED) and BLUE and still when
    it is set the safe way (paper, off). Both ends are marked on purpose — an unmarked box beside
    a marked one reads as "unknown" rather than "the safe one".

    The tint goes on the BOX (``tile``'s fifth argument) rather than on the number inside it,
    because these two are states and every other tile in the grid is a measurement.
    """
    mode = _function(SWITCH_JS, "envTile")
    trade = _function(SWITCH_JS, "tradeTile")

    assert 'live ? "flash-red" : (env === "paper" ? "tint-blue" : "")' in mode
    assert 'tr ? (armed ? "flash-red" : "tint-blue") : ""' in trade

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".bt-stat.tint-blue" in css


def test_the_flash_is_a_css_animation_that_stays_visible_with_motion_off():
    """The pulse, and the fallback: with ``prefers-reduced-motion`` the box keeps the red border
    and drops only the animation, so the warning survives for a reader who turned motion off."""
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")

    assert ".bt-stat.flash-red" in css
    assert "@keyframes bt-flash" in css
    reduced = css[css.index("prefers-reduced-motion"):]
    assert ".bt-stat.flash-red" in reduced[:400] and "animation: none" in reduced[:400]


def test_the_grid_has_its_own_declaration():
    """The shared grid rule is scoped to ``#bt-metrics``, so tiles under any other id stack
    in a single column — the report page hit this and says so in a comment."""
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")

    assert ".live-metrics { display: grid" in css
    assert 'id="live-metrics" class="live-metrics"' in \
        (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")


def test_the_two_state_boxes_are_buttons_on_the_header_s_own_handlers():
    """Asked for: the Mode and Trading boxes act as buttons, with the same handlers and behaviour
    as the controls in the top nav.

    Each calls what the header's own control for that state called — the master switch's
    ``toggleTrading()``, and the mode's single write path — so there is no second implementation of
    either decision, and no second confirmation standing between a click and real money. A real
    ``<button>`` rather than a div with a handler, because the keyboard has to reach it; the
    measurement boxes beside them stay divs, which is what makes "this one is pressable" readable
    from the shape alone. Both pages place the same two boxes, with their own handler names.
    """
    body = _function(SWITCH_JS, "tile")

    assert '<button type="button" class="bt-stat' in body, "a button, not a div with a click"
    assert 'onclick="' in body
    assert '<div class="bt-stat' in body, "and the plain boxes are still plain"

    detail = _function(APP_JS, "renderLiveDetail")
    assert 'TraiderSwitch.envTile(mode, locked, "onModeBoxClick()")' in detail
    assert 'TraiderSwitch.tradeTile(state.tradingPayload, "toggleTrading()")' in detail

    boxes = _function(LOG_JS, "renderBoxes")
    assert 'TraiderSwitch.envTile(inPlay, !!trading.locked, "flipMode()")' in boxes
    assert 'TraiderSwitch.tradeTile(state.trading, "toggleTrading()")' in boxes

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert "button.bt-stat {" in css, (
        "the global `button` rule sets a height and a centred font; the box has to keep its own"
    )


def test_the_mode_cannot_be_switched_while_trading_is_on():
    """Asked for: switching accounts in flight must be impossible from the panel.

    The server refuses the write (409) and the loop would be left running against an account it was
    not opened on — sending LIVE orders against positions it opened on paper is the worst case in
    this app. So the Mode box goes down with every other locked control, and its hint says why
    instead of inviting a click that would be refused.
    """
    body = _function(SWITCH_JS, "envTile")

    assert "locked" in body, "the box is handed the lock"
    assert "trading is ON — turn it off to switch accounts" in body, "and says why"

    tile = _function(SWITCH_JS, "tile")
    assert "disabled" in tile and '? " disabled"' in tile, "a locked box renders disabled"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert "button.bt-stat:disabled," in css and "cursor: not-allowed" in css

    # Both pages hand it the lock they have: the dashboard's configuration lock, and the log page's
    # copy of the same field.
    assert 'TraiderSwitch.envTile(mode, locked, ' in _function(APP_JS, "renderLiveDetail")
    assert '"flipMode()"' in _function(LOG_JS, "renderBoxes")


def test_the_switch_itself_is_never_locked():
    """The one control that has to survive every lock: turning trading OFF.

    A disabled switch would leave the bot running with no way to stop it from the page — and it is
    the lock's own precondition, since the server unlocks when the switch goes off. The box is
    disabled for one reason only, on both pages: nothing was READ, and a switch must not be used on
    a guess.
    """
    body = _function(SWITCH_JS, "tradeTile")

    assert "locked" not in body, "the switch's box is never handed the configuration lock"
    assert "click, !payload" in body, "only an unread state disables it"


def test_the_state_boxes_carry_the_same_two_dots():
    """Asked for: the dots the header carried, on the Mode and Trading boxes, with the same
    behaviour — blue for the calm setting, red for the one that spends money, and red BLINKS.

    They are styled circles driven by a CSS animation rather than the dropdown's 🔵/🔴 glyphs, and
    that difference is the point: a glyph in the box would have to be rewritten by the same 700ms
    timer the dropdown uses, and that rewrite goes through ``setIfChanged`` — it would replace the
    two BUTTONS under the cursor mid-click, and restart their pulse on every tick. The rules are
    the header's own: both ends are marked (an unmarked box beside a marked one reads as "unknown"
    rather than "fine"), and with motion reduced the red dot keeps its colour and stops moving.
    """
    assert 'dot(live)' in _function(SWITCH_JS, "envTile")
    assert "dot(armed)" in _function(SWITCH_JS, "tradeTile")

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".bt-stat .dot.calm" in css and ".bt-stat .dot.alert" in css
    assert "animation: dot-blink" in css and "@keyframes dot-blink" in css
    reduced = css[css.index("prefers-reduced-motion"):]
    assert ".bt-stat .dot.alert { animation: none; }" in reduced[:600]


def test_the_open_count_moved_from_the_header_into_the_panel():
    """Asked for: an Open box with the count of open positions — and then on the log page too.

    It is the header's "0 open" pill, moved: the count of the account being traded, with the tip
    still naming what the OTHER account holds — a position there is real whatever mode this run is
    in, and it is what refuses an arming. An account that could not be READ is not a count of zero,
    so that box shows "?" where the pill said "unreadable".
    """
    body = _function(SWITCH_JS, "openTile")

    assert "known === false" in body and "unknown_count" in body
    assert '"?"' in body, "an unreadable account is not a zero"
    assert "TraiderSwitch.openTile(" in _function(APP_JS, "renderLiveDetail"), "in the panel's grid"
    assert "TraiderSwitch.openTile(" in _function(LOG_JS, "renderBoxes"), "and the log page's"

    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="open-count"' not in html, "and the header's copy is gone"


def test_the_armed_note_is_gone_from_the_panel():
    """Asked for removal: "Armed. While ON, every configuration panel is locked…".

    Both facts it carried are already on screen — the Trading box reads "on", and the mode it is
    armed in is its own box — and the lock is not silent about itself either: a locked panel
    refuses with the server's own message. Gone from the markup, the script and the stylesheet.
    """
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="trading-live-note"' not in html
    assert "trading-live-note" not in APP_JS, "and nothing writes to the id that is gone"
    assert "nothing running it, nothing trades" not in html


def test_the_bottom_warnings_block_is_gone():
    """Asked for removal: the block of sentences under the boxes.

    Its lines repeated what the panel had already said — the broker and exchange failure is the
    tick line's own message, verbatim — and a panel that says the same thing twice at two ends of
    one card reads as two problems. Gone from the markup, the script and the stylesheet.
    """
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")

    assert 'id="live-warnings"' not in html
    assert "live-warnings" not in APP_JS, "and nothing writes to the id that is gone"
    assert "#live-warnings" not in css


def test_the_log_page_reads_and_renders_the_accounts():
    assert "/api/v1/accounts" in LOG_JS
    assert 'setIfChanged($("lg-accounts")' in LOG_JS
    assert "lg-accounts" in LOG_HTML


def test_the_log_page_accounts_are_boxes_like_every_other_screen():
    """A number is read by glancing at a box, a table by scanning labels one at a time — the
    dashboard's trading panel, the backtest KPIs and the report page all say so in their own
    words, and this page's account figures belong with them.

    The grid rule comes with it: the shared one is scoped to ``#bt-metrics``, so a grid under any
    other id stacks its boxes in one column unless the page declares its own. The report page hit
    exactly that, and this test is the one that would catch the log page repeating it."""
    body = _function(LOG_JS, "renderBoxes")
    assert "tile(" in body and "lg-metrics" in body
    assert 'class="lg-acct' in body, "each row of boxes has to say which account it is"
    assert "lg-acct-tag" in body, "and whether it is the one in play"

    tile = _function(SWITCH_JS, "tile")
    assert "bt-stat" in tile, "the same box the dashboard and the report page use"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".lg-metrics {\n  display: grid" in css


def test_the_log_page_shows_only_the_account_being_traded():
    """Switching paper -> live left the log page reading as the paper account, and the fix is
    subtraction: the panel renders the account in play and NOTHING ELSE.

    ``/accounts`` reports both environments and an ``env`` saying which one is being traded. The
    panel used to render both, in the payload's own order, so with paper listed first and no live
    credentials to show, the only figures on the page belonged to the account that was NOT in play
    — under a header that said "live". Marking them was the first attempt and it is not enough: a
    balance sitting on the panel is a number waiting to be read as the wrong account's, and the
    only way to stop that is for it not to be there.

    Nothing is lost by it. The reason an unreadable account cannot be read is still stated, and
    what an account HOLDS stays on the next panel — the part of the idle account an operator
    actually needs, and it is what refuses an arming on top of it.
    """
    body = _function(LOG_JS, "renderBoxes")

    assert "inPlayEnv()" in body, "the account in play, from the page's one helper"
    assert ".filter(mine)" in body, "and the rows are filtered to it"
    assert "not in play" not in body, "the idle account is not rendered at all"
    assert ".sort(" not in body, "so there is nothing left to order"
    assert "in play</span>" in body, "the one row there is says which account it is"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".lg-acct.in-play" in css, "the mark has to be visible, not just present in the DOM"


def test_every_panel_on_the_log_page_is_scoped_to_the_account_in_play():
    """One rule for the whole page, not a filter per table: WHICH account is the scope of the page,
    and every panel that has an account dimension obeys the same helper.

    This is the invariant that the paper/live mix broke. The loop writes ONE set of files per
    strategy — ``ticks/<day>.jsonl``, ``orders.jsonl``, ``trades.jsonl``, ``latest.json`` — so both
    accounts' rows sit in the same files, separated only by the ``env`` field on each record. A
    page that renders them together has to be read with a mental filter, and the one time it
    mattered, the paper account's equity was read as the live account's.

    Asserted per panel rather than as "the file contains `mine`", because a function that lost its
    filter would still leave the other six passing.
    """
    expected = {
        # panel -> the element it fills, and what scoping it there looks like
        "renderBoxes": ("lg-accounts", "mine"),
        "renderAccount": ("lg-positions", "mine"),
        "renderTicks": ("lg-ticks", "mine"),
        "renderOrders": ("lg-orders", "mine"),
        "renderTrades": ("lg-trades", "mine"),
        "renderGates": ("lg-gates", "mine"),
    }
    for name, (host, needle) in expected.items():
        body = _function(LOG_JS, name)
        assert host in body, f"{name} must be the panel that fills #{host}"
        assert needle in body, f"{name} must scope to the account in play"

    # The gates are the surface that reads the STRATEGY-WIDE heartbeat (one ``latest.json`` per
    # strategy, whatever account the tick ran for), so it is where another account's data is most
    # likely to leak in — and it is exactly what that panel did, drawing a paper tick's pipeline
    # under a ``live`` heading.
    gates = _function(LOG_JS, "renderGates")
    assert "if (!mine(tick))" in gates, "a tick from the other account's gates are not drawn"
    assert "nothing has ticked for the ${inPlayEnv()} account yet" in gates, (
        "and the panel says why it is empty rather than leaving it looking broken"
    )


def test_an_unreadable_account_in_play_is_a_warning_not_a_footnote():
    """The traded account being unreadable is the reason nothing can trade, and the reason there
    are no figures under its name — so it is amber and it names the fix, never a row of dashes and
    never a muted line. Muted grey in a list is what this page was read past.

    This is the state the paper -> live switch produced with no live keys configured.
    """
    body = _function(LOG_JS, "renderBoxes")

    assert 'class="warn"' in body, "the traded account's failure is a warning"
    assert "empty(why)" not in body, "not a muted line, and not for some other account"
    assert "Account Settings" in body, "the warning has to say where to fix it"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert "#lg-accounts p.warn" in css, "and the warning has to be visible"


def test_the_day_percentage_is_not_divided_by_a_hundred_twice():
    """``day_pl_pct`` arrives as a percentage, while the page's ``percent()`` helper multiplies
    a FRACTION by a hundred — using that helper here would report a 1% day as 100%."""
    body = _function(LOG_JS, "renderBoxes")

    assert "percentText(" in body
    assert "percent(" not in body.replace("percentText(", ""), \
        "the fraction helper must not touch a value that is already a percentage"

    formatter = _function(LOG_JS, "percentText")
    assert "* 100" not in formatter and "percent(" not in formatter.replace("percentText(", "")


def test_the_fast_poll_does_not_blank_what_the_broker_told_us():
    """The 5-second poll fetches no orders, so a render driven by its own null argument dropped
    every Alpaca-derived tile and put the protection hint back — for the rest of the minute,
    until the slow poll restored them. Caught in the browser by counting tiles either side of a
    poll; no test could see it, because both states are "correct" for the render that made them.
    """
    body = _function(APP_JS, "renderLive")

    assert "state.liveOrders = orders" in body, "the last read has to be remembered"
    assert "renderLiveDetail(loop, state.liveOrders)" in body, "and rendered from"
    assert "renderProtection(state.liveOrders" in body


def test_the_panel_arms_its_own_poll_when_the_page_loads():
    """Reported from the page: the account boxes were simply missing.

    The boot read is the cheap half (the loop's own files), and the broker half — the accounts,
    the orders, the clock — only runs on the slow poll. Nothing started that poll after a page
    load: it was armed by the ↻ button, by folding and unfolding the row, and by a tab switch,
    so a freshly opened dashboard sat on the files-only read and the boxes that come from Alpaca
    never appeared. Arming it here makes the first poll happen seconds after the page does.
    """
    body = _function(APP_JS, "loadTrading")

    assert "loadLive(false);" in body, "boot reads the files only"
    assert "scheduleLivePoll();" in body, "and then has to arm the poll that fetches the rest"


def test_a_failed_account_read_keeps_the_last_good_boxes():
    """An account that could not be re-read is not an account worth zero.

    The failure payload carries an empty ``accounts`` list, and storing it replaced a perfectly
    good snapshot with a blank panel — no boxes at all. Reported as "the boxes disappeared",
    which is exactly how it read.
    """
    body = _function(APP_JS, "renderLive")

    assert "if (accounts.ok !== false) state.liveAccounts = accounts;" in body, \
        "only a GOOD read is stored"


def test_the_account_boxes_are_the_same_five_the_log_page_shows():
    """Equity, Day, Cash, buying power and the account's standing: the same set the log page's
    accounts panel reports, in the same order, because the two screens answer the same question
    about the same account."""
    body = _function(APP_JS, "renderLiveDetail")

    for label in ('"Account"', '"Equity"', '"Day"', '"Cash"', '"Buying power"', '"Status"'):
        assert f"liveTile({label}" in body, f"the {label} box belongs in this grid"


def test_the_other_account_s_figures_are_never_drawn_under_this_mode():
    """Asked for: after a mode switch the section must show the NEW account's data.

    The snapshot in hand belongs to the account the panel was about when it was read, so a switch
    makes it the account we just left. The panel draws no figures until the read for this mode
    lands — a second or so, and never the wrong numbers. ``state.liveAccounts`` keeps the old
    snapshot, so a switch BACK draws that account's own last known figures at once rather than
    blinking empty.
    """
    body = _function(APP_JS, "renderLiveDetail")

    assert 'const staleSnapshot = !!(accounts.env && mode && accounts.env !== mode);' in body
    assert "staleSnapshot" in body.split("const active")[1].split(";")[0], \
        "the figure rows are gated on the snapshot being about this account"


def test_a_written_switch_takes_the_full_read_at_once():
    """Asked for: the mode switch must not wait for the poll.

    Only the mode box comes from the switch's own read; the account figures come from the broker
    half of the live poll, on a minute cadence. So the write re-reads the switch, re-reads the
    broker, and only then restarts the cadence — in that order, because the mode has to be the new
    one before the figures for it are asked for. The write itself is the shared module's, which
    calls the reload its caller handed it; this is the dashboard's.
    """
    shared = _function(SWITCH_JS, "flipEnv")
    assert "if (deps.reload) await deps.reload();" in shared, (
        "the shared write re-reads WHATEVER the page says it re-reads, and only on a real change"
    )

    body = _function(APP_JS, "onModeBoxClick")

    assert "await loadTrading();" in body
    assert "await refreshLiveNow();" in body
    assert body.index("await loadTrading();") < body.index("await refreshLiveNow();"), \
        "the new mode is written to the page before the account behind it is read"

    # The log page's own reload is a full re-read: the mode scopes every panel on that page.
    log = _function(LOG_JS, "flipMode")
    assert "loadAll(" in log


def test_the_panel_does_not_restate_the_strategy_or_the_endpoint():
    """Asked for removal: the Strategy / Instrument / Bar size / Endpoint rows.

    Every one of the four is on screen already — the strategy bar names the strategy, the header
    summary carries the instrument and the bar size, the Mode box carries the account and its
    endpoint.
    """
    body = _function(APP_JS, "renderTradingPanel")

    for row in ('execRow("Strategy"', 'execRow("Instrument"', 'execRow("Bar size"',
                'execRow("Endpoint"'):
        assert row not in body, f"{row} is context the page already shows"


def test_the_loop_line_names_no_process():
    """Asked for removal: "Held by pid 23710 on Kalins-MacBook-Pro.local (strategy 'GPRO'),
    started 2026-09-20T21:46:11.587122+00:00 ·".

    It is a debugging string on a status line, and it pushed the one fact worth reading — when
    the loop wakes next — off the end of it. Which process holds the claim is in the log and in
    the lease file; the panel says whether anything is running and when it runs again.

    What the line DOES carry, when it applies, is the account: the tick comes from the loop's own
    record, which is written for one environment, so after a mode switch it can be the account we
    just left — and it has to say so rather than be read as this account's.
    """
    body = _function(APP_JS, "renderLive")

    assert "holder_text" not in body and "Held by" not in body and "Last claimed by" not in body
    assert "Next wake" in body, "the useful half of that sentence stays"
    assert 'const belongsTo = !tickEnv || !inPlay || tickEnv === inPlay;' in body, \
        "a tick from the other account is marked, not passed off as this one's"
