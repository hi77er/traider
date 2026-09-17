"""Behavioural tests for the chart range sync (``app.js``).

The price chart, every indicator pane drawn separately and the backtest equity
curve must show the SAME PERIOD at the same zoom: moving or zooming any of them
moves all the others. The real block is lifted out of ``app.js`` and run under
node against fake charts, so the assertions are about what each chart is told to
show, not about how the functions are written.

What is pinned — each of these was a reported bug:

* **the mapping is exact for every kind of series.** An indicator's series is the
  price series minus a prefix (RSI drops its warm-up rows), so its bar 0 is the
  price chart's bar N. The backtest equity curve is a *subsample* of the run (the
  panel thins it to 600 points, then appends the run's exact final value), so its
  bars are 2, 2, 2, … price bars apart and finally 1 — no single offset, and not
  even a single step. Every chart is written to the same BARS, not to the same
  indices, and a series whose bars are not all found is not synced at all;
* **a satellite the user touches drives the price chart.** Dragging an indicator
  pane left moved the pane and left the price chart standing still;
* **the empty space is part of the view.** Syncing by TIME range could not express
  it — the library clamps such a request to the data — so a pane dragged past the
  end of its series, or a price chart scrolled into whitespace, desynchronised the
  others. A range outside the bars must survive the trip unchanged;
* **nothing loops.** A chart we just pushed to must not push back;
* **a layout emission is not a gesture.** A pane's own first-paint range must not
  move the price chart;
* **a series whose bars are not all present is not synced at all**, rather than
  being moved to guessed bars.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "/* ---------- Every chart shows the same bars ----------"
END_MARKER = "/* ---------- Crosshair sync (vertical time line"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the chart sync"
)


def extract_range_sync_block() -> str:
    """The real range-sync source, lifted verbatim out of `app.js`."""
    src = APP_JS.read_text(encoding="utf-8")
    return src[src.index(START_MARKER):src.index(END_MARKER)]


HARNESS = r"""
// ---- the world the lifted block expects -------------------------------------
const state = { chart: null, chartTimes: [], oscCharts: [] };
let btChart = null; // only the equity-curve scenarios set this
globalThis.requestAnimationFrame = (fn) => fn(); // inline: nothing is timing-dependent

function makeChart(name) {
  const rec = { name: name, subs: [], pushes: [], from: 0, to: 0 };
  const scale = {
    getVisibleLogicalRange: () => ({ from: rec.from, to: rec.to }),
    setVisibleLogicalRange: (r) => {
      rec.pushes.push({ from: r.from, to: r.to });
      rec.from = r.from;
      rec.to = r.to;
    },
    subscribeVisibleLogicalRangeChange: (cb) => { rec.subs.push(cb); },
    unsubscribeVisibleLogicalRangeChange: (cb) => {
      rec.subs = rec.subs.filter((s) => s !== cb);
    },
  };
  // What the library does when a range is APPLIED: set it, then tell subscribers.
  // `emit` plays both halves, so a push this code makes comes back the same way.
  rec.emit = (from, to) => {
    rec.from = from;
    rec.to = to;
    rec.subs.forEach((cb) => cb({ from: from, to: to }));
  };
  rec.clear = () => { rec.pushes = []; };
  return { rec: rec, timeScale: () => scale };
}

// A fake DOM element, so a gesture can be fired exactly like the browser would.
function makeEl() {
  const listeners = {};
  return {
    addEventListener: (name, cb) => { listeners[name] = cb; },
    fire: (name) => { if (listeners[name]) listeners[name](); },
  };
}

// The price chart has 20 bars. RSI (period 3) starts at bar 3 and momentum
// (period 5) at bar 5 — the "series is the price series minus a prefix" shape —
// and one series shares no bar with the price chart at all.
const priceTimes = [];
for (let i = 0; i < 20; i++) priceTimes.push('t' + i);
const rsiTimes = priceTimes.slice(3);
const momTimes = priceTimes.slice(5);
const foreignTimes = ['other-a', 'other-b']; // shares no bar with the price chart
// The equity curve the panel draws: the run thinned to 600 points (every 2nd bar
// here) with the run's exact final value appended after the subsample.
const curveTimes = [];
for (let i = 0; i < 19; i += 2) curveTimes.push(priceTimes[i]); // 0, 2, ... 18
curveTimes.push(priceTimes[19]);                               // ...appended: 19

const price = makeChart('price');
const rsi = makeChart('rsi');
const mom = makeChart('mom');
const foreign = makeChart('foreign');
const curve = makeChart('curve');
state.chart = price;
state.chartTimes = priceTimes;
state.oscCharts = [rsi, mom, foreign]; // the curve joins through btChart, as in app.js

const rsiEl = makeEl();
const momEl = makeEl();
const curveEl = makeEl();
watchPaneInteraction(rsi, rsiEl);
watchPaneInteraction(mom, momEl);
watchPaneInteraction(curve, curveEl);

const ALL = [price, rsi, mom, foreign, curve];
const out = { placements: {}, scenarios: {} };
const snapshot = () => ALL.map((c) => ({ chart: c.rec.name, from: c.rec.from, to: c.rec.to }));

// ---- every satellite adopts the price chart's view, on its own bars ---------
price.rec.from = 2;
price.rec.to = 12;
subscribeMainToOscTime();
registerRangeSync(rsi, rsiTimes);
registerRangeSync(mom, momTimes);
registerRangeSync(foreign, foreignTimes);
out.placements = {
  price: _rangeMaps.has(price),
  rsi: _rangeMaps.get(rsi)[0],
  mom: _rangeMaps.get(mom)[0],
  foreign: _rangeMaps.has(foreign) ? 'registered' : null,
};
out.scenarios.adopted = snapshot();

// ---- the user drags an indicator pane ---------------------------------------
ALL.forEach((c) => c.rec.clear());
rsiEl.fire('pointermove');               // a gesture, as the browser reports it
rsi.rec.emit(1, 6);                      // the pane moves; the price chart is 3 bars behind
out.scenarios.paneDrags = {
  charts: snapshot(),
  pricePushes: price.rec.pushes.length,
  momPushes: mom.rec.pushes.length,
};

// ---- the price chart drives in the other direction (and is never pushed back) -
ALL.forEach((c) => c.rec.clear());
price.rec.emit(4, 9);
out.scenarios.priceDrags = { charts: snapshot(), echoes: price.rec.pushes.length };

// ---- the empty space before the first bar is part of the view ---------------
ALL.forEach((c) => c.rec.clear());
rsi.rec.emit(-2, 3);
out.scenarios.whitespace = { charts: snapshot(), rsiKept: { from: rsi.rec.from, to: rsi.rec.to } };

// ---- a pane's own first paint is not a gesture ------------------------------
ALL.forEach((c) => c.rec.clear());
mom.rec.emit(0, 5);                      // momEl never fired: nobody touched it
out.scenarios.layoutEmission = { charts: snapshot(), pricePushes: price.rec.pushes.length };

// ---- the equity curve: a SUBSAMPLED series ---------------------------------
// The regression: its bars are NOT a constant number of price bars apart, so a
// single offset could not describe it and the curve was silently left out of the
// sync — dragging the price chart never moved it.
btChart = curve;
price.rec.from = 0;
price.rec.to = 18;
ALL.forEach((c) => c.rec.clear());
registerRangeSync(curve, curveTimes);
out.scenarios.subsampled = {
  placements: _rangeMaps.get(curve),
  charts: snapshot(),
};

// ---- ...and the curve drives the others when the user drags it --------------
// Its own bars 5..9 are price bars 10..18, so that is what everything shows.
ALL.forEach((c) => c.rec.clear());
curveEl.fire('pointerdown');
curve.rec.emit(5, 9);
out.scenarios.subsampledDrag = { charts: snapshot(), pricePushes: price.rec.pushes.length };

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def sync_results(tmp_path_factory) -> dict:
    script = tmp_path_factory.mktemp("rangesync") / "sync.js"
    script.write_text(extract_range_sync_block() + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "range sync harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def _at(rows, name):
    return [r for r in rows if r["chart"] == name][0]


def test_each_series_is_placed_on_the_price_charts_bars(sync_results):
    """RSI starts 3 bars in, momentum 5 — derived from the bars, not assumed."""
    placed = sync_results["placements"]
    assert placed["price"] is True, "the price chart is the reference for the others"
    assert placed["rsi"] == 3
    assert placed["mom"] == 5


def test_a_series_sharing_no_bar_with_the_price_chart_is_not_registered(sync_results):
    assert sync_results["placements"]["foreign"] is None, "nothing to sync it against"


def test_satellites_adopt_the_price_charts_view(sync_results):
    rows = sync_results["scenarios"]["adopted"]
    assert (_at(rows, "price")["from"], _at(rows, "price")["to"]) == (2, 12)
    assert (_at(rows, "rsi")["from"], _at(rows, "rsi")["to"]) == (-1, 9)  # 3 bars behind
    assert (_at(rows, "mom")["from"], _at(rows, "mom")["to"]) == (-3, 7)  # 5 bars behind


def test_dragging_an_indicator_pane_moves_the_price_chart(sync_results):
    """The reported bug: the pane moved and the price chart stayed put."""
    scenario = sync_results["scenarios"]["paneDrags"]
    rows = scenario["charts"]
    assert (_at(rows, "price")["from"], _at(rows, "price")["to"]) == (4, 9), "1+3 .. 6+3"
    assert scenario["pricePushes"] == 1
    # ...and a pane that was NOT touched still follows onto the same bars.
    assert (_at(rows, "mom")["from"], _at(rows, "mom")["to"]) == (-1, 4), "1+3-5 .. 6+3-5"
    assert scenario["momPushes"] == 1


def test_moving_the_price_chart_moves_the_panes(sync_results):
    scenario = sync_results["scenarios"]["priceDrags"]
    rows = scenario["charts"]
    assert (_at(rows, "rsi")["from"], _at(rows, "rsi")["to"]) == (1, 6)
    assert (_at(rows, "mom")["from"], _at(rows, "mom")["to"]) == (-1, 4)
    assert scenario["echoes"] == 0, "the price chart must never be pushed back to"


def test_the_empty_space_is_carried_over(sync_results):
    """A range outside the bars must survive the trip — a time range could not."""
    scenario = sync_results["scenarios"]["whitespace"]
    rows = scenario["charts"]
    assert (_at(rows, "price")["from"], _at(rows, "price")["to"]) == (1, 6), "-2+3 .. 3+3"
    assert scenario["rsiKept"] == {"from": -2, "to": 3}, "the pane keeps its whitespace"


def test_a_layout_emission_does_not_move_anything(sync_results):
    scenario = sync_results["scenarios"]["layoutEmission"]
    rows = scenario["charts"]
    assert scenario["pricePushes"] == 0, "an untouched pane must not drive the price chart"
    assert (_at(rows, "price")["from"], _at(rows, "price")["to"]) == (1, 6), "still where it was"


def test_the_equity_curve_is_linked_like_every_other_chart(sync_results):
    """The reported bug: the equity curve was the one chart left out.

    The panel's curve is a subsample of the run with the final value appended, so
    its bars are 2, 2, …, 2, 1 price bars apart — it cannot be described by an
    offset, and requiring one dropped it from the sync entirely.
    """
    scenario = sync_results["scenarios"]["subsampled"]
    assert scenario["placements"] == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 19]
    rows = scenario["charts"]
    assert (_at(rows, "price")["from"], _at(rows, "price")["to"]) == (0, 18)
    # Its own bars 0..9 are price bars 0..18, so it shows the same period.
    assert (_at(rows, "curve")["from"], _at(rows, "curve")["to"]) == (0, 9)


def test_dragging_the_equity_curve_moves_the_other_charts(sync_results):
    scenario = sync_results["scenarios"]["subsampledDrag"]
    rows = scenario["charts"]
    assert scenario["pricePushes"] == 1
    assert (_at(rows, "price")["from"], _at(rows, "price")["to"]) == (10, 18), "its bar 5 is price bar 10"
    # ...and the panes follow onto the same bars.
    assert (_at(rows, "rsi")["from"], _at(rows, "rsi")["to"]) == (7, 15)
    assert (_at(rows, "mom")["from"], _at(rows, "mom")["to"]) == (5, 13)
