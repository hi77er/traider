"""Behavioural tests for the shared chart zoom (``chart_zoom.js``).

The dashboard and the report page draw their series in several charts: the price
chart, one chart per indicator drawn separately (MACD, RSI, ATR, momentum,
volatility, relative volume, volume) and the backtest equity/drawdown charts. The
rule for the zoom is a property of the chart and its data, not of the series it
holds, so it lives in one file and is exercised here against fakes.

What is pinned:

* a plain wheel NEVER zooms (the library treats any vertical delta as zoom, so a
  two-finger swipe that carries a vertical component zoomed while the user meant
  to pan); only a pinch — a wheel event with ``ctrlKey`` — zooms;
* the zoom stops at the data on BOTH ends. It used to be unbounded, and asking for
  a window wider than the series made the library clamp what it reported back:
  re-deriving the next step from that answer collapsed the view to a handful of
  bars ("the zoom restarts at maximum zoom in") as soon as a pinch-out passed the
  end of the data;
* one listener per element, re-pointed when a chart is rebuilt on it;
* no SINGLE event may take the view somewhere drastic. A pinch arrives as a stream of
  wheel events, and a flick — one event with a delta in the hundreds — used to collapse
  the window onto a single bar in one frame ("it suddenly zoomed in to the maximum");
* a gesture that is mostly SIDEWAYS is a move, not a zoom. Two fingers travelling
  together is a pan, and with ``ctrl`` stamped on it the zoom was applied to a reader who
  was trying to reach an earlier stretch of the session.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHART_ZOOM_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "chart_zoom.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the chart zoom"
)

HARNESS = r"""
// ---- fakes -----------------------------------------------------------------
function makeElement() {
  const el = {
    added: 0,
    wheels: 0,
    drags: 0,
    handlers: {},
    addEventListener: function (name, cb) {
      el.added += 1;
      if (name === "wheel") el.wheels += 1;
      if (name === "mousedown") el.drags += 1;
      if (!el.handlers[name]) el.handlers[name] = [];
      el.handlers[name].push(cb);
    },
    getBoundingClientRect: function () { return { left: 0, width: 1000 }; },
    fire: function (name, ev) {
      const list = el.handlers[name] || [];
      for (let i = 0; i < list.length; i++) list[i](ev);
    },
  };
  return el;
}

function makeChart(from, to) {
  const rec = { sets: [], from: from, to: to };
  const chart = {
    rec: rec,
    timeScale: function () {
      return {
        getVisibleLogicalRange: function () { return { from: rec.from, to: rec.to }; },
        setVisibleLogicalRange: function (r) {
          rec.sets.push({ from: r.from, to: r.to });
          rec.from = r.from;
          rec.to = r.to;
        },
        width: function () { return 1000; },
      };
    },
  };
  return chart;
}

function wheel(deltaY, ctrlKey, clientX, deltaX) {
  return {
    ctrlKey: !!ctrlKey,
    deltaY: deltaY,
    // Most wheel events carry no horizontal part at all, so it defaults to none.
    deltaX: deltaX === undefined ? 0 : deltaX,
    clientX: clientX === undefined ? 500 : clientX,
    // Measured in the browser: a wheel reported while a button is held still comes with
    // `buttons: 0`, so the handler cannot rely on it alone — the drag flag is the real signal.
    buttons: 0,
    defaultPrevented: false,
    stopped: false,
    preventDefault: function () { this.defaultPrevented = true; },
    stopPropagation: function () { this.stopped = true; },
  };
}

function span(r) { return r.to - r.from; }

const out = { ranges: {}, bound: {}, flags: {} };

// ---- 1. pure clamping ------------------------------------------------------
const mid = { from: 0, to: 100 };
out.flags.maxZoomStep = ChartZoom.MAX_ZOOM_STEP;
out.flags.maxStepFactor = Math.exp(ChartZoom.MAX_ZOOM_STEP);
out.ranges.zoomOut = ChartZoom.clampRange(mid, 100, 0.5, 400);       // small step out
// Pinching OUT in a stream until it stops: the window never gets wider than the series.
let widened = { from: 300, to: 400 };
for (let i = 0; i < 20; i++) widened = ChartZoom.clampRange(widened, 600, 0.5, 400);
out.ranges.zoomOutPastEnd = widened;
out.ranges.zoomInHard = ChartZoom.clampRange(mid, -3000, 0.5, 400);
// ...and pinching IN at the RIGHT edge until it stops: the floor, with the bar under the pointer
// staying under it.
let held = { from: 300, to: 400 };
for (let i = 0; i < 40; i++) held = ChartZoom.clampRange(held, -600, 1, 400);
out.ranges.zoomInFromRight = held;
out.ranges.unknownBars = ChartZoom.clampRange(mid, 600, 0.5, 0);
out.ranges.anchorLeft = ChartZoom.clampRange(mid, -100, 0, 400);    // pinch at the left edge
out.ranges.anchorRight = ChartZoom.clampRange(mid, -100, 1, 400);   // ...and at the right one
out.ranges.degenerate = ChartZoom.clampRange({ from: 5, to: 5 }, 60, 0.5, 400);
// The reported bug: the window was panned into the empty space after the last bar
// (bars 1092), and one pinch snapped it back to [0, 1092] before zooming.
out.ranges.pannedIntoWhitespace = ChartZoom.clampRange({ from: 278.8, to: 1369.8 }, 60, 0.5, 1092);
// The floor is still there — a STREAM of pinches reaches it, one event cannot.
let stepped = { from: 0, to: 100 };
for (let i = 0; i < 40; i++) stepped = ChartZoom.clampRange(stepped, -3000, 0.5, 400);
out.ranges.zoomInRepeated = stepped;

// ---- 2. a bound element: plain wheel vs pinch -------------------------------
const el = makeElement();
const chart = makeChart(900, 1000); // a window inside the series, as on screen
ChartZoom.bind(el, function () { return { chart: chart, barCount: 1092 }; });

const plain = wheel(600, false);
el.fire("wheel", plain);
out.flags.plainWheelZoomed = chart.rec.sets.length;
out.flags.plainWheelPrevented = plain.defaultPrevented;
// A plain wheel is deliberately NOT stopped: panning it is the library's job, and it kept it.
out.flags.plainWheelStopped = plain.stopped;

const pinchOut = wheel(60, true);
el.fire("wheel", pinchOut);
out.flags.pinchPrevented = pinchOut.defaultPrevented;
out.flags.pinchStopped = pinchOut.stopped;
out.flags.afterPinchOut = chart.rec.sets.length;

// keep pinching out: once the window is as wide as the series this changes nothing
for (let i = 0; i < 12; i++) el.fire("wheel", wheel(60, true));
const setsAtLimit = chart.rec.sets.length;
for (let i = 0; i < 20; i++) el.fire("wheel", wheel(60, true));
out.bound.heldOut = {
  span: span(chart.rec),
  center: (chart.rec.from + chart.rec.to) / 2,
  sets: chart.rec.sets.length,
  setsAfterReachingTheLimit: chart.rec.sets.length - setsAtLimit,
};

// pinch in hard: stops at a single bar, and then stops doing anything at all
for (let i = 0; i < 40; i++) el.fire("wheel", wheel(-600, true));
const setsAtFloor = chart.rec.sets.length;
for (let i = 0; i < 20; i++) el.fire("wheel", wheel(-600, true));
out.bound.heldIn = {
  span: span(chart.rec),
  sets: chart.rec.sets.length,
  setsAfterReachingTheFloor: chart.rec.sets.length - setsAtFloor,
};

// ---- 3. one listener per element, re-pointed at the current chart -----------
const second = makeChart(10, 30); // a window inside its own data, so a pinch moves
const firstSetsBefore = chart.rec.sets.length;
ChartZoom.bind(el, function () { return { chart: second, barCount: 50 }; });
out.bound.wheelListeners = el.wheels;
out.bound.dragListeners = el.drags;
second.rec.sets = [];
el.fire("wheel", wheel(600, true));
out.bound.secondChartCalls = second.rec.sets.length;
out.bound.firstChartStaysPut = chart.rec.sets.length - firstSetsBefore;

// ---- 4. the wheels that belong to a DRAG -----------------------------------
// The reported bug: pressing the button on the chart and moving sideways zoomed it. On a
// trackpad the same two fingers that move the chart also report as a scroll/pinch, so those
// wheel events arrive while the pointer is down — with ctrl set, because that is how the OS
// reports the magnify half of the gesture. Measured in the browser: they still carry
// `buttons: 0`, so the drag flag is the signal that catches them.
const dragEl = makeElement();
const dragChart = makeChart(900, 1000);
ChartZoom.bind(dragEl, function () { return { chart: dragChart, barCount: 1092 }; });

const pinchMid = wheel(-600, true);
dragEl.fire("mousedown", {});
const beforeMid = dragChart.rec.sets.length;
dragEl.fire("wheel", pinchMid);
const sidewaysMid = wheel(-10, true, 500, -200);
dragEl.fire("wheel", sidewaysMid);
out.drag = {
  duringDragSets: dragChart.rec.sets.length - beforeMid,
  duringDragRange: { from: dragChart.rec.from, to: dragChart.rec.to },
  pinchPrevented: pinchMid.defaultPrevented,
  pinchStopped: pinchMid.stopped,
  sidewaysPrevented: sidewaysMid.defaultPrevented,
  sidewaysStopped: sidewaysMid.stopped,
};
// A drag that ends outside the element must not leave the chart stuck: the flag is released on
// the WINDOW, so a pinch after the button comes up is a pinch again.
const setsBeforeRelease = dragChart.rec.sets.length;
fireWindow("mouseup");
dragEl.fire("wheel", wheel(-600, true));
out.drag.setsAfterRelease = dragChart.rec.sets.length - setsBeforeRelease;
out.drag.spanAfterRelease = span(dragChart.rec);
// ...and one that never gets a mouseup (the window losing focus mid-drag) is released too.
dragEl.fire("mousedown", {});
fireWindow("blur");
const setsBeforeBlur = dragChart.rec.sets.length;
dragEl.fire("wheel", wheel(-600, true));
out.drag.setsAfterBlur = dragChart.rec.sets.length - setsBeforeBlur;

// ---- 5. a ctrl+wheel that is mostly SIDEWAYS -------------------------------
// Two fingers travelling together is a MOVE. It is still swallowed (an unswallowed ctrl+wheel
// zooms the whole page), but it is not a zoom: the window is shifted, by the same arithmetic the
// library pans with.
const sideEl = makeElement();
const sideChart = makeChart(900, 1000);
ChartZoom.bind(sideEl, function () { return { chart: sideChart, barCount: 1092 }; });
const sideways = wheel(-20, true, 500, -200);
sideEl.fire("wheel", sideways);
out.sideways = {
  prevented: sideways.defaultPrevented,
  stopped: sideways.stopped,
  range: { from: sideChart.rec.from, to: sideChart.rec.to },
  spanBefore: 100,
  spanAfter: span(sideChart.rec),
  sets: sideChart.rec.sets.length,
};

// a resolver that reports no chart (a disposed/cleared one) must do nothing
ChartZoom.bind(el, function () { return { chart: null, barCount: 0 }; });
second.rec.sets = [];
el.fire("wheel", wheel(600, true));
out.bound.disposedCalls = second.rec.sets.length;

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def zoom_results(tmp_path_factory) -> dict:
    """Run the real chart_zoom.js under node with fake charts/elements."""
    script = tmp_path_factory.mktemp("chartzoom") / "zoom.js"
    # The page's own order and semantics: `window` IS the global object in a
    # browser, so `window.ChartZoom = ...` (what chart_zoom.js does) is what makes
    # the bare `ChartZoom` reference in app.js/report.js resolve. Node has no
    # window, so the stub has to BE the global for the harness to mean anything.
    script.write_text(
        "globalThis.window = globalThis;\n"
        # The drag flag is released on the WINDOW (a drag can end outside the element),
        # so the harness has to be able to fire those too.
        "globalThis.__winHandlers = {};\n"
        "globalThis.addEventListener = function (name, cb) {\n"
        "  (globalThis.__winHandlers[name] = globalThis.__winHandlers[name] || []).push(cb);\n"
        "};\n"
        "globalThis.fireWindow = function (name, ev) {\n"
        "  const list = globalThis.__winHandlers[name] || [];\n"
        "  for (let i = 0; i < list.length; i++) list[i](ev || {});\n"
        "};\n"
        + CHART_ZOOM_JS.read_text(encoding="utf-8")
        + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "chart zoom harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# the clamp
# ---------------------------------------------------------------------------
def span(range_) -> float:
    """The number of bars a clamped range covers."""
    return range_["to"] - range_["from"]


def test_zooming_out_stops_at_the_whole_series(zoom_results):
    got = zoom_results["ranges"]["zoomOutPastEnd"]
    assert got["to"] - got["from"] == 400, "the window never gets wider than the series"


def test_a_window_panned_into_empty_space_keeps_its_position(zoom_results):
    """The reported bug: pinching after dragging the tail of the data to the middle
    of the chart snapped the window back to [0, bars] (looking like "the chart
    resets to fill the width") before it zoomed."""
    got = zoom_results["ranges"]["pannedIntoWhitespace"]
    # 278.8 was where the pan left it; the pinch only widens the window (0.5 bars
    # of drift come from the anchor arithmetic, not from a snap-back to 0).
    assert got["from"] == pytest.approx(278.3, abs=0.05), "it must zoom from where it was left"
    assert got["from"] > 200, "...and must not snap back to the start of the data"
    assert got["to"] - got["from"] == 1092, "...and still stop at the whole series"


def test_no_single_event_can_take_the_view_somewhere_drastic(zoom_results):
    """The reported bug: panning with two fingers collapsed the chart onto one candle.

    A trackpad reports a pinch as a STREAM of wheel events, and a fast flick (or a coarse wheel)
    arrives as ONE event with a delta in the hundreds: unbounded, that event zoomed the session
    chart from 72 bars to 9 in a single frame — which reads as "it suddenly zoomed in to the
    maximum" and loses the stretch the reader was looking at. Measured in the browser before the
    fix: a deltaY of -400 took [23, 95] to [25, 34].
    """
    hard = zoom_results["ranges"]["zoomInHard"]
    floor = zoom_results["flags"]["maxStepFactor"]

    assert span(hard) >= 100 / floor - 0.5, f"one event must not shrink the window past {floor:.2f}x"


def test_a_STREAM_of_events_still_reaches_the_floor(zoom_results):
    """Capping the step must not cost the reader the zoom itself: forty events get there, which
    is a fraction of a second of pinching."""
    stepped = zoom_results["ranges"]["zoomInRepeated"]

    assert span(stepped) == 1, "the floor is still reachable"
    assert zoom_results["ranges"]["zoomInHard"]["to"] - zoom_results["ranges"]["zoomInHard"]["from"] > 1


def test_zooming_in_stops_at_one_bar(zoom_results):
    held = zoom_results["ranges"]["zoomInFromRight"]

    assert held["to"] - held["from"] == 1
    assert held["to"] == 400, "the bar under the pointer is the one that stays"


def test_the_bar_under_the_pointer_stays_put(zoom_results):
    left = zoom_results["ranges"]["anchorLeft"]
    right = zoom_results["ranges"]["anchorRight"]
    # Pinching at the left edge keeps the left edge (0) fixed; at the right edge
    # the window moves left instead and keeps its right edge at 100.
    assert left["from"] == 0
    assert right["to"] == 100


def test_a_chart_with_no_known_bar_count_is_still_bounded(zoom_results):
    """Before the data arrives the zoom must not run away, and must not lock."""
    got = zoom_results["ranges"]["unknownBars"]
    assert 0 < got["to"] - got["from"] <= 1000


def test_a_degenerate_range_is_ignored(zoom_results):
    assert zoom_results["ranges"]["degenerate"] is None


# ---------------------------------------------------------------------------
# the binding
# ---------------------------------------------------------------------------
def test_a_plain_wheel_never_zooms(zoom_results):
    assert zoom_results["flags"]["plainWheelZoomed"] == 0
    assert zoom_results["flags"]["plainWheelPrevented"] is False
    # ...and it is not stopped either: panning a plain wheel is the library's job and it keeps it.
    assert zoom_results["flags"]["plainWheelStopped"] is False


def test_a_pinch_zooms_and_keeps_the_page_still(zoom_results):
    assert zoom_results["flags"]["afterPinchOut"] == 1
    assert zoom_results["flags"]["pinchPrevented"] is True


def test_a_pinch_is_taken_out_of_the_librarys_reach(zoom_results):
    """The second zoomer, and the reason this listener is in the CAPTURE phase.

    The library scales ANY ctrl+wheel whatever ``handleScale`` says — measured in the browser on the
    session chart: with ``pinch: false`` a PURE sideways ctrl+wheel still zoomed 1.6x, and a single
    vertical one 1.58x where this module's own step is 1.42x, i.e. both implementations were
    scaling it. Stopping the event where it arrives is what leaves exactly one zoomer.
    """
    assert zoom_results["flags"]["pinchStopped"] is True, "the library must not see the pinch"
    assert zoom_results["sideways"]["stopped"] is True


def test_a_mostly_SIDEWAYS_pinch_MOVES_the_chart(zoom_results):
    """Two fingers travelling together is a MOVE, not a zoom.

    With ctrl stamped on it (which is what an OS that reads the same gesture as a magnify sends) it
    used to zoom instead, which is how a reader trying to reach an earlier stretch of the session
    ended up at maximum zoom. The window shifts by the pixels travelled as a share of the pane,
    which is the arithmetic the library pans with — and its WIDTH is untouched.
    """
    got = zoom_results["sideways"]
    assert got["range"] == {"from": 880, "to": 980}, "moved 200px of a 1000px pane over 100 bars"
    assert got["spanAfter"] == got["spanBefore"], "a sideways gesture is not a zoom"
    assert got["sets"] == 1
    assert got["prevented"] is True, "or the browser zooms the whole page instead"


def test_a_wheel_that_belongs_to_a_drag_never_zooms(zoom_results):
    """The reported bug: "when i click and hold the click on top of the chart and move left or
    right it zooms in".

    On a trackpad the two fingers that move the chart also report as a scroll/pinch, so wheel
    events arrive mid-drag with ctrl set. The pointer is already moving the chart, and the events
    are swallowed whole: no zoom, and no second source of movement either.
    """
    got = zoom_results["drag"]
    assert got["duringDragSets"] == 0, "nothing at all was applied to the chart"
    assert got["duringDragRange"] == {"from": 900, "to": 1000}, "and it did not move either"
    assert got["pinchPrevented"] is True
    assert got["pinchStopped"] is True
    assert got["sidewaysPrevented"] is True
    assert got["sidewaysStopped"] is True


def test_a_drag_that_ends_outside_the_chart_does_not_stick(zoom_results):
    """The flag is released on the WINDOW: a drag ended off the element, or a window that lost
    focus mid-drag, must not leave the chart unable to zoom again."""
    got = zoom_results["drag"]
    assert got["setsAfterRelease"] == 1, "the button coming up hands the pinch back"
    assert got["spanAfterRelease"] < 100, "...and it really zoomed"
    assert got["setsAfterBlur"] == 1, "so does a drag the window took focus away from"


def test_pinching_out_holds_at_the_whole_series(zoom_results):
    """The window widens to the length of the series, keeps its centre (the pointer
    stays over the same bar) and then stops changing altogether."""
    held = zoom_results["bound"]["heldOut"]
    assert held["span"] == 1092
    assert abs(held["center"] - 950) < 1, "the view did not move"
    assert held["setsAfterReachingTheLimit"] == 0, "a pinch at the limit must be a no-op"


def test_pinching_in_holds_at_one_bar(zoom_results):
    held = zoom_results["bound"]["heldIn"]
    assert held["span"] == 1
    assert held["setsAfterReachingTheFloor"] == 0


def test_one_listener_per_element_repointed_at_the_current_chart(zoom_results):
    assert zoom_results["bound"]["wheelListeners"] == 1
    # ...plus the one mousedown that arms the drag flag, which must not be re-added on a rebuild.
    assert zoom_results["bound"]["dragListeners"] == 1
    assert zoom_results["bound"]["secondChartCalls"] == 1, "the new chart gets the gesture"
    assert zoom_results["bound"]["firstChartStaysPut"] == 0, "the replaced chart is not touched"


def test_a_chart_that_is_gone_is_not_touched(zoom_results):
    assert zoom_results["bound"]["disposedCalls"] == 0


def test_the_helper_is_a_shared_global():
    """Both pages load it by name, so it must be assigned to `window`."""
    src = CHART_ZOOM_JS.read_text(encoding="utf-8")
    assert "window.ChartZoom = (function ()" in src
    assert "\nconst ChartZoom" not in src, "a lexical global cannot be checked by callers"


def test_every_chart_turns_the_librarys_own_zoom_off():
    """One zoomer, and it is this module.

    A ctrl+wheel is what a trackpad pinch arrives as, and the library scales it itself when
    ``handleScale.pinch`` is left on: TWO implementations met on one gesture, the library's with no
    per-event bound and none of the clamping above, and the second one is what turned a flick into
    "the chart suddenly zoomed in to the maximum". Every chart that binds this module therefore
    turns both of the library's own scale gestures off, and keeps panning.
    """
    assets = CHART_ZOOM_JS.parent
    for name in ("app.js", "report.js"):
        src = (assets / name).read_text(encoding="utf-8")
        charts = src.count("LightweightCharts.createChart(")
        assert charts and src.count("pinch: false") == charts, (
            f"{name}: {charts} charts but {src.count('pinch: false')} turn the library's pinch off"
        )
        assert src.count("mouseWheel: false") >= charts, "and its wheel zoom, as before"
    # The session monitor builds all three of its panes from ONE options helper, so its single
    # declaration is what covers them.
    log_src = (assets / "log.js").read_text(encoding="utf-8")
    assert log_src.count("LightweightCharts.createChart(") == 3
    assert log_src.count("pinch: false") == 1, "declared once, in ``chartOptions``"
    assert "handleScale: { mouseWheel: false, pinch: false" in log_src


def test_every_chart_can_show_its_whole_series():
    """Zooming out stopped at a fortnight instead of the 60 days that were loaded.

    The clamp below allows the whole series, but the LIBRARY silently refuses to
    draw more bars than fit at its own ``minBarSpacing`` (0.5 px per bar by default)
    and applies a NARROWER range than the one it was handed. A 528px plot therefore
    capped out at ~1,056 bars, so 60 days of 5-minute candles (3,191 bars) could
    never be viewed in full however far the user pinched — and because the applied
    range then never matched the range we asked for, the gesture also stopped
    registering as satisfied. Every chart now sets a much denser floor, taken from
    this module so the app's limit and the library's cannot drift apart.
    """
    zoom_src = CHART_ZOOM_JS.read_text(encoding="utf-8")
    assert "MIN_BAR_SPACING: MIN_BAR_SPACING" in zoom_src, "the constant must be exported"
    decl = [ln for ln in zoom_src.splitlines() if "const MIN_BAR_SPACING" in ln][0]
    value = float(decl.split("=")[1].strip().rstrip(";"))
    assert value <= 0.05, "0.5 px per bar (the library default) is what caused the bug"
    # 60 days of 5-minute RTH bars is 3,191, and it must fit a narrow pane.
    assert 500 / value > 3200, f"{value} px/bar cannot fit 3,191 bars into 500px"

    assets = CHART_ZOOM_JS.parent
    for name in ("app.js", "report.js"):
        src = (assets / name).read_text(encoding="utf-8")
        charts = src.count("LightweightCharts.createChart(")
        fixed = src.count("minBarSpacing: ChartZoom.MIN_BAR_SPACING")
        assert charts and charts == fixed, (
            f"{name}: {charts} charts but {fixed} set minBarSpacing — a chart without "
            "it silently caps how far the user can zoom out"
        )
