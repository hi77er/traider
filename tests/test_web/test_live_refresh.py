"""The log page's poll, and the trading boxes both pages draw.

These are text-level assertions on the static files, which this project has no runner to do
better for. They are here anyway because the invariants they hold cost MONEY when they break,
and a browser check cannot be run in CI: everything else about the poll is verified by hand.

What is left of the polling on these pages is the log page's. A past day cannot gain rows, so
refreshing one is pure cost and a page that churns under a reader's cursor — which is why the
refresh is gated on today and on the tab being visible. The lab's own poll went with the Trading
panel: the switch, the mode and the account were removed from that page whole, so nothing there
reads the broker any more and there is no cadence left to gate.

The other half of the file is the SHARED box module's. Both pages draw the same trading boxes,
so what a box looks like, what its dots do and what the mode write re-reads belong with the
module rather than with whichever page happens to host them.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
LOG_JS = (ROOT / "src" / "web" / "static" / "log.js").read_text(encoding="utf-8")
# The box builders, the switch's wording and the mode write are the SHARED module's: the Session
# monitor shows the three trading boxes and the lab still boxes its own numbers, so the assertions
# about what a box looks like belong with the module rather than with either page.
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
# the log page's own poll
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


def test_the_log_poll_reschedules_itself():
    body = _function(LOG_JS, "schedulePoll")

    assert "setTimeout" in body and "setInterval" not in body


def test_choosing_a_day_is_what_decides_whether_the_log_polls():
    """Switching to a past day has to stop the poll, not leave the old one running."""
    assert "schedulePoll();" in _function(LOG_JS, "loadLog")


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
    """The two settings the rest of the page is read through, as boxes, first.

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


def test_the_two_state_boxes_are_buttons_on_the_page_s_own_handlers():
    """Asked for: the Mode and Trading boxes act as buttons, with the same handlers and behaviour
    as the header's own controls.

    Each calls what that page's control for the state called — the master switch's
    ``toggleTrading()``, and the mode's single write path — so there is no second implementation of
    either decision, and no second confirmation standing between a click and real money. A real
    ``<button>`` rather than a div with a handler, because the keyboard has to reach it; the
    measurement boxes beside them stay divs, which is what makes "this one is pressable" readable
    from the shape alone. The page hosting them supplies its own handler names — with the Trading
    panel gone from the lab, the Session monitor is the page that does.
    """
    body = _function(SWITCH_JS, "tile")

    assert '<button type="button" class="bt-stat' in body, "a button, not a div with a click"
    assert 'onclick="' in body
    assert '<div class="bt-stat' in body, "and the plain boxes are still plain"

    boxes = _function(LOG_JS, "renderBoxes")
    assert 'TraiderSwitch.envTile(inPlay, !!trading.locked, "flipMode()")' in boxes
    assert 'TraiderSwitch.tradeTile(state.trading, "toggleTrading()")' in boxes

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert "button.bt-stat {" in css, (
        "the global `button` rule sets a height and a centred font; the box has to keep its own"
    )


def test_the_mode_cannot_be_switched_while_trading_is_on():
    """Asked for: switching accounts in flight must be impossible from the box.

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

    # The page hosting the box hands it the lock it has: the Session monitor's copy of the field
    # the server enforces.
    assert '"flipMode()"' in _function(LOG_JS, "renderBoxes")


def test_the_switch_itself_is_never_locked():
    """The one control that has to survive every lock: turning trading OFF.

    A disabled switch would leave the bot running with no way to stop it from the page — and it is
    the lock's own precondition, since the server unlocks when the switch goes off. The box is
    disabled for one reason only, on the page that carries it: nothing was READ, and a switch must
    not be used on a guess.
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

    The lab drew it too until the Trading panel left that page; the Session monitor's grid is the
    one that carries it now, and the header's copy is gone for good.
    """
    body = _function(SWITCH_JS, "openTile")

    assert "known === false" in body and "unknown_count" in body
    assert '"?"' in body, "an unreadable account is not a zero"
    assert "TraiderSwitch.openTile(" in _function(LOG_JS, "renderBoxes"), "in the page's grid"

    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="open-count"' not in html, "and the header's copy is gone"


# ---------------------------------------------------------------------------
# the account figures on the log page
# ---------------------------------------------------------------------------
def test_the_log_page_reads_and_renders_the_accounts():
    assert "/api/v1/accounts" in LOG_JS
    assert 'setIfChanged($("lg-accounts")' in LOG_JS
    assert "lg-accounts" in LOG_HTML


def test_the_log_page_accounts_are_boxes_like_every_other_screen():
    """A number is read by glancing at a box, a table by scanning labels one at a time — the backtest
    KPIs, the report page and the lab's own signal and risk boxes all say so in their own words, and
    this page's account figures belong with them.

    The grid rule comes with it: the shared one is scoped to ``#bt-metrics``, so a grid under any
    other id stacks its boxes in one column unless the page declares its own. The report page hit
    exactly that, and this test is the one that would catch the log page repeating it."""
    body = _function(LOG_JS, "accountFigures")
    assert "tile(" in body and "lg-metrics" in body

    tile = _function(SWITCH_JS, "tile")
    assert "bt-stat" in tile, "the same box the lab and the report page use"

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
    # The account's NAME line ("paper ****R9V", tagged "in play") was removed on request: the Mode
    # box above the figures already says which account it is, the page is scoped to one throughout,
    # and the masked number was a label to read twice for nothing. What must not come back is the
    # other account's FIGURES — the reason the line existed in the first place.
    assert "lg-acct" not in body, "no name line above the boxes"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".lg-acct" not in css, "and its styles went with it, rather than lingering"


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


def test_a_broker_hiccup_does_not_send_the_reader_after_their_keys():
    """Two ways an account comes back unreadable, and one sentence between them is a lie half the
    time. A key that is missing or refused is the reader's to fix and the panel says where; a
    broker that timed out is NOT — and the panel is exactly where someone goes to ask "is anything
    trading", so telling them to re-enter the keys they already entered is a wild goose chase."""
    body = _function(LOG_JS, "accountTrouble")
    returns = [part for part in body.split("return ") if "<p class=" in part]

    assert len(returns) == 2, "one sentence for the credential, one for the broker"
    assert "Account Settings" in returns[0] and "add the" in returns[0]
    assert "Account Settings" not in returns[1], "a timeout is not fixed in Account Settings"
    assert "key pair is configured" in returns[1] and "did not answer" in returns[1]
    assert 'row.fix === "credentials"' in body, "the server's own verdict decides which"


def test_the_panel_draws_figures_or_the_trouble_line_and_never_both():
    """``renderBoxes`` maps every row through one of the two, so a known account can never be
    rendered as a warning and an unreadable one can never render as figures."""
    body = _function(LOG_JS, "renderBoxes")

    assert "row.known ? accountFigures(row) : accountTrouble(row)" in body


def test_an_unreadable_account_in_play_is_a_warning_not_a_footnote():
    """The traded account being unreadable is the reason nothing can trade, and the reason there
    are no figures under its name — so it is amber and, when the credential is what is wrong, it
    names the fix: never a row of dashes and never a muted line. Muted grey in a list is what this
    page was read past.

    This is the state the paper -> live switch produced with no live keys configured.
    """
    body = _function(LOG_JS, "accountTrouble")
    figures = _function(LOG_JS, "accountFigures")

    assert 'class="warn"' in body, "the traded account's failure is a warning"
    assert "empty(why)" not in body, "not a muted line, and not for some other account"
    assert "Account Settings" in body, "the credential warning has to say where to fix it"
    assert 'class="warn"' not in figures, "and an account that answered is never a warning"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert "#lg-accounts p.warn" in css, "and the warning has to be visible"


def test_the_day_percentage_is_not_divided_by_a_hundred_twice():
    """``day_pl_pct`` arrives as a percentage, while the page's ``percent()`` helper multiplies
    a FRACTION by a hundred — using that helper here would report a 1% day as 100%."""
    body = _function(LOG_JS, "accountFigures")

    assert "percentText(" in body
    assert "percent(" not in body.replace("percentText(", ""), \
        "the fraction helper must not touch a value that is already a percentage"

    formatter = _function(LOG_JS, "percentText")
    assert "* 100" not in formatter and "percent(" not in formatter.replace("percentText(", "")


def test_the_lab_s_own_numbers_are_still_the_shared_module_s_boxes():
    """The Trading panel left the lab, and its account grid went with it — but the lab still puts
    numbers in boxes: the signal counts under the rules, and the risk settings beside them.

    Both go through the module the Session monitor's boxes come from, so there is one box in this
    app rather than a second look that drifts. The lab's alias for it was renamed ``liveTile`` ->
    ``statTile`` when the account grid went; the tiles that stayed still build themselves with it.
    """
    signals = _function(APP_JS, "signalTiles")
    risk = _function(APP_JS, "strategyRiskTiles")

    assert "statTile(" in signals, "the signal counts are boxes, not a sentence"
    assert "statTile(" in risk, "and so is every risk setting"
    assert "liveTile" not in APP_JS, "the alias the account grid used is gone"


def test_a_written_switch_takes_the_full_read_at_once():
    """Asked for: the mode switch must not wait for the poll.

    Only the mode box comes from the switch's own read; every panel on the Session monitor is
    scoped to the account in play, so what the write has to re-read is the PAGE, not the box. That
    reload is the page's own and is handed to the shared write — the write itself is the module's,
    which is what keeps the confirmation and the endpoint in one place.
    """
    shared = _function(SWITCH_JS, "flipEnv")
    assert "if (deps.reload) await deps.reload();" in shared, (
        "the shared write re-reads WHATEVER the page says it re-reads, and only on a real change"
    )

    log = _function(LOG_JS, "flipMode")
    assert "loadAll(" in log, "the whole page, because the mode scopes every panel on it"

