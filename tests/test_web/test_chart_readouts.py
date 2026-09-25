"""The session chart's two read-outs: the indicators the rules use, and the signals.

A row of CHIPS over the chart, one per indicator, every chip ON by default, plus the signals
checkbox — and this is what they have to do:

* **the chips** — one per indicator the strategy's RULES test, and no more: the bundle is every
  feature the strategy is configured with, but an indicator nothing tests is a picture without a
  question. Each chip draws its indicator where that kind belongs: the price-scaled ones as series
  on the candles, the rest in a pane of their own. The absolute-volume overlay is deliberately NOT
  drawn: this chart's own volume pane is that series, and a second copy would be two pictures of
  one thing.
* **signals** — the signals the LOOP generated that were not a hold, read from the day's own tick
  records. They are drawn as ONE ROW of arrows along the top of the price pane, each pointing down
  at the bar it was decided on: a signal is an event on a bar and not a price, and on the candles
  the arrows sat over the very bars they were about. A signal something came of (an order that was
  sent, or a round trip closed) is green or red; one that never became an order is grey, because a
  chart of a live session is read for what happened rather than for what was said.

Both run here for real, against fakes for the library: the assertions are about the series, the
panes and the markers the page asks for, not about its text.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOG_JS = ROOT / "src" / "web" / "static" / "log.js"
LOG_HTML = (ROOT / "src" / "web" / "templates" / "log.html").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")

# The span the session-chart tests lift: the constants, the panes and the read-outs, down to the
# day loader. ``drawReadouts``, ``onChartToggle`` and ``renderSession`` are all inside it.
START_MARKER = "const CHART_HEIGHT"
END_MARKER = "\n  async function loadLog(day)"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the chart"
)

HARNESS = r"""
// ---- the page's world, faked ----------------------------------------------
const els = {};
function make(id, extra) {
  const el = Object.assign({
    id: id, hidden: false, textContent: "", style: {},
    clientWidth: 1000, children: [],
    appendChild: function (child) { this.children.push(child); },
    addEventListener: function () {},
  }, extra || {});
  // A real element's children go when its markup is replaced, and the pane host relies on it:
  // the toggle sets ``innerHTML = ""`` before building the panes again.
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get: function () { return html; },
    set: function (value) { html = value; this.children = []; },
  });
  els[id] = el;
  return el;
}
function makeElement(tag) {
  return {
    tag: tag, className: "", textContent: "", children: [], style: {},
    appendChild: function (child) { this.children.push(child); },
    // A pane element takes gestures like any other: the chart marks which pane the reader is
    // working in so only that one may move the others (see ``linkPanes``).
    addEventListener: function () {},
  };
}
global.document = {
  hidden: false,
  getElementById: function (id) { return els[id] === undefined ? null : els[id]; },
  createElement: makeElement,
};
const $ = (id) => document.getElementById(id);
// The page's own escaping and write-only-if-changed helpers live above the lifted block in the
// real file; here they are the two lines the read-outs are allowed to assume.
const esc = (value) => String(value === null || value === undefined ? "" : value);
function setIfChanged(el, html) { if (el.innerHTML !== html) el.innerHTML = html; }
["lg-chart", "lg-equity", "lg-equity-panel", "lg-equity-note", "lg-chart-note", "lg-chart-meta",
  "lg-vol-label", "lg-osc-panes"].forEach((id) => make(id));
make("lg-signals-toggle", { checked: true });
// The account's pane is off until a round trip has closed (or the reader says otherwise), so its
// chip starts UNCHECKED — the page writes its own answer into the box when it re-decides.
make("lg-equity-toggle", { checked: false });

// The chip row's host. A chip is written into it as markup and wired by a query over what was
// written, so the fake has to answer that query — with STABLE objects per key, or the handler the
// page attached to a chip would be lost by the next look at it.
const chipHost = make("lg-indicator-chips");
chipHost.querySelectorAll = function (selector) {
  if (selector !== "[data-key]") return [];
  const keys = [...this.innerHTML.matchAll(/data-key="([^"]+)"/g)].map((m) => m[1]);
  this.chips = this.chips || {};
  return keys.map((key) => {
    if (!this.chips[key]) {
      this.chips[key] = { dataset: { key: key }, classList: { toggle: function () {} }, onclick: null };
    }
    return this.chips[key];
  });
};
function chipKeys() {
  return [...chipHost.innerHTML.matchAll(/data-key="([^"]+)"/g)].map((m) => m[1]);
}
function clickChip(key) {
  const chip = chipHost.querySelectorAll("[data-key]").find((c) => c.dataset.key === key);
  if (!chip || !chip.onclick) throw new Error("no chip for " + key);
  chip.onclick();
}

const state = {
  log: { day: "2026-09-21", ticks: [] }, accounts: { env: "paper" }, dataset: null,
  trades: { trades: [] },
};

const calls = [];
const answers = {};
async function api(path) {
  calls.push(path);
  const key = Object.keys(answers).find((k) => path.indexOf(k) === 0);
  if (key === undefined) throw new Error("unexpected request: " + path);
  return answers[key];
}

// The page's own rule about which account is being traded. The account FILTER (``mine``) and the
// market zone are the real ones, lifted out of the file with the chart, because the read-outs are
// the code under test and those two are what they filter and label by.
const inPlayEnv = () => "paper";

// ---- the chart library, recording what it is told -------------------------
const panes = [];
// The pane's own y scale, so a test can MOVE it: 300px tall, ten price units to the pixel, and
// ``scaleTop`` is the price at its top edge. The signal row is placed against this pair of
// conversions, which is how it is checked without a browser.
let scaleTop = 3000;
function series(kind, options) {
  return {
    kind: kind, options: options || {}, data: [], markers: [], removed: false,
    setData: function (d) { this.data = d; },
    setMarkers: function (list) { this.markers = list; },
    applyOptions: function (o) { Object.assign(this.options, o); },
    coordinateToPrice: function (y) { return scaleTop - Number(y) * 10; },
    priceToCoordinate: function (value) { return (scaleTop - Number(value)) / 10; },
  };
}
function makeChart(host) {
  const pane = {
    host: host, removed: false, series: [], pushed: null, fitted: 0,
    addCandlestickSeries: function (o) { const s = series("candles", o); this.series.push(s); return s; },
    addHistogramSeries: function (o) { const s = series("histogram", o); this.series.push(s); return s; },
    addLineSeries: function (o) { const s = series("line", o); this.series.push(s); return s; },
    removeSeries: function (s) { s.removed = true; this.series = this.series.filter((x) => x !== s); },
    priceScale: function () { return { applyOptions: function () {} }; },
    timeScale: function () {
      return {
        fitContent: function () { pane.fitted += 1; },
        subscribeVisibleTimeRangeChange: function (cb) { pane.rangeCb = cb; },
        setVisibleRange: function (r) { pane.pushed = r; },
        setVisibleLogicalRange: function (r) { pane.pushed = r; },
        // What the pane is showing: the linking hands a window over in the receiving pane's own
        // indices, having read this one's (see ``linkPanes``).
        getVisibleLogicalRange: function () { return { from: 0, to: 2 }; },
        getVisibleRange: function () { return { from: 1790000000, to: 1790000900 }; },
        applyOptions: function () {},
      };
    },
    subscribeCrosshairMove: function () {},
    remove: function () { this.removed = true; },
  };
  panes.push(pane);
  if (host) host.pane = pane;   // what the harness reads a pane's series from
  return pane;
}
global.LightweightCharts = {
  createChart: makeChart, CrosshairMode: { Normal: 0 },
};
// The row is placed a second time on the library's own animation frame (the price scale settles
// there), so the harness runs it straight through: what a test wants to see is the state the
// reader would, not the two-step path to it.
global.requestAnimationFrame = function (callback) { callback(); return 1; };
global.cancelAnimationFrame = function () {};
global.ChartZoom = { bind: function () {}, MIN_BAR_SPACING: 0.5 };
global.ChartTime = {
  timeScaleOptions: function () { return {}; },
  localizationOptions: function () { return {}; },
  isIntraday: function (interval) { return String(interval).indexOf("d") < 0; },
};

// ---- the day, the strategy's indicators, and what the loop did ------------
const T0 = 1790000000;                       // the fixture day, in unix seconds
const barFor = (seconds) => new Date(seconds * 1000).toISOString();

function bars(n) {
  const rows = [];
  for (let i = 0; i < n; i += 1) {
    const open = 1.30 + i * 0.01;
    rows.push({
      date: "2026-09-21", time: T0 + i * 300, open: open, high: open + 0.05,
      low: open - 0.01, close: open + 0.02, volume: 1000 + i,
    });
  }
  return rows;
}

// Two of the seventeen points are from the DAY BEFORE and one from the day after: the bundle is
// the lab's, computed over sixty days, while this chart is of one session. Drawn unclipped they
// would stretch the price pane to two months.
//
// ``used`` is the server's answer to "do this strategy's RULES test it": the two the rules name
// get a chip, a third name is marked used but has no chip, and never a pane either — `volume_abs`
// is that series, and this chart's own volume pane is where it is already drawn.
const OUTSIDE = 86400;
const INDICATORS = { symbol: "GPRO", interval: "5m", overlays: [
  { key: "sma_20", label: "SMA 20", scale: "price", kind: "line", color: "#4c8dff", used: true,
    lines: [{ name: "sma_20", data: [
      { time: T0 - OUTSIDE, value: 1.20 }, { time: T0, value: 1.31 },
      { time: T0 + 300, value: 1.32 }, { time: T0 + OUTSIDE, value: 1.40 }] }] },
  { key: "rsi_14", label: "RSI 14", scale: "osc", kind: "line", color: "#f0b429", used: true,
    lines: [{ name: "rsi_14", data: [
      { time: T0 - OUTSIDE, value: 40 }, { time: T0, value: 55 }] }] },
  { key: "vratio_20", label: "Relative Volume 20", scale: "osc", kind: "line", color: "#26a69a",
    used: false, lines: [{ name: "vratio_20", data: [{ time: T0, value: 1.4 }] }] },
  { key: "ema_9", label: "EMA 9", scale: "price", kind: "line", color: "#26a69a", used: false,
    lines: [{ name: "ema_9", data: [{ time: T0, value: 1.25 }] }] },
  { key: "volume_abs", label: "Volume (absolute)", scale: "osc", kind: "histogram",
    color: "#8a93a6", used: true,
    lines: [{ name: "volume_abs", data: [{ time: T0, value: 1200 }] }] },
] };

// Four bars drawn, and the loop's own records for them: a hold, a filled BUY, a BUY it never
// sent, a filled SELL, one for the OTHER account, and one for a bar that is not on screen.
const TICKS = [
  { env: "paper", bar: barFor(T0), signal: "HOLD", action: "decided" },
  { env: "paper", bar: barFor(T0 + 300), signal: "BUY", action: "decided",
    order_ids: ["o-1"], intents: [{ order_id: "o-1" }] },
  { env: "paper", bar: barFor(T0 + 600), signal: "BUY", action: "decided" },
  { env: "paper", bar: barFor(T0 + 900), signal: "SELL", action: "decided",
    trades: [{ ret: -0.01 }] },
  { env: "live", bar: barFor(T0 + 1200), signal: "SELL", action: "decided",
    order_ids: ["o-9"] },
  { env: "paper", bar: barFor(T0 + 99999), signal: "BUY", action: "decided",
    order_ids: ["o-8"] },
];

Object.assign(answers, {
  "/api/v1/dataset/status": { symbol: "GPRO", interval: "5m", market_timezone: "America/New_York" },
  "/api/v1/dataset/data": { rows: bars(4) },
  "/api/v1/accounts/history": { ok: true, env: "paper", points: [] },
  "/api/v1/chart/indicators": INDICATORS,
});
state.log.ticks = TICKS;

function pricePane() {
  return panes.filter((p) => p.host && p.host.id === "lg-chart").pop();
}
// The series the arrows hang on: the INVISIBLE line on the price pane. Every other line there is
// an indicator, so "does it draw" is what tells the two apart.
function rowSeries() {
  const pane = pricePane();
  if (!pane) return null;
  return pane.series.filter((s) => s.kind === "line" && s.options.lineVisible === false).pop() || null;
}
function markers() {
  const row = rowSeries();
  return (row ? row.markers : []) || [];
}
function candleMarkers() {
  const pane = pricePane();
  const candles = pane ? pane.series.filter((s) => s.kind === "candles").pop() : null;
  return (candles ? candles.markers : []) || [];
}
function rowValues() {
  const row = rowSeries();
  return (row ? row.data : []).map((p) => p.value);
}
function rowValue() {
  const values = rowValues();
  return values.length ? values[0] : null;
}
function indicatorPanes() {
  return els["lg-osc-panes"].children.map((box) => ({
    title: (box.children[0] || {}).textContent,
    kinds: ((box.children[1] || {}).pane || { series: [] }).series.length,
  }));
}
// The indicator panes the page has made, alive and torn down. A pane that is rebuilt on every
// poll would show up here as a growing pile of corpses.
function createdIndicatorPanes() {
  return panes.filter((p) => p.host && !p.host.id);
}
function builtPanes() {
  return panes.filter((p) => p.host && !p.host.id).length;
}

// What the price pane and the indicator panes hold, as the test wants to read it. ``row`` marks
// the signal row, which is a series like any other here but not an indicator.
function priceSeries() {
  const pane = pricePane();
  return (pane ? pane.series : []).map((s) => ({
    kind: s.kind, points: s.data.length, times: s.data.map((p) => p.time),
    removed: s.removed, row: s.options.lineVisible === false,
  }));
}

const out = { steps: [] };
async function step(label) {
  calls.length = 0;
  const before = panes.length;
  await renderSession("2026-09-21");
  out.steps.push({
    label: label,
    made: panes.length - before,
    price: priceSeries(),
    markers: markers(),
    candleMarkers: candleMarkers(),
    rowValues: rowValues(),
    rowValue: rowValue(),
    panes: indicatorPanes(),
    chips: chipKeys(),
    row: chipHost.innerHTML,
    oscAlive: createdIndicatorPanes().filter((p) => !p.removed).length,
    oscRemoved: createdIndicatorPanes().filter((p) => p.removed).length,
    indicatorCalls: calls.filter((c) => c.indexOf("/api/v1/chart/indicators") === 0).length,
    note: els["lg-chart-note"].textContent,
  });
}

(async () => {
  await step("defaults");

  clickChip("rsi_14");
  await step("the RSI chip off");

  clickChip("sma_20");
  await step("and the SMA chip too");

  clickChip("rsi_14");
  clickChip("sma_20");
  await step("both chips back on");

  // The pane is PINCHED: the same pixels, prices three hundred higher. The row is a price, so the
  // page is asked to put it back where the top of the pane now is — which is what the library does
  // by firing the visible-range change the row is placed from.
  scaleTop = 3300;
  if (pricePane().rangeCb) pricePane().rangeCb({ from: T0, to: T0 + 900 });
  await step("the pane rescaled");

  els["lg-signals-toggle"].checked = false;
  await onChartToggle();
  await step("signals off");

  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_harness(tmp_path_factory) -> dict:
    src = LOG_JS.read_text(encoding="utf-8")
    # Four slices of the real file: the zone rule and the account filter the chart block sits
    # below, the marker/volume modules the page loads first, and the chart block itself.
    zone_start = src.index("function marketZone()")
    zone = src[zone_start : src.index("\n  }", zone_start) + len("\n  }")]
    mine_start = src.index("function mine(record)")
    mine = src[mine_start : src.index("\n  }", mine_start) + len("\n  }")]
    chart_start = src.index(START_MARKER)
    chart = src[chart_start : src.index(END_MARKER, chart_start)]
    script = tmp_path_factory.mktemp("readouts") / "readouts.js"
    script.write_text(
        (ROOT / "src" / "web" / "static" / "chart_time.js").read_text(encoding="utf-8")
        + "\n" + zone + "\n" + mine + "\n" + chart + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "read-out harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return {step["label"]: step for step in json.loads(proc.stdout)["steps"]}


@pytest.fixture(scope="module")
def drawn(tmp_path_factory) -> dict:
    return _run_harness(tmp_path_factory)


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------
def test_the_chart_head_carries_the_signals_checkbox_and_it_starts_on():
    """The signals read-out is still a checkbox, and still on by default: the chart is read for
    what the strategy did, and a reader who has to switch that on every time reads it half-blind."""
    head = LOG_HTML[LOG_HTML.index('id="lg-chart-meta"') - 2000 : LOG_HTML.index('id="lg-chart-meta"')]

    assert 'id="lg-signals-toggle"' in head
    assert '<input type="checkbox" id="lg-signals-toggle" checked' in LOG_HTML
    assert LOG_HTML.count('onchange="onChartToggle()"') == 1
    assert "signals" in head
    assert 'onclick="event.stopPropagation()"' in head, "or a click also folds the chart"


def test_the_chart_head_carries_the_equity_chip():
    """The account's pane has a switch of the reader's own, beside the signals one.

    The pane appears by itself once a round trip has closed (see ``equityWanted``), which is the
    page deciding that a curve is worth showing; the chip is the reader overruling that either way
    — showing the account on a day whose trades say nothing, or taking it off the screen when the
    account's curve is not what they are reading. It starts unchecked because the pane does, and
    the page writes the real answer into it whenever it re-decides.
    """
    head = LOG_HTML[LOG_HTML.index('id="lg-chart-meta"') - 2000 : LOG_HTML.index('id="lg-chart-meta"')]
    body = LOG_JS.read_text(encoding="utf-8")

    assert 'id="lg-equity-toggle"' in head, "in the chart's head, not beside the chart"
    assert '<input type="checkbox" id="lg-equity-toggle" onchange="onEquityToggle()">' in LOG_HTML
    assert LOG_HTML.count('onchange="onEquityToggle()"') == 1
    assert 'class="chart-toggle"' in head, "the same switch the signals use"
    assert "equity" in head, "labelled"
    assert 'id="lg-signals-toggle"' in head, "beside the signals chip, not instead of it"
    assert LOG_HTML.count('onchange="onChartToggle()"') == 1, "one handler each"
    assert "window.onEquityToggle = onEquityToggle;" in body, "or the chip does nothing at all"
    # The chip switches a chart that folds away with the panel, so unlike the line beside it the
    # label is not ``keep-visible`` (the signals chip is the same).
    chip = LOG_HTML[: LOG_HTML.index('<input type="checkbox" id="lg-equity-toggle"')]
    chip = chip[chip.rindex("<label"):] + LOG_HTML[LOG_HTML.index('id="lg-equity-toggle"'):]
    assert "keep-visible" not in chip[: chip.index("</label>")]


def test_the_indicators_are_chips_above_the_chart_and_not_a_checkbox():
    """The indicators are chosen one at a time, so they get one control each: a row of chips over
    the price pane, in the shape the lab's chart uses. The checkbox they replaced is gone."""
    assert 'id="lg-indicators-toggle"' not in LOG_HTML, "the old checkbox"
    assert 'id="lg-indicator-chips"' in LOG_HTML
    body = LOG_HTML[LOG_HTML.index('id="chart-body"') : LOG_HTML.index('id="lg-chart"')]

    assert 'id="lg-indicator-chips"' in body, "above the chart's own frame"
    assert LOG_HTML.index('id="lg-indicator-chips"') < LOG_HTML.index('class="chart-volume-frame"')
    assert "indicator-toggles" in LOG_HTML, "the chip row's styling, shared with the lab"
    assert ".indicator-toggles" in CSS and "button.chip-ind" in CSS


def test_the_indicator_panes_have_a_host_between_the_price_and_the_account():
    """Where the panes go: under the candles, above the account's curve. Both are charts in their
    own right and the order is the one the page is read in."""
    assert 'id="lg-osc-panes"' in LOG_HTML
    assert LOG_HTML.index('id="lg-chart-note"') < LOG_HTML.index('id="lg-osc-panes"')
    assert LOG_HTML.index('id="lg-osc-panes"') < LOG_HTML.index('id="lg-equity-panel"')
    # The host is measured before a chart is built in it (the library measures its container as
    # the chart is created), so hiding it while empty is how this panel comes up with no panes.
    assert "#lg-osc-panes:empty" not in CSS


def test_the_toggles_are_reachable_from_the_markup():
    assert "window.onChartToggle = onChartToggle;" in LOG_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the signals
# ---------------------------------------------------------------------------
def test_only_the_signals_that_were_not_a_hold_are_marked(drawn):
    """A hold is the absence of a signal, and a marker on every bar of a quiet session is a
    scribble rather than information."""
    markers = drawn["defaults"]["markers"]

    assert len(markers) == 3, "the hold, the other account's tick and the missing bar are out"


def test_a_filled_signal_takes_the_trade_s_colour_and_an_unfilled_one_is_grey(drawn):
    """The distinction the read-out exists for: not what it said, but what it said that anything
    happened on. Green and red are the trade's own colours; grey is the signal nothing came of —
    and with both sides drawn the same way, the COLOUR is what says which side it was."""
    markers = drawn["defaults"]["markers"]
    by_colour = {(m["shape"], m["color"]) for m in markers}

    assert ("arrowDown", "#26a69a") in by_colour, "a filled BUY"
    assert ("arrowDown", "#ef5350") in by_colour, "a filled SELL"
    assert ("arrowDown", "#8a93a6") in by_colour, "a BUY that was never sent, grey"


def test_the_arrows_are_a_ROW_at_the_top_of_the_pane_pointing_down_at_their_bar(drawn):
    """Where the arrows are, and why they left the candles.

    A signal is an event on a bar, not a price: drawn on the candle it belongs to it sat over the
    bars it was about, at a height that moved with the day's range. All of them are now on ONE row
    at the top of the pane, every arrow pointing DOWN at its own bar — and nothing at all is drawn
    on the candles.

    The row is placed in PIXELS from the top, against the library's own price conversion, so the
    assertion that matters is the VALUE it lands on: 14px down a 300px fake pane is 2860, and it
    gives every drawn bar a point at that value so the arrows have something to hang on.
    """
    step = drawn["defaults"]

    assert all(m["position"] == "aboveBar" for m in step["markers"]), "all of them at the top"
    assert all(m["shape"] == "arrowDown" for m in step["markers"]), "and all pointing down"
    assert step["candleMarkers"] == [], "the candles carry nothing any more"
    assert step["rowValues"] == [2860, 2860, 2860, 2860], "one point per bar, at the row's price"
    assert step["rowValue"] == 2860


def test_the_row_follows_the_top_of_the_pane_wherever_it_moves(drawn):
    """The row is a price, and the top of the pane moves under it.

    A pinch, a pan, a day fitted differently — each one rescales the price axis, and the library
    says so by firing the visible-range change the row is placed from. Left where it was, the row
    would drift down into the candles the moment the reader touched the scale.
    """
    assert drawn["defaults"]["rowValue"] == 2860
    rescaled = drawn["the pane rescaled"]

    assert rescaled["rowValue"] == 3160, "placed again, against the pane that is there now"
    assert rescaled["rowValues"] == [3160, 3160, 3160, 3160]
    assert all(m["position"] == "aboveBar" for m in rescaled["markers"])


def test_the_markers_sit_on_the_bars_that_are_drawn_and_in_order(drawn):
    """The library rejects markers out of order, and a marker on a bar that is not on screen is a
    marker nobody can see."""
    markers = drawn["defaults"]["markers"]
    times = [m["time"] for m in markers]

    assert times == sorted(times)
    assert all(1790000000 <= t <= 1790000900 for t in times), "inside the drawn bars"


def test_turning_the_signals_off_takes_the_markers_off(drawn):
    assert drawn["signals off"]["markers"] == []
    assert drawn["signals off"]["candleMarkers"] == []
    assert drawn["defaults"]["markers"] != [], "and on by default"


# ---------------------------------------------------------------------------
# the indicator chips
# ---------------------------------------------------------------------------
def test_a_chip_for_every_indicator_the_rules_test_and_no_more(drawn):
    """The bundle is every feature the strategy is CONFIGURED with; the chips are the ones its
    RULES read. An indicator nothing tests is a line on a chart with no question attached to it —
    and the volume gets no chip whatever the rules say, because this chart's own volume pane is
    that series already: a chip for it would be a switch with nothing behind it."""
    assert drawn["defaults"]["chips"] == ["sma_20", "rsi_14"], "the two the rules name"
    assert "vratio_20" not in drawn["defaults"]["chips"]
    assert "volume_abs" not in drawn["defaults"]["chips"], "used, and still no chip"


def test_the_chips_start_on_and_draw_their_indicator(drawn):
    """On, all of them: the reader opened a chart of the strategy's own session and wants to see
    what it traded on. Each goes where its kind belongs — the price-scaled one on the candles, the
    oscillator in a pane of its own."""
    step = drawn["defaults"]
    drawn_lines = [s for s in step["price"] if s["kind"] == "line" and not s["row"]]

    assert [s["points"] for s in drawn_lines] == [2], "SMA 20 on the price pane, the day's points"
    assert [p["title"] for p in step["panes"]] == ["RSI 14"]
    assert step["oscAlive"] == 1


def test_an_indicator_the_rules_do_not_test_is_never_drawn(drawn):
    """Not a chip, not a series, not a pane — in any state of the row. ``volume_abs`` is out on top
    of that: the session chart's own volume pane is that series already."""
    for label, step in drawn.items():
        assert [p["title"] for p in step["panes"]] == (["RSI 14"] if step["oscAlive"] else [])
        assert "Relative Volume 20" not in str(step["panes"])
        assert "Volume (absolute)" not in str(step["panes"])
        assert len([s for s in step["price"] if s["kind"] == "line" and not s["row"]]) <= 1, (
            "no EMA 9")


def test_the_series_are_clipped_to_the_day_on_screen(drawn):
    """The bundle is computed over sixty days and the lab charts all of them; this chart is of ONE
    session. Left unclipped, the overlays stretch the price pane's own fit to two months and leave
    the day under examination squeezed into a sliver at the right-hand edge."""
    step = drawn["defaults"]
    drawn_times = set()
    for series in step["price"]:
        drawn_times.update(series["times"])

    assert drawn_times <= {1790000000, 1790000300, 1790000600, 1790000900}, (
        "only the bars on screen: no point from the day before or the day after")
    for pane_series in step["panes"]:
        assert pane_series["kinds"] >= 1


def test_a_chip_takes_only_its_own_indicator_off(drawn):
    """One chip, one indicator. The pane goes; the candle overlay the other chip owns does not."""
    def lines(step):
        return [s for s in step["price"] if s["kind"] == "line" and not s["row"]]

    off = drawn["the RSI chip off"]

    assert off["panes"] == [] and off["oscAlive"] == 0, "its pane is gone"
    assert len(lines(off)) == 1, "the SMA stayed"

    bare = drawn["and the SMA chip too"]

    assert lines(bare) == []
    assert bare["price"][0]["kind"] == "candles", "the bars stay"


def test_switching_chips_back_on_does_not_double_anything(drawn):
    """Switching is the common way the row is exercised, so it has to be idempotent: two SMAs and
    two RSI panes would be one bug visible twice."""
    again = drawn["both chips back on"]

    assert len([s for s in again["price"] if s["kind"] == "line" and not s["row"]]) == 1
    assert len(again["panes"]) == 1
    assert again["panes"][0]["title"] == "RSI 14" and again["oscAlive"] == 1, "the pane is back"
    assert again["oscRemoved"] == 1, "and the one it replaced is gone, not stacked on"


def test_the_bundle_is_read_once_per_page(drawn):
    """It is memoized on the server and identical to the lab's, so re-reading it on every chip
    and every poll would be requests for a picture that has not changed."""
    assert drawn["defaults"]["indicatorCalls"] == 1
    assert drawn["both chips back on"]["indicatorCalls"] == 0, "never again, however often flipped"


def test_a_read_out_that_fails_never_takes_the_chart_with_it(drawn):
    """The bars are the chart; the read-outs are drawn on it. A harness or a series the library
    refuses must leave the day drawn and say so in the line under the pane."""
    assert drawn["defaults"]["price"], "the price pane still has its bars"
    assert drawn["defaults"]["note"] == ""
