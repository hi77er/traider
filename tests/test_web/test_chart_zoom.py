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
* one listener per element, re-pointed when a chart is rebuilt on it.
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
    listeners: {},
    addEventListener: function (name, cb) { el.added += 1; el.listeners[name] = cb; },
    getBoundingClientRect: function () { return { left: 0, width: 1000 }; },
    fire: function (ev) { if (el.listeners.wheel) el.listeners.wheel(ev); },
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
      };
    },
  };
  return chart;
}

function wheel(deltaY, ctrlKey, clientX) {
  return {
    ctrlKey: !!ctrlKey,
    deltaY: deltaY,
    clientX: clientX === undefined ? 500 : clientX,
    defaultPrevented: false,
    preventDefault: function () { this.defaultPrevented = true; },
  };
}

function span(r) { return r.to - r.from; }

const out = { ranges: {}, bound: {}, flags: {} };

// ---- 1. pure clamping ------------------------------------------------------
const mid = { from: 0, to: 100 };
out.ranges.zoomOut = ChartZoom.clampRange(mid, 100, 0.5, 400);       // small step out
out.ranges.zoomOutPastEnd = ChartZoom.clampRange({ from: 300, to: 400 }, 600, 0.5, 400);
out.ranges.zoomInHard = ChartZoom.clampRange(mid, -3000, 0.5, 400);
out.ranges.zoomInFromRight = ChartZoom.clampRange({ from: 300, to: 400 }, -3000, 1, 400);
out.ranges.unknownBars = ChartZoom.clampRange(mid, 600, 0.5, 0);
out.ranges.anchorLeft = ChartZoom.clampRange(mid, -100, 0, 400);    // pinch at the left edge
out.ranges.anchorRight = ChartZoom.clampRange(mid, -100, 1, 400);   // ...and at the right one
out.ranges.degenerate = ChartZoom.clampRange({ from: 5, to: 5 }, 60, 0.5, 400);
// The reported bug: the window was panned into the empty space after the last bar
// (bars 1092), and one pinch snapped it back to [0, 1092] before zooming.
out.ranges.pannedIntoWhitespace = ChartZoom.clampRange({ from: 278.8, to: 1369.8 }, 60, 0.5, 1092);

// ---- 2. a bound element: plain wheel vs pinch -------------------------------
const el = makeElement();
const chart = makeChart(900, 1000); // a window inside the series, as on screen
ChartZoom.bind(el, function () { return { chart: chart, barCount: 1092 }; });

const plain = wheel(600, false);
el.fire(plain);
out.flags.plainWheelZoomed = chart.rec.sets.length;
out.flags.plainWheelPrevented = plain.defaultPrevented;

const pinchOut = wheel(60, true);
el.fire(pinchOut);
out.flags.pinchPrevented = pinchOut.defaultPrevented;
out.flags.afterPinchOut = chart.rec.sets.length;

// keep pinching out: once the window is as wide as the series this changes nothing
for (let i = 0; i < 12; i++) el.fire(wheel(60, true));
const setsAtLimit = chart.rec.sets.length;
for (let i = 0; i < 20; i++) el.fire(wheel(60, true));
out.bound.heldOut = {
  span: span(chart.rec),
  center: (chart.rec.from + chart.rec.to) / 2,
  sets: chart.rec.sets.length,
  setsAfterReachingTheLimit: chart.rec.sets.length - setsAtLimit,
};

// pinch in hard: stops at a single bar, and then stops doing anything at all
for (let i = 0; i < 40; i++) el.fire(wheel(-600, true));
const setsAtFloor = chart.rec.sets.length;
for (let i = 0; i < 20; i++) el.fire(wheel(-600, true));
out.bound.heldIn = {
  span: span(chart.rec),
  sets: chart.rec.sets.length,
  setsAfterReachingTheFloor: chart.rec.sets.length - setsAtFloor,
};

// ---- 3. one listener per element, re-pointed at the current chart -----------
const second = makeChart(10, 30); // a window inside its own data, so a pinch moves
const firstSetsBefore = chart.rec.sets.length;
ChartZoom.bind(el, function () { return { chart: second, barCount: 50 }; });
out.bound.listeners = el.added;
second.rec.sets = [];
el.fire(wheel(600, true));
out.bound.secondChartCalls = second.rec.sets.length;
out.bound.firstChartStaysPut = chart.rec.sets.length - firstSetsBefore;

// a resolver that reports no chart (a disposed/cleared one) must do nothing
ChartZoom.bind(el, function () { return { chart: null, barCount: 0 }; });
second.rec.sets = [];
el.fire(wheel(600, true));
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


def test_zooming_in_stops_at_one_bar(zoom_results):
    assert zoom_results["ranges"]["zoomInHard"]["to"] - zoom_results["ranges"]["zoomInHard"]["from"] == 1
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


def test_a_pinch_zooms_and_keeps_the_page_still(zoom_results):
    assert zoom_results["flags"]["afterPinchOut"] == 1
    assert zoom_results["flags"]["pinchPrevented"] is True


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
    assert zoom_results["bound"]["listeners"] == 1
    assert zoom_results["bound"]["secondChartCalls"] == 1, "the new chart gets the gesture"
    assert zoom_results["bound"]["firstChartStaysPut"] == 0, "the replaced chart is not touched"


def test_a_chart_that_is_gone_is_not_touched(zoom_results):
    assert zoom_results["bound"]["disposedCalls"] == 0


def test_the_helper_is_a_shared_global():
    """Both pages load it by name, so it must be assigned to `window`."""
    src = CHART_ZOOM_JS.read_text(encoding="utf-8")
    assert "window.ChartZoom = (function ()" in src
    assert "\nconst ChartZoom" not in src, "a lexical global cannot be checked by callers"
