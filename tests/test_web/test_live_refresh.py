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
# the Live panel
# ---------------------------------------------------------------------------
def test_the_live_poll_is_gated_on_the_panel_being_open_and_the_tab_being_visible():
    body = _function(APP_JS, "livePanelVisible")

    assert "body.hidden" in body, "a collapsed panel must not poll"
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
    """Same property on the Live panel: the visibility check is what prevents the poll, not
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
