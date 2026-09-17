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


def test_the_tick_table_renders_a_bar_s_notes_with_a_warning_mark():
    """A note exists to be SEEN, so the tick table is where it has to land.

    ``src.data.quality`` reports an odd bar without refusing it, which means nothing else in
    the system will ever mention it — if this cell is missing, the note is written to disk and
    read by nobody. The ⚠ and its own colour are what keep it distinguishable at a glance from
    the ordinary reason text beside it in the same row.
    """
    assert "notesCell(tick.notes)" in LOG_JS, "the tick row renders the notes"
    assert '"notes"' in LOG_JS, "and the table declares the column"

    start = LOG_JS.index("function notesCell")
    helper = LOG_JS[start:start + 700]
    assert "⚠" in helper, "marked, so it is not mistaken for the tick's reason"
    assert 'class="warn"' in helper
    assert '"—"' in helper, "an em dash when there is nothing to say, not a blank cell"
    assert ".lg-table td.warn" in CSS, "the marker has its own colour (amber, not red)"


def test_the_live_panel_says_when_arming_has_nothing_to_run_it():
    """Two facts, one switch — so the panel has to join them.

    Arming writes ``trading.json``; nothing ticks until a process runs the loop, and this
    dashboard never starts one (that is the two-process split). The chip says "stopped" whether
    trading is on or off, so without a line joining the two, flipping the switch looks like it
    did nothing — which is exactly how it was read.

    A visible LINE rather than a ``title``: the embedded browser renders no native tooltip, and
    the chip's own explanation is one.
    """
    assert "trading is armed, but no loop is running" in APP_JS
    assert "python -m src.main" in APP_JS, "and it says what to start"

    at = APP_JS.index("trading is armed, but no loop is running")
    assert "lines.push(" in APP_JS[at - 40:at], "rendered as a visible line, not a tooltip"

    # The guard immediately above it has to require BOTH facts. Either one alone makes the note
    # a lie in the other case: with the switch off there is nothing armed to warn about, and
    # while the loop IS running the warning would contradict the chip beside it.
    guard = APP_JS[APP_JS.rindex("if (", 0, at):at].split("\n", 1)[0]
    assert "armed" in guard, f"gated on the switch being ON: {guard}"
    assert 'loop.state === "stopped"' in guard and 'loop.state === "never"' in guard, (
        f"and on the loop not running: {guard}"
    )


# ---------------------------------------------------------------------------
# the Trading panel moved
# ---------------------------------------------------------------------------
def test_the_panels_are_named_uniquely_and_the_account_panel_is_trading():
    """The panel that reports the account and the loop is titled "Trading" — and only one is.

    Two headings with the same word on one screen is a panel nobody can refer to, and the
    switch at the top was already called "Trading", so it is the switch that takes the longer
    name. (The element ids stay ``live-*``: they are internal, and renaming them would churn
    the CSS and the JS for nothing anyone can see.)
    """
    headings = re.findall(r"<h2>([^<]*)</h2>", HTML)

    assert headings.count("Trading") == 1, f"exactly one panel is 'Trading': {headings}"
    assert "Live" not in headings, "the account panel is not called Live any more"
    assert "Trading switch" in headings, "and the switch is nameable apart from it"


def test_both_panels_report_the_account_being_traded_and_not_the_others_faults():
    """A 401 on the account this run never touches must not read like a fault in this run.

    Both panels answer about the account the trading MODE points at. The switch panel still
    lists the other account's POSITIONS — something open over there is real whatever mode we
    are in, and it is what stops the bot being armed on top of it — but not its unreadability.
    That verdict is not hidden: it stays on the credential badge, in the Account popup, and on
    the log page, which shows both accounts side by side.
    """
    assert "const traded = String((exec && exec.env)" in APP_JS
    assert "account.known === false && (!traded || env === traded)" in APP_JS
    assert "row.env === accounts.env || row.known" not in APP_JS, (
        "the account panel must not warn about the other environment any more"
    )


def test_the_trading_panel_is_in_the_left_column_under_the_backtest():
    assert _column('id="live-card"') == "left"

    backtest = HTML.index('id="backtest"')
    live = HTML.index('id="live-card"')
    assert backtest < live, "the Trading panel sits under the Backtest panel"


def test_the_live_panel_is_not_hidden_with_the_chart():
    """It reports the loop, the accounts and the exchange — none of which need a stored
    dataset. Inside ``#dashboard`` it would vanish with the chart when there is none, which is
    exactly when someone is looking for why nothing is happening."""
    dashboard_end = HTML.index("</section>", HTML.index('id="dashboard"'))
    assert HTML.index('id="live-card"') > dashboard_end


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

    body = HTML[HTML.index('id="live-body"'):HTML.index("</section>", HTML.index('id="live-body"'))]
    assert "/log" not in body, "and only there, not in both places"


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
    card = HTML[HTML.index('id="backtest"'):HTML.index('id="live-card"')]
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
