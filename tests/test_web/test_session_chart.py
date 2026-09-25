"""The session chart: the bars with their volume, and the account's equity under them.

Two panes over one time axis, drawn for the day the page is showing. The interesting behaviours
are the ones a screenshot cannot show, so they are run here against a fake chart library:

* the bars are read for THAT day (`start`/`end`, every row) rather than for the dataset's newest
  window — the page is scoped to a day and the chart has to be too;
* a poll redraws the series but does NOT rebuild the charts, because rebuilding throws away the
  zoom somebody just set — and the same goes for re-fitting the view;
* the equity half fails on its own: a broker that cannot be read leaves the price chart standing
  and puts a sentence under the pane, which is the state this page is most likely to be read in
  when something is wrong;
* the dataset is read BEFORE anything is drawn, because it is what names the axis's time zone.

The chart code lives inside the page's IIFE, so the harness lifts the block out of the real file
and supplies the browser (elements, storage) and the library — nothing else is faked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.data.dataset import save_dataset
from src.web.app import app

client = TestClient(app)

LOG_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "log.js"
LOG_HTML = Path(__file__).resolve().parents[2] / "src" / "web" / "templates" / "log.html"
CSS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "style.css"
# The volume read-out the page attaches: the real module, so the label's behaviour is the shipped
# one rather than a stand-in (see test_volume_readout.py for the read-out's own tests).
VOLUME_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "chart_volume.js"

START_MARKER = "const CHART_HEIGHT"
END_MARKER = "\n  async function loadLog(day)"

HARNESS = r"""
// ---- the page's world, faked ----------------------------------------------
const els = {};
function make(id) {
  els[id] = {
    id: id, innerHTML: "", hidden: false, textContent: "", style: {},
    clientWidth: 1000, offsetWidth: 40,
    // Recorded rather than dropped: the pointer leaving the pane is one of the ways the volume
    // read-out has to be put away, and it is the page's own listener that does it. The rest are
    // kept by type too, so a test can put the reader INSIDE a pane (see ``fire``).
    handlers: {},
    addEventListener: function (type, cb) {
      (this.handlers[type] = this.handlers[type] || []).push(cb);
      if (type === "mouseleave") this.onLeave = cb;
    },
  };
  return els[id];
}
function fire(el, type, ev) {
  (el.handlers[type] || []).forEach((cb) => cb(ev || {}));
}
global.document = {
  hidden: false,
  getElementById: function (id) { return els[id] === undefined ? null : els[id]; },
};
const $ = (id) => document.getElementById(id);
// The page's own escaping and write-only-if-changed helpers live above the lifted block in the
// real file; here they are the two lines the read-outs are allowed to assume.
const esc = (value) => String(value === null || value === undefined ? "" : value);
function setIfChanged(el, html) { el.innerHTML = html; }
["lg-chart", "lg-equity", "lg-equity-panel", "lg-equity-note", "lg-chart-note", "lg-chart-meta",
  "lg-vol-label", "lg-osc-panes", "lg-equity-toggle"].forEach(make);
// The chip reports the pane's own answer, and the page writes it in whenever it re-decides.
els["lg-equity-toggle"].checked = false;
// The chip row. The bundle below marks nothing as used, so the row stays empty and there is no
// chip here to wire — this file keeps its eye on the session chart's own panes.
els["lg-indicator-chips"] = make("lg-indicator-chips");
els["lg-indicator-chips"].querySelectorAll = function () { return []; };

const state = {
  log: { day: "2026-09-21" }, accounts: { env: "paper" }, dataset: null, equity: null,
  // One closed round trip on the account in play: the state the equity pane exists for.
  trades: { trades: [{ env: "paper", ret: 0.04 }] },
};

// Scripted HTTP, by path: the chart reads the dataset, the bars and the account's history.
const calls = [];
let answers = {};
async function api(path) {
  calls.push(path);
  const key = Object.keys(answers).find((k) => path.indexOf(k) === 0);
  if (key === undefined) throw new Error("unexpected request: " + path);
  const answer = answers[key];
  if (answer instanceof Error) throw answer;
  return typeof answer === "function" ? answer(path) : answer;
}

const inPlayEnv = () => "paper";

// The chart library, recording what it is told. `panes` is the list of panes created, so a
// rebuild is visible as a length rather than as a guess.
const panes = [];
function series(kind) {
  return {
    kind: kind, data: [], setData: function (d) { this.data = d; },
    // The library's own y for a value on this series. The fake is linear and deliberately obvious
    // so a test can name the pixel it expects: 300px of pane, ten units to the pixel.
    priceToCoordinate: function (value) { return 300 - Number(value) / 10; },
    // The read-outs ask the candle series for its markers (set to none while both toggles are
    // off, which is what this harness leaves them as) and take a series off the chart again.
    markers: [],
    setMarkers: function (list) { this.markers = list; },
  };
}
function makeChart(host, options) {
  const pane = {
    host: host.id, options: options, removed: false, fitted: 0, sized: null,
    series: [],
    addCandlestickSeries: function () { const s = series("candles"); this.series.push(s); return s; },
    addHistogramSeries: function () { const s = series("volume"); this.series.push(s); return s; },
    addLineSeries: function () { const s = series("line"); this.series.push(s); return s; },
    priceScale: function () { return { applyOptions: function () {} }; },
    timeScale: function () {
      return {
        fitContent: function () { pane.fitted += 1; },
        subscribeVisibleTimeRangeChange: function (cb) { pane.rangeCb = cb; },
        setVisibleRange: function (r) { pane.pushed = r; pane.pushedAs = "time"; },
        setVisibleLogicalRange: function (r) { pane.pushed = r; pane.pushedAs = "logical"; },
        // What the pane is SHOWING. The linking reads the window from here rather than from the
        // range it is told about, because that one stops at the pane's last point and loses the
        // empty space a drag past the data is looking at.
        getVisibleLogicalRange: function () { return pane.logical || null; },
        getVisibleRange: function () { return null; },
        applyOptions: function () {},
      };
    },
    subscribeCrosshairMove: function (cb) { pane.crosshairCb = cb; },
    remove: function () { this.removed = true; },
  };
  panes.push(pane);
  return pane;
}
global.LightweightCharts = {
  createChart: makeChart,
  CrosshairMode: { Normal: 0 },
};
global.ChartZoom = { bind: function () {}, MIN_BAR_SPACING: 0.5 };
global.ChartTime = { timeScaleOptions: function () { return {}; }, localizationOptions: function () { return {}; } };

// ---- the day's bars, as the dataset serves them ---------------------------
// Bar 1 CLOSES DOWN on the one before it, so the volume histogram has both colours to choose
// between — a fixture of rising bars would pass a broken colour rule.
function bars(n) {
  const rows = [];
  for (let i = 0; i < n; i += 1) {
    const open = 1.30 + i * 0.01;
    rows.push({
      date: "2026-09-21", time: 1790000000 + i * 300, datetime: "2026-09-21 12:0" + i,
      open: open, high: open + 0.05, low: open - 0.01,
      close: i === 1 ? open - 0.02 : open + 0.02, volume: 1000 + i,
    });
  }
  return rows;
}
const HISTORY = { ok: true, env: "paper", points: [
  { time: 1790000000, equity: 100000 }, { time: 1790000300, equity: 100250.5 },
]};

// The panes are found by the HOST they were built in rather than by the order they were built
// in: the account's pane is only built once it can be seen, so it does not always follow the
// price one (see ``buildEquityPane``).
function paneIn(id) {
  const found = panes.filter((p) => p.host === id);
  return found.length ? found[found.length - 1] : { series: [], fitted: 0, sized: null };
}

const out = { runs: [] };

async function run(label, patch) {
  Object.assign(answers, patch.answers || {});
  if (patch.day) state.log.day = patch.day;
  // The exchange's today comes from the payload (see ``/api/v1/log``), and the chart compares the
  // day it is drawing against it to say WHICH empty day this is.
  if (patch.today !== undefined) state.log.today = patch.today;
  if (patch.trades) state.trades = { trades: patch.trades };
  calls.length = 0;
  const before = panes.length;
  await renderSession(state.log.day);
  // The trades are read AFTER the session — the broker half is last in ``loadAll`` — so this is
  // the page's own second look at the pane, in the order ``renderTrades`` takes it: the list has
  // just arrived, and if the pane appeared on the strength of it the curve is fetched now.
  if (patch.tradesAfter) state.trades = { trades: patch.tradesAfter };
  if (showEquityPane()) await loadEquity();
  out.runs.push({
    label: label,
    calls: calls.slice(),
    historyCalls: calls.filter((c) => c.indexOf("/api/v1/accounts/history") === 0).length,
    equityHidden: els["lg-equity-panel"].hidden,
    made: panes.length - before,
    fitted: panes.slice(before).reduce((sum, c) => sum + c.fitted, 0),
    fittedAll: panes.reduce((sum, c) => sum + c.fitted, 0),
    removed: panes.filter((c) => c.removed).length,
    priceHost: els["lg-chart"].innerHTML,
    meta: els["lg-chart-meta"].textContent,
    note: els["lg-equity-note"].textContent,
    noteHidden: els["lg-equity-note"].hidden,
    barNote: els["lg-chart-note"].textContent,
    barNoteHidden: els["lg-chart-note"].hidden,
    candles: paneIn("lg-chart").series
      .map((s) => ({ kind: s.kind, data: s.data.slice() })),
    equityPane: paneIn("lg-equity").series
      .map((s) => ({ kind: s.kind, data: s.data.slice() })),
    equityBuilt: panes.slice(before).filter((p) => p.host === "lg-equity").length,
  });
}

(async () => {
  // 1. First draw of the day.
  await run("first", { answers: {
    "/api/v1/dataset/status": { symbol: "GPRO", interval: "5m", market_timezone: "America/New_York" },
    "/api/v1/dataset/data": { rows: bars(3) },
    "/api/v1/accounts/history": HISTORY,
    // Read once per page, always: the chips over the chart are built from it. Nothing is marked
    // `used` here because nothing in this harness's rules tests a drawable series.
    "/api/v1/chart/indicators": { symbol: "GPRO", interval: "5m", overlays: [
      { key: "sma_20", label: "SMA 20", scale: "price", kind: "line", color: "#4c8dff",
        used: false, lines: [{ name: "sma_20", data: [] }] },
    ] },
  } });

  // 2. The poll: same day, one more bar, and the reader's zoom must survive.
  await run("poll", { answers: { "/api/v1/dataset/data": { rows: bars(4) } } });

  // 3. A day with no bars stored (and an account that cannot be read either).
  await run("other day", { day: "2026-09-18", answers: {
    "/api/v1/dataset/data": { rows: [] },
    "/api/v1/accounts/history": new Error("/api/v1/accounts/history?env=paper is missing (404) — the dashboard is running older code than this page, so restart it"),
  } });

  // 4. The bars themselves unreadable: the note says so and the equity read is not attempted,
  //    because there is no session to draw an account against.
  await run("bars fail", { day: "2026-09-17", answers: {
    "/api/v1/dataset/data": new Error("500 Internal Server Error"),
  } });

  // 5. Nothing has been closed: no pane, and no curve asked for that nobody would see.
  await run("nothing closed", { day: "2026-09-21", trades: [], answers: {
    "/api/v1/dataset/data": { rows: bars(3) },
  } });

  // 6. A round trip closed on the OTHER account does not reveal it: the curve drawn is this
  //    account's, so it answers to this account's trades.
  await run("other account closed", { trades: [{ env: "live", ret: 0.1 }] });

  // 7. The exchange's TODAY, with the session not started: the shape the page opens in first
  //    thing in the morning. The pane is empty, and it says which kind of empty it is.
  await run("today, not started", { day: "2026-09-24", today: "2026-09-24", answers: {
    "/api/v1/dataset/data": { rows: [] },
  } });

  // 8. The first load, in its real order: the session is drawn before the trades are read, so
  //    the pane is hidden for the session and appears with the list — and that is when a curve
  //    is fetched out of turn rather than twenty seconds later.
  await run("the first load", {
    day: "2026-09-21", trades: [], tradesAfter: [{ env: "paper", ret: 0.04 }],
    answers: { "/api/v1/accounts/history": HISTORY, "/api/v1/dataset/data": { rows: bars(3) } },
  });

  // 9. The volume read-out, driven the way the library drives it: the crosshair names the bar
  //    under the pointer and hands over that bar's own data.
  const label = els["lg-vol-label"];
  const volumeSeries = paneIn("lg-chart").series.filter((s) => s.kind === "volume")[0];
  function hover(param) {
    paneIn("lg-chart").crosshairCb(param);
    return {
      text: label.textContent, hidden: label.hidden,
      left: label.style.left, top: label.style.top,
    };
  }
  out.volume = {
    overABar: hover({
      time: 1, point: { x: 400, y: 250 }, seriesData: new Map([[volumeSeries, { value: 1000 }]]),
    }),
    pointerLeft: (els["lg-chart"].onLeave(), { hidden: label.hidden }),
    nearTheEdge: hover({
      time: 1, point: { x: 2, y: 250 }, seriesData: new Map([[volumeSeries, { value: 2500 }]]),
    }),
    overNoBar: hover({ time: 1, point: { x: 400, y: 250 }, seriesData: new Map() }),
    left: hover({ time: undefined, point: undefined, seriesData: new Map() }),
  };

  // ---- 9. who is allowed to move whom -------------------------------------
  // Three panes share one time axis but NOT one amount of data: the account's curve holds fewer
  // points than the bars do, and a TIME range cannot even express the empty space a drag past the
  // last bar is looking at. So a window pushed onto the thinner pane comes back NARROWER than it
  // was asked for — and passing that back to the price chart is what used to zoom the session
  // chart the moment a drag reached the end of the data (measured in the browser: 168 bars became
  // 88, with the window pinned at the last bar).
  const pricePane = paneIn("lg-chart");
  const equityPane = paneIn("lg-equity");
  const T0 = 1790000000;                    // the fixture's own bar times: three bars, 300s apart
  const T1 = 1790000300;
  const T2 = 1790000600;
  pricePane.pushed = null;
  equityPane.pushed = null;
  pricePane.logical = null;
  equityPane.logical = null;
  out.linking = {};
  // The price pane leads. Its window is read from its OWN indices — three bars and two bars of
  // empty space past the last of them here — because the library's own time range STOPS at the
  // last bar, and a window handed over without its empty space is what changed a pane's SCALE
  // instead of moving it.
  pricePane.logical = { from: 0, to: 4 };
  pricePane.rangeCb({ from: T0, to: T2 });
  out.linking.priceLeads = {
    toEquity: equityPane.pushed && { from: equityPane.pushed.from, to: equityPane.pushed.to },
    how: equityPane.pushedAs,
  };
  // The account's pane answers with a CLAMPED window. That answer is our own push coming back,
  // whatever its values, and it must stop here.
  pricePane.pushed = null;
  equityPane.rangeCb({ from: T0, to: T2 });
  out.linking.echoFromThinnerPane = pricePane.pushed;
  // A change in a pane nobody is working in is not the reader moving anything either — a pane's
  // range also moves when the layout catches up around it.
  equityPane.rangeCb({ from: T1, to: T2 });
  out.linking.ghostChange = pricePane.pushed;
  // ...but a GESTURE in that pane does lead: dragging the account's pane takes the others with it,
  // and the window is handed over in the price chart's OWN indices.
  equityPane.logical = { from: 0, to: 1 };
  fire(els["lg-equity"], "pointerdown");
  equityPane.rangeCb({ from: T0, to: T1 });
  out.linking.touchedPane = pricePane.pushed
    && { from: pricePane.pushed.from, to: pricePane.pushed.to, how: pricePane.pushedAs };

  // ---- 10. the equity chip: the reader's word on the account's pane -----------
  // The pane appears by itself once a round trip has closed today (``equityWanted``). The chip
  // overrules that either way — and switching it ON has to BUILD the chart, because a chart
  // created inside a hidden panel comes up 0px wide and never recovers (``buildEquityPane``).
  const equityBox = els["lg-equity-toggle"];
  out.equityChip = {};
  // A flat day: no round trip, so the page's own rule keeps the pane away.
  state.trades = { trades: [] };
  equityChoice = null;
  equityBox.checked = true;          // stale from an earlier paint: the sync must correct it
  syncEquityPane();
  out.equityChip.flatDayWithoutTheChip = els["lg-equity-panel"].hidden;
  out.equityChip.boxAfterSync = equityBox.checked;
  // ...and the chip shows it anyway, curve and all.
  equityBox.checked = true;
  await onEquityToggle();
  out.equityChip.flatDayWithTheChip = els["lg-equity-panel"].hidden;
  out.equityChip.built = paneIn("lg-equity").series.length > 0;
  out.equityChip.curve = paneIn("lg-equity").series.map((s) => s.data.length);
  // A day whose round trip DID close, with the reader keeping the pane off the screen.
  state.trades = { trades: [{ env: "paper", ret: 0.04 }] };
  equityBox.checked = false;
  await onEquityToggle();
  out.equityChip.closedButHidden = els["lg-equity-panel"].hidden;
  // ...and, untouched, the page shows it by itself — with the chip saying so, or a box reading
  // "off" would be sitting over a visible chart.
  equityChoice = null;
  syncEquityPane();
  out.equityChip.closedAndUntouched = els["lg-equity-panel"].hidden;
  out.equityChip.boxReportsThePane = equityBox.checked;

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> dict:
    """The page's chart block, run once under node: the runs below, and the read-out's answers."""
    src = LOG_JS.read_text(encoding="utf-8")
    # Three slices of the real file, because the chart's own block sits below the page's small
    # helpers: the zone rule, the account filter the pane answers to, and the session chart.
    # Everything else is the harness's.
    zone_start = src.index("function marketZone()")
    zone = src[zone_start : src.index("\n  }", zone_start) + len("\n  }")]
    mine_start = src.index("function mine(record)")
    mine = src[mine_start : src.index("\n  }", mine_start) + len("\n  }")]
    chart_start = src.index(START_MARKER)
    chart = src[chart_start : src.index(END_MARKER, chart_start)]
    script = tmp_path_factory.mktemp("session") / "session.js"
    script.write_text(
        VOLUME_JS.read_text(encoding="utf-8") + "\n" + zone + "\n" + mine + "\n" + chart + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "session chart harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def session(harness) -> dict:
    return {run["label"]: run for run in harness["runs"]}


@pytest.fixture(scope="module")
def volume(harness) -> dict:
    """The read-out's four answers, which the crosshair rather than a load cycle produces."""
    return harness["volume"]


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the chart"
)


def test_the_bars_are_read_for_the_day_the_page_is_showing(session):
    """The page is scoped to a day, so the chart is too — the dataset's newest window is a
    different question and would draw a different day's bars under this one's heading.

    Both bounds are spelled to the SECOND: see ``test_that_query_string_actually_returns_the_day``
    for what a bare date as the end bound does.
    """
    first = unquote(session["first"]["calls"][1])
    assert first == (
        "/api/v1/dataset/data?start=2026-09-21 00:00:00&end=2026-09-21 23:59:59&limit=0"
    )
    # The second day needs no second status read: the zone is already in hand, and asking again
    # for something that cannot have changed is a request per redraw.
    other = unquote(session["other day"]["calls"][0])
    assert other == (
        "/api/v1/dataset/data?start=2026-09-18 00:00:00&end=2026-09-18 23:59:59&limit=0"
    )
    assert not any(c.endswith("/dataset/status") for c in session["other day"]["calls"])


def test_that_query_string_actually_returns_the_day(tmp_path):
    """The half a canned API cannot check, and the bug this test exists for: the endpoint filters
    on each bar's own stamp, so a bare DATE as the end bound is midnight — the day's first
    instant — and the page's first version asked for "2026-09-21 to 2026-09-21" and got NOTHING
    back. The chart came up as an empty pane with a working fetch behind it, which is the kind of
    failure a fake server that ignores the query will happily pass.
    """
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="GPRO",
        historical_bar_size="5m",
    )
    # ONE session, deliberately: 09:30 to 15:55 at five minutes. A frame that ran on past the
    # close would put bars in the same calendar day that are not part of the session, and the
    # count below is about the session (the endpoint filters on each bar's own stamp, not on the
    # trading window).
    index = pd.date_range("2026-09-18 09:30", periods=78, freq="5min")
    frame = pd.DataFrame(
        {
            "open": 1.30, "high": 1.35, "low": 1.29, "close": 1.31,
            "volume": [1000 + i for i in range(len(index))],
        },
        index=index,
    )
    save_dataset(settings, frame, "GPRO", "5m")
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        day = client.get("/api/v1/dataset/data", params={
            "start": "2026-09-18 00:00:00", "end": "2026-09-18 23:59:59", "limit": 0,
        }).json()
        bare = client.get("/api/v1/dataset/data", params={
            "start": "2026-09-18", "end": "2026-09-18", "limit": 0,
        }).json()
    finally:
        app.dependency_overrides.clear()

    assert day["total"] == 78, "one session at five minutes"
    assert all(row["date"] == "2026-09-18" for row in day["rows"])
    assert bare["total"] == 0, "a bare date as the end bound is the day's first instant"


def test_the_dataset_is_read_before_anything_is_drawn(session):
    """It names the axis's time zone, and the tables' bar cells too. Drawing first and re-labelling
    after would show the reader the wrong times for as long as the read took."""
    assert session["first"]["calls"][0] == "/api/v1/dataset/status"


def test_the_zone_is_read_before_the_tables_that_name_a_bar():
    """The ticks and orders tables print a BAR in every row, and nothing in the loop's own
    records says which zone it is in. So the dataset is read in ``loadLog`` — before either
    table renders — rather than only by the chart, which is the one panel that could wait."""
    body = LOG_JS.read_text(encoding="utf-8")
    start = body.index("async function loadLog(day)")
    load_log = body[start : body.index("\n  }", start)]

    assert "await readDataset()" in load_log
    assert load_log.index("await readDataset()") < load_log.index("renderTicks()")


def test_the_candles_and_the_volume_get_the_bars(session):
    candles = [s for s in session["first"]["candles"] if s["kind"] == "candles"][0]["data"]
    volume = [s for s in session["first"]["candles"] if s["kind"] == "volume"][0]["data"]

    assert len(candles) == 3
    assert candles[0]["open"] == 1.30 and candles[0]["low"] == 1.29
    assert all(isinstance(c["close"], (int, float)) for c in candles)
    assert [v["value"] for v in volume] == [1000, 1001, 1002]
    # A rising bar's volume is green and a falling one's red, or the histogram says nothing about
    # which side the bar closed on.
    assert volume[0]["color"].startswith("rgba(38, 166, 154"), "close >= open"
    assert volume[1]["color"].startswith("rgba(239, 83, 80"), "closed down"


def test_the_equity_pane_gets_the_brokers_points(session):
    line = [s for s in session["first"]["equityPane"] if s["kind"] == "line"][0]["data"]

    assert line == [
        {"time": 1790000000, "value": 100000.0},
        {"time": 1790000300, "value": 100250.5},
    ]
    assert session["first"]["note"] == "", "a curve that read is not annotated"
    assert session["first"]["noteHidden"] is True


def test_a_poll_redraws_without_rebuilding_and_without_re_fitting(session):
    """Rebuilding throws away the zoom; re-fitting zooms the reader out every twenty seconds."""
    assert session["first"]["made"] == 2, "the price pane and the equity pane"
    assert session["poll"]["made"] == 0, "same day: drawn again in place"
    assert session["poll"]["fitted"] == 0, "and the view is left where the reader put it"
    # The new bar did reach the series.
    candles = [s for s in session["poll"]["candles"] if s["kind"] == "candles"][0]["data"]
    assert len(candles) == 4


def test_a_new_day_rebuilds_the_panes(session):
    assert session["other day"]["made"] == 2
    assert session["other day"]["removed"] == 2, "the old day's panes are taken down"


def test_an_unreadable_account_leaves_the_price_chart_standing(session):
    """The two halves fail separately. This is the state a page is most likely to be read in when
    something is wrong, and a chart that vanishes with the broker is a page with no news on it."""
    other = session["other day"]
    assert [s["kind"] for s in other["candles"]] == ["candles", "volume"]
    assert len([s for s in other["candles"] if s["kind"] == "candles"][0]["data"]) == 0
    assert other["priceHost"] == "", "the pane is not replaced by an error"
    # The pane drew nothing because the day has no bars, and it says so: an empty chart with no
    # sentence beside it reads as an empty chart that broke.
    assert other["barNote"] == "no bars were recorded for this day"
    assert "equity history is not served" in other["note"]
    assert "restart" in other["note"], "and the message names the fix"
    assert other["noteHidden"] is False


def test_a_day_that_has_not_started_says_so_on_the_chart(session):
    """The morning state: the exchange's today, with no bars in it yet.

    The pane is empty, and WHICH empty it is matters — a session that has not started is not a
    session that failed, and a blank chart with no sentence beside it reads as a broken one.
    """
    got = session["today, not started"]

    assert got["barNote"] == "the session has not started — no bars for today yet"
    assert [s["kind"] for s in got["candles"]] == ["candles", "volume"]
    assert len(got["candles"][0]["data"]) == 0
    assert got["equityHidden"] is True, "and no account pane: nothing has been closed today"


# ---------------------------------------------------------------------------
# the equity chip: the reader's word on the account's pane
# ---------------------------------------------------------------------------
def test_the_equity_chip_shows_the_pane_on_a_day_whose_trades_say_nothing(harness):
    """The chip is the reader overruling the page's own rule, in the SHOW direction.

    The pane is kept away until a round trip has closed, because a flat curve under a price chart
    reads as a verdict on the strategy — but an account worth looking at on a flat day is the
    reader's business, not the page's. Switching it on also has to BUILD the chart: a chart
    created inside a hidden panel comes up 0px wide and never recovers.
    """
    got = harness["equityChip"]

    assert got["flatDayWithoutTheChip"] is True, "the page's own rule keeps it away"
    assert got["flatDayWithTheChip"] is False, "and the chip shows it"
    assert got["built"] is True, "built at the moment it is shown, not while it was hidden"
    assert got["curve"] == [2], "and the account's own points are drawn on it"


def test_the_equity_chip_hides_a_pane_the_day_would_have_shown(harness):
    """...and in the HIDE direction, which is the one that has to win over a round trip."""
    assert harness["equityChip"]["closedButHidden"] is True


def test_the_equity_chip_reports_the_panes_own_answer(harness):
    """The chip is a switch, not a second opinion: when a round trip closes the pane appears on
    its own, and a box still reading "off" over a visible chart would be lying about the page."""
    got = harness["equityChip"]

    assert got["closedAndUntouched"] is False, "a round trip closed: the page shows the pane"
    assert got["boxReportsThePane"] is True, "and the chip says so"
    assert got["boxAfterSync"] is False, "nor does it keep a stale tick from an earlier state"


def test_a_failure_is_a_sentence_BESIDE_the_chart_not_inside_it(session):
    """The library draws its canvas INTO the host element, so a message written into that host
    deletes the chart — which is how the price pane came up blank the first time it was built.
    Both notes are siblings of their hosts for that reason, and the price one says what happened.
    """
    failed = session["bars fail"]
    assert "the bars could not be read" in failed["barNote"]
    assert failed["barNoteHidden"] is False
    assert failed["priceHost"] == "", "nothing is written into the chart's own host"
    # Nothing to draw a session against, so the account is not asked: one failure, not two.
    assert not any("accounts/history" in c for c in failed["calls"])


def test_the_pane_appears_with_the_first_closed_round_trip(session):
    """An account that has never finished a round trip has a flat line, and a flat line under a
    price chart reads as a verdict on the strategy — so the pane waits for a trade to answer for.
    The price pane is never hidden with it: the bars are the session, and they are true whether or
    not anything was traded."""
    empty = session["nothing closed"]

    assert empty["equityHidden"] is True
    assert empty["historyCalls"] == 0, "no curve is asked for that nobody would see"
    assert len([s for s in empty["candles"] if s["kind"] == "candles"][0]["data"]) == 3
    assert empty["barNote"] == "" and empty["priceHost"] == ""


def test_a_trade_on_the_other_account_does_not_reveal_it(session):
    """The curve drawn is the in-play account's, so it answers to that account's round trips —
    the same filter every other table on this page takes, and for the same reason."""
    other = session["other account closed"]

    assert other["equityHidden"] is True
    assert other["historyCalls"] == 0


def test_the_first_load_hides_it_for_the_session_and_fetches_on_the_trades(session):
    """The order on the first load is the page's own: ``loadAll`` draws the session BEFORE it asks
    the broker, so the pane cannot be decided by the chart — it is decided by ``renderTrades``,
    when the closed trades arrive. The curve is fetched on that turn rather than left empty until
    the next poll twenty seconds later.
    """
    first = session["the first load"]
    line = [s for s in first["equityPane"] if s["kind"] == "line"][0]["data"]

    assert first["equityHidden"] is False, "the list arrived, and the pane came with it"
    assert first["historyCalls"] == 1, "fetched once, on the turn — not once per half"
    assert len(line) == 2, "and the broker's points reached the series"
    # Built on that same turn, not at page load: the library measures its container as the chart
    # is created, and a chart created inside a hidden panel comes up 0px wide with no way back
    # (measured in the browser — ``resize`` does not fix it in the pinned build). Which is also
    # why the pane is built HERE at all rather than with the price chart.
    assert first["equityBuilt"] == 1, "the pane is built on the turn it appears"
    assert session["nothing closed"]["equityBuilt"] == 0, "and never while it is hidden"


def test_the_volume_read_out_labels_the_bar_the_crosshair_is_on(volume):
    """A bar's height is a comparison; the number is what the bar is FOR. The library hands over
    the bar under the pointer and the y of its value on the volume scale — which is the top of
    that bar, so the label sits directly over it."""
    over = volume["overABar"]

    assert over["hidden"] is False
    assert over["text"] == "1,000", "the amount, with thousands separators"
    assert over["left"] == "400px", "over the column the pointer is on"
    assert over["top"] == "200px", "and at the top of that bar, not at the pointer"


def test_the_read_out_is_nudged_in_at_the_edges(volume):
    """At the first and last bar the label would otherwise hang over the price scale — it is
    centred on the bar, so half of it sits outside the pane."""
    edge = volume["nearTheEdge"]

    assert edge["text"] == "2,500"
    assert edge["left"] == "22px", "half of the label plus a gap in from the left edge"


def test_the_read_out_is_put_away_when_there_is_no_bar(volume):
    """A crosshair over a time with no bar (a gap in the session), a crosshair leaving the pane,
    and the pointer walking off it: all three have to clear the label, or the last amount read
    stays on screen as if it were current."""
    assert volume["overNoBar"]["hidden"] is True
    assert volume["left"]["hidden"] is True
    assert volume["pointerLeft"]["hidden"] is True


def test_the_read_out_hangs_off_the_price_panes_crosshair_and_is_not_in_the_chart_host():
    """Wired where the bars are, through the module both pages share, and placed in a frame around
    the host: the library owns that element's contents and has taken back everything this page has
    put in there."""
    body = LOG_JS.read_text(encoding="utf-8")
    build = body[body.index("function buildCharts()") :][:2600]

    # The module is NOT stubbed: the page's own ``ChartVolume.attach`` call wires the shipped
    # read-out to the volume series and this page's label. The label's behaviour is tested in
    # test_volume_readout.py; here it is the wiring, through the page's own call.
    assert "ChartVolume.attach({" in build
    assert "series: charts.volume," in build, "the series the page draws"
    assert 'label: $("lg-vol-label")' in build, "and its own label element"

    html = LOG_HTML.read_text(encoding="utf-8")
    frame = html[html.index('class="chart-volume-frame"') :]
    frame = frame[: frame.index("</div>", frame.index('id="lg-vol-label"'))]
    assert 'id="lg-chart"' in frame, "the host is inside the frame"
    assert 'id="lg-vol-label" hidden' in frame, "and the label starts out of the way"

    css = CSS.read_text(encoding="utf-8")
    assert ".chart-volume-frame { position: relative; }" in css


def test_the_equity_block_starts_hidden_and_the_price_pane_does_not():
    """Hidden in the MARKUP, so the pane stays out of the way even if the script never runs, and
    the heading goes with it — a section whose chart is hidden is a heading with nothing under
    it. The price pane is never inside that wrapper: the bars are the session itself.
    """
    html = LOG_HTML.read_text(encoding="utf-8")
    start = html.index('<div id="lg-equity-panel" hidden>')
    end = html.index("</div>", html.index('id="lg-equity-note"'))
    block = html[start:end]

    assert "Account equity" in block, "the heading is hidden with its chart"
    assert 'id="lg-equity"' in block
    assert 'id="lg-equity-note"' in block
    assert html.index('id="lg-chart"') < start, "the price pane is outside it"
    assert "#lg-equity-panel[hidden]" in CSS.read_text(encoding="utf-8")


def test_the_trades_reveal_the_pane_they_answer_to():
    """The pane hangs off the closed-trade list, and that list is read LAST in ``loadAll`` — the
    session is drawn before the broker is asked — so the chart cannot make the decision alone.
    ``renderTrades`` is the panel that knows, and it both shows the pane and asks for the curve
    out of turn; the chart refreshes it on every draw while it is wanted.
    """
    body = LOG_JS.read_text(encoding="utf-8")
    trades = body[body.index("function renderTrades()") :][:2600]
    assert "if (showEquityPane()) loadEquity();" in trades

    session = body[body.index("async function renderSession(day)") :][:3000]
    assert "if (equityWanted()) {" in session
    assert "await loadEquity();" in session
    # Guarded, so the pane's own element list and the drawer are always there together.
    load_equity = body[body.index("async function loadEquity()") :][:120]
    assert "if (!charts.line) return;" in load_equity


def test_the_meta_line_names_what_the_chart_is_of(session):
    """The instrument comes from the DATASET the bars were read from rather than from the strategy
    the page is scoped to: if the two ever disagree, the chart says which one it is showing."""
    assert session["first"]["meta"] == "GPRO · 5m · 2026-09-21 · 3 bars"
    assert session["other day"]["meta"] == "GPRO · 5m · 2026-09-18"


# ---------------------------------------------------------------------------
# three panes over one axis, and one window
# ---------------------------------------------------------------------------
def test_the_price_pane_leads_the_others(harness):
    """The bars are the reference: panning or zooming them takes the indicator and account panes
    with it, with no gesture needed in those.

    The window is handed over in the RECEIVING pane's own indices rather than as a time range,
    because a time range cannot carry the empty space beyond that pane's last point and the library
    clamps one that reaches past its data — which is how a shared window used to change a pane's
    SCALE. The price pane is showing three bars and two bars of empty space past the last of them;
    the account's curve has two points on the same grid, so that window is index 0 to 4 there —
    three points past its own last one, which is exactly the space a time range could not carry.
    """
    assert harness["linking"]["priceLeads"] == {"toEquity": {"from": 0, "to": 4}, "how": "logical"}


def test_a_thinner_pane_cannot_pull_the_session_chart_back(harness):
    """The reported bug: clicking and dragging the chart sideways zoomed it, "the movement reaches
    the end of the chart and right after reaching the end it starts to zoom".

    Three panes share one axis but not one amount of data — the account's curve holds fewer points
    than the bars — and a TIME range cannot express the empty space a drag past the last bar is
    looking at. So the window pushed onto that pane comes back NARROWER than it was asked for, and
    handing that back to the price chart as if the reader had asked for it is what zoomed the chart
    exactly when the drag reached the end of the data. Measured in the browser before the fix:
    168 bars became 88, pinned at the last bar.

    An answer that arrives while our own push is standing is that push coming back — whatever
    values it carries — so it stops where it is.
    """
    assert harness["linking"]["echoFromThinnerPane"] is None


def test_a_change_in_a_pane_nobody_is_working_in_is_ignored(harness):
    """A pane's range also moves when the layout catches up around it, and a pane below the price
    chart is not the reader moving anything unless the reader is IN it."""
    assert harness["linking"]["ghostChange"] is None


def test_a_gesture_in_a_pane_still_leads(harness):
    """...while the reader working in the account's pane DOES take the others with them — again in
    the price chart's own indices, and never as a new scale for it."""
    assert harness["linking"]["touchedPane"] == {"from": 0, "to": 1, "how": "logical"}


# ---------------------------------------------------------------------------
# where the panel sits, and what it loads
# ---------------------------------------------------------------------------
def test_the_panel_sits_above_the_loop_and_folds_like_the_rest():
    html = LOG_HTML.read_text(encoding="utf-8")
    chart = html.index("<h2>Session chart</h2>")
    assert html.index("<h2>Account</h2>") < chart < html.index("<h2>The Loop</h2>")
    assert 'class="collapse-body" id="chart-body"' in html
    for host in ("lg-chart", "lg-equity", "lg-equity-note", "lg-chart-note", "lg-chart-meta"):
        assert f'id="{host}"' in html, host
    # The pane order is the point: the price above, the account under it.
    assert html.index('id="lg-chart"') < html.index('id="lg-equity"')
    # What the chart is OF survives the fold, like the loop's clock: "the chart of what" is not
    # detail, and a folded panel should still answer it.
    assert 'id="lg-chart-meta"' in html[html.index('id="lg-chart-meta"') - 200 :]
    assert 'class="muted keep-visible" id="lg-chart-meta"' in html


def test_the_page_loads_the_chart_before_it_uses_it():
    html = LOG_HTML.read_text(encoding="utf-8")
    order = [
        html.index("lightweight-charts"),
        html.index("/static/chart_zoom.js"),
        html.index("/static/chart_time.js"),
        html.index("/static/trading_switch.js"),
        html.index("/static/log.js"),
    ]
    assert order == sorted(order), "the library, then its two helpers, then the page"


def test_the_chart_is_redrawn_by_every_path_that_changes_the_day():
    """The chart hangs off the DAY's loader, so every path that moves the day moves the chart with
    it: the boot read, the ↻ button and the poll all go through ``loadAll``, and picking a day in
    the menu goes straight to ``loadLog``.

    Pinned because it was first wired to ``loadAll`` alone, and the day menu then left the pane
    drawing the day before's session under the new day's heading.
    """
    body = LOG_JS.read_text(encoding="utf-8")
    load_log = body[body.index("async function loadLog(day)") :][:2500]

    assert "await renderSession(" in load_log
    assert "loadLog(button.dataset.day)" in body, "the day button must still go through loadLog"
