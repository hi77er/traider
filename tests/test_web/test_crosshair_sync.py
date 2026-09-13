"""Behavioural tests for the dashboard crosshair sync.

`app.js` links the crosshair across the price chart, every oscillator pane and
the backtest equity curve: hovering any one of them places a vertical time line
(and a value line) on all the others. lightweight-charts only draws a crosshair
on the chart under the pointer, so the others are positioned by hand with
`setCrosshairPosition`.

The tricky part is the re-entrancy guard. The first cut remembered, per chart,
the time it had just pushed and swallowed the next event at that same time —
assuming the library would echo the placement back. It does not: verified
against lightweight-charts 4.1.3, `setCrosshairPosition` emits no crosshair
event at all, so the marker was never cleared and a genuine hover on a pane,
at the bar the mouse was already resting on, produced NO sync whatsoever.

The logic therefore cannot be trusted to string assertions. These tests lift the
real sync block out of `app.js`, run it under node against fake charts that
reproduce the library's actual event semantics, and assert the resulting calls.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "const _crosshairAnchors"
END_FUNCTION = "function _onCrosshairMove"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the dashboard sync"
)


def extract_sync_block() -> str:
    """The real crosshair-sync source, lifted verbatim out of `app.js`."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    fn = src.index(END_FUNCTION, start)
    end = src.index("\n}\n", fn) + 3
    return src[start:end]


HARNESS = r"""
// ---- fake charts with lightweight-charts 4.1.3 semantics ------------------
// `setCrosshairPosition` deliberately does NOT notify the subscribers: that is
// what the real library does, and assuming otherwise is what caused the bug.
function makeChart(name) {
  const rec = { name: name, sets: [], clears: 0, subs: [] };
  const chart = {
    rec: rec,
    subscribeCrosshairMove: function (cb) { rec.subs.push(cb); },
    setCrosshairPosition: function (price, time) { rec.sets.push({ price: price, time: time }); },
    clearCrosshairPosition: function () { rec.clears += 1; },
  };
  // what the library does when the pointer really moves over this chart
  chart.hover = function (time) { rec.subs.forEach(function (cb) { cb({ time: time }); }); };
  // ...and when the pointer leaves it (no time in the payload)
  chart.leave = function () { rec.subs.forEach(function (cb) { cb({ time: undefined }); }); };
  return chart;
}

function mapOf(pairs) {
  const m = new Map();
  pairs.forEach(function (p) { m.set(p[0], p[1]); });
  return m;
}

const SERIES = { id: 'series' };
const main = makeChart('main');
const paneA = makeChart('paneA');
const paneB = makeChart('paneB');
const bt = makeChart('bt');
const ALL = [main, paneA, paneB, bt];

function snapshot() {
  const out = {};
  ALL.forEach(function (c) {
    out[c.rec.name] = { sets: c.rec.sets, clears: c.rec.clears };
  });
  return out;
}
function reset() { ALL.forEach(function (c) { c.rec.sets = []; c.rec.clears = 0; }); }
// The guard expires on the next task, exactly as it does between two real
// mousemove events, so each simulated hover must be separated by a timeout.
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

(async function () {
  const out = {};

  registerCrosshairAnchor(main, SERIES, mapOf([[1, 10], [2, 20], [3, 30]]), 10);
  registerCrosshairAnchor(paneA, SERIES, mapOf([[1, 100], [2, 200], [3, 300]]), 100);
  registerCrosshairAnchor(paneB, SERIES, mapOf([[2, 7], [3, 9]]), 7);
  registerCrosshairAnchor(bt, SERIES, mapOf([[1, 1], [3, 1.5]]), 1);
  out.anchorCount = _crosshairAnchors.size;

  // 1. hovering the price chart pushes to every satellite, each at its OWN
  //    scale's value for that instant (bt is sparse: nearest previous is t=1).
  reset();
  main.hover(2);
  out.hoverMain = snapshot();
  await tick();

  // 2. the mouse resting on the same bar repeats the same time. Every repeat
  //    must still push; swallowing it froze the crosshair on the first chart.
  reset();
  main.hover(2);
  out.hoverMainAgainSameTime = snapshot();
  await tick();

  // 3. hovering a PANE must push to the price chart and the other panes. This
  //    is the case the old echo marker silently dropped.
  reset();
  paneA.hover(2);
  out.hoverPaneA = snapshot();
  await tick();

  // 4. ...at a different bar, and from the other pane.
  reset();
  paneA.hover(3);
  out.hoverPaneAOtherBar = snapshot();
  await tick();

  reset();
  paneB.hover(3);
  out.hoverPaneB = snapshot();
  await tick();

  // 5. leaving a chart clears every other chart's line (and not its own).
  reset();
  main.leave();
  out.leaveMain = snapshot();
  await tick();

  // 6. a chart with no usable data for that instant falls back to its first
  //    real value rather than dropping the vertical line.
  reset();
  main.hover(1);
  out.hoverEarliest = snapshot();
  await tick();

  // 7. a disposed chart stops being synced.
  forgetCrosshairAnchor(paneB);
  reset();
  main.hover(2);
  out.afterForget = snapshot();
  out.anchorCountAfterForget = _crosshairAnchors.size;

  // 8. the sparse-data lookup itself.
  const sparse = _crosshairValues([{ time: 1, value: 1 }, { time: 3, value: 1.5 }]);
  const dense = _crosshairValues([
    { time: 1, value: 5 }, null, { time: 2, value: null }, { time: 3, value: 7 },
  ]);
  out.lookup = {
    exact: _crosshairValueAt({ values: sparse.values, times: sparse.times, fallback: sparse.fallback }, 3),
    between: _crosshairValueAt({ values: sparse.values, times: sparse.times, fallback: sparse.fallback }, 2),
    beforeFirst: _crosshairValueAt({ values: sparse.values, times: sparse.times, fallback: sparse.fallback }, 0),
    afterLast: _crosshairValueAt({ values: sparse.values, times: sparse.times, fallback: sparse.fallback }, 99),
    dropsNulls: dense.times.length,
    firstValueFallback: dense.fallback,
  };

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def sync_results(tmp_path_factory) -> dict:
    """Run the real sync block under node and return what it did."""
    script = tmp_path_factory.mktemp("crosshair") / "sync.js"
    script.write_text(extract_sync_block() + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "crosshair sync harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def _sets(results: dict, hover: str) -> dict:
    return {
        name: rec["sets"]
        for name, rec in results[hover].items()
        if rec["sets"]
    }


def test_hovering_price_chart_syncs_all_satellites(sync_results):
    pushed = _sets(sync_results, "hoverMain")
    assert set(pushed) == {"paneA", "paneB", "bt"}
    # Each chart keeps its OWN scale: RSI 200, MACD hist 7, equity 1 at t=2.
    assert pushed["paneA"] == [{"price": 200, "time": 2}]
    assert pushed["paneB"] == [{"price": 7, "time": 2}]
    # Sparse equity curve resolves to the nearest point at or before t=2.
    assert pushed["bt"] == [{"price": 1, "time": 2}]
    # The hovered chart is left to track the mouse itself.
    assert sync_results["hoverMain"]["main"]["sets"] == []


def test_hovering_the_same_bar_twice_still_pushes(sync_results):
    """Regression: the old echo marker swallowed repeats at one time."""
    assert _sets(sync_results, "hoverMainAgainSameTime") == _sets(sync_results, "hoverMain")


def test_hovering_a_pane_syncs_the_price_chart_and_other_panes(sync_results):
    """Regression: hovering a pane used to push nothing at all."""
    pushed = _sets(sync_results, "hoverPaneA")
    assert set(pushed) == {"main", "paneB", "bt"}
    assert pushed["main"] == [{"price": 20, "time": 2}]
    assert pushed["paneB"] == [{"price": 7, "time": 2}]
    assert sync_results["hoverPaneA"]["paneA"]["sets"] == []
    # The rest of the panes are pushed as well, not just the price chart.
    assert len(pushed) == 3


def test_every_satellite_gets_the_hovered_time(sync_results):
    hovered_time = {
        "hoverMain": 2,
        "hoverPaneA": 2,
        "hoverPaneAOtherBar": 3,
        "hoverPaneB": 3,
    }
    for hover, expected in hovered_time.items():
        pushed = _sets(sync_results, hover)
        assert pushed, f"{hover} pushed nothing"
        for name, calls in pushed.items():
            assert [c["time"] for c in calls] == [expected], f"{hover}/{name} pushed {calls}"
            assert all(c["price"] is not None for c in calls)


def test_hovering_a_pane_at_another_bar_follows_the_mouse(sync_results):
    pushed = _sets(sync_results, "hoverPaneAOtherBar")
    assert pushed["main"] == [{"price": 30, "time": 3}]
    assert pushed["paneB"] == [{"price": 9, "time": 3}]


def test_the_other_pane_drives_the_sync_too(sync_results):
    pushed = _sets(sync_results, "hoverPaneB")
    assert set(pushed) == {"main", "paneA", "bt"}
    assert pushed["main"] == [{"price": 30, "time": 3}]
    assert pushed["paneA"] == [{"price": 300, "time": 3}]
    assert sync_results["hoverPaneB"]["paneB"]["sets"] == []


def test_leaving_a_chart_clears_the_other_crosshairs(sync_results):
    cleared = {n: r["clears"] for n, r in sync_results["leaveMain"].items() if r["clears"]}
    assert set(cleared) == {"paneA", "paneB", "bt"}
    # The chart under the pointer clears itself.
    assert sync_results["leaveMain"]["main"]["sets"] == []


def test_earliest_bar_still_produces_a_vertical_line(sync_results):
    pushed = _sets(sync_results, "hoverEarliest")
    assert pushed["paneA"] == [{"price": 100, "time": 1}]
    # paneB has no point at t=1 at all: it must fall back, not skip the update.
    assert pushed["paneB"] == [{"price": 7, "time": 1}]


def test_forgotten_chart_is_no_longer_synced(sync_results):
    assert sync_results["anchorCount"] == 4
    assert sync_results["anchorCountAfterForget"] == 3
    pushed = _sets(sync_results, "afterForget")
    assert set(pushed) == {"paneA", "bt"}


def test_sparse_data_resolves_to_the_preceding_point(sync_results):
    lookup = sync_results["lookup"]
    assert lookup["exact"] == 1.5
    assert lookup["between"] == 1  # nearest point at or before t=2
    assert lookup["beforeFirst"] == 1  # falls back to the first real value
    assert lookup["afterLast"] == 1.5
    assert lookup["dropsNulls"] == 2  # null entries never enter the time axis
    assert lookup["firstValueFallback"] == 5


def test_every_chart_removal_forgets_its_crosshair_anchor():
    """A disposed chart left in the map is pushed to on every hover forever."""
    src = APP_JS.read_text(encoding="utf-8")
    lines = src.splitlines()
    unpaired = [
        (i + 1, line.strip())
        for i, line in enumerate(lines)
        if ".remove()" in line
        and not any("forgetCrosshairAnchor" in prev for prev in lines[max(0, i - 3):i])
    ]
    assert unpaired == []


def test_crosshair_guard_does_not_key_off_the_pushed_time():
    """The guard must not compare the pushed time: that reintroduces the bug."""
    body = extract_sync_block()
    guard = body[body.index(END_FUNCTION):]
    assert "__xcEcho" not in body
    assert "_crosshairSyncing" in guard
    # Guard against a future edit re-adding a time comparison in the early return.
    early = guard[:guard.index("const time =")]
    assert "param.time ===" not in early


# --------------------------------------------------------------------------
# The report page links its equity and drawdown charts the same way, so the
# same behaviours are asserted against that (independently written) copy.
# --------------------------------------------------------------------------

REPORT_JS = APP_JS.parent / "report.js"

REPORT_START = "const crossAnchors"
REPORT_END = "function onCrosshairMove"


def extract_report_sync_block() -> str:
    """The report page's sync source, lifted verbatim out of `report.js`.

    `report.js` is an IIFE, so its members are indented by two spaces; the first
    lone `  }` after the last helper closes `onCrosshairMove`.
    """
    src = REPORT_JS.read_text(encoding="utf-8")
    start = src.index(REPORT_START)
    fn = src.index(REPORT_END, start)
    end = src.index("\n  }\n", fn) + 4
    return src[start:end]


REPORT_HARNESS = r"""
function makeChart(name) {
  const rec = { name: name, sets: [], clears: 0, subs: [] };
  const chart = {
    rec: rec,
    subscribeCrosshairMove: function (cb) { rec.subs.push(cb); },
    setCrosshairPosition: function (price, time) { rec.sets.push({ price: price, time: time }); },
    clearCrosshairPosition: function () { rec.clears += 1; },
  };
  chart.hover = function (time) { rec.subs.forEach(function (cb) { cb({ time: time }); }); };
  chart.leave = function () { rec.subs.forEach(function (cb) { cb({ time: undefined }); }); };
  return chart;
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

// report.js registers RAW points; it builds the map/times itself.
const SERIES = { id: 'series' };
const equity = makeChart('equity');
const draw = makeChart('drawdown');
const ALL = [equity, draw];
let _anchorCount = 0;

function snapshot() {
  const out = {};
  ALL.forEach(function (c) {
    if (c.rec.sets.length || c.rec.clears) {
      out[c.rec.name] = { sets: c.rec.sets, clears: c.rec.clears };
    }
  });
  return out;
}
function reset() { ALL.forEach(function (c) { c.rec.sets = []; c.rec.clears = 0; }); }

(async function () {
  const out = {};
  registerCrosshair(equity, SERIES, [
    { time: 1, value: 1.0 }, { time: 2, value: 1.1 }, { time: 3, value: 1.2 },
  ]);
  registerCrosshair(draw, SERIES, [
    { time: 2, value: -4.5 }, { time: 3, value: -7.25 },
  ]);
  _anchorCount = crossAnchors.size;

  reset(); equity.hover(3);
  out.hoverEquity = snapshot();
  await tick();

  reset(); equity.hover(3); // stationary mouse: the same time again
  out.hoverEquityAgain = snapshot();
  await tick();

  reset(); draw.hover(3);
  out.hoverDrawdown = snapshot();
  await tick();

  reset(); draw.leave();
  out.leaveDrawdown = snapshot();
  await tick();

  // The drawdown chart only starts at t=2, so an earlier hover must fall back.
  reset(); equity.hover(1);
  out.hoverBeforeDrawdownStarts = snapshot();

  // Nearest-previous lookup, exercised through the report's own helper.
  const anchor = { values: new Map([[2, -4.5], [3, -7.25]]), times: [2, 3], fallback: -4.5 };
  out.lookup = {
    exact: crosshairValueAt(anchor, 3),
    between: crosshairValueAt(anchor, 2),
    beforeFirst: crosshairValueAt(anchor, 0),
    afterLast: crosshairValueAt(anchor, 9),
  };

  out.anchorCount = _anchorCount;
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def report_results(tmp_path_factory) -> dict:
    script = tmp_path_factory.mktemp("report-crosshair") / "sync.js"
    script.write_text(extract_report_sync_block() + REPORT_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "report crosshair harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_report_anchors_both_charts(report_results):
    assert report_results["anchorCount"] == 2


def test_report_hovering_the_equity_chart_pushes_the_drawdown_value(report_results):
    pushed = report_results["hoverEquity"]
    assert list(pushed) == ["drawdown"]
    assert pushed["drawdown"]["sets"] == [{"price": -7.25, "time": 3}]


def test_report_repeated_hover_on_one_bar_still_pushes(report_results):
    assert report_results["hoverEquityAgain"] == report_results["hoverEquity"]


def test_report_hovering_the_drawdown_chart_pushes_back_to_equity(report_results):
    pushed = report_results["hoverDrawdown"]
    assert list(pushed) == ["equity"]
    assert pushed["equity"]["sets"] == [{"price": 1.2, "time": 3}]


def test_report_leaving_a_chart_clears_the_other(report_results):
    left = report_results["leaveDrawdown"]
    assert left["equity"]["clears"] == 1
    assert "drawdown" not in left


def test_report_sparse_chart_falls_back_to_its_first_value(report_results):
    pushed = report_results["hoverBeforeDrawdownStarts"]
    assert pushed["drawdown"]["sets"] == [{"price": -4.5, "time": 1}]


def test_report_sparse_lookup_resolves_to_the_preceding_point(report_results):
    lookup = report_results["lookup"]
    assert lookup["exact"] == -7.25
    assert lookup["between"] == -4.5
    assert lookup["beforeFirst"] == -4.5
    assert lookup["afterLast"] == -7.25


def test_report_charts_use_a_free_floating_crosshair():
    src = REPORT_JS.read_text(encoding="utf-8")
    assert "crosshair: { mode: LightweightCharts.CrosshairMode.Normal }" in src


def test_report_clears_anchors_before_disposing_charts():
    src = REPORT_JS.read_text(encoding="utf-8")
    body = src[src.index("function clearCharts()"):src.index("/* ---------- loading")]
    delete_at = body.index("crossAnchors.delete(c)")
    remove_at = body.index("c.remove()")
    assert delete_at < remove_at


def test_report_guard_does_not_key_off_the_pushed_time():
    body = extract_report_sync_block()
    guard = body[body.index(REPORT_END):]
    early = guard[:guard.index("const time =")]
    assert "param.time ===" not in early
    assert "crossSyncing" in guard


def test_both_pages_register_every_chart_they_create():
    """Three creation sites in app.js, two in report.js — all must be anchored."""
    app = APP_JS.read_text(encoding="utf-8")
    assert app.count("registerCrosshairAnchor(") == 1 + 3  # def + 3 call sites
    report = REPORT_JS.read_text(encoding="utf-8")
    assert report.count("registerCrosshair(") == 1 + 2  # def + equity + drawdown

