/* TRAIDER dashboard — drives the two UI states from the dataset status API. */
"use strict";

const PAGE_SIZE = 100;
const POLL_MS = 2000;
// Subtle full-height fill painted UNDER the candles from each real position
// open (IN) to close (OUT), so it is obvious when a position was held — and,
// by its colour, whether that round trip made money. A position still open at
// the end of the data keeps the neutral tint (its outcome is unknown).
const POSITION_SHADE = "rgba(110, 160, 255, 0.15)";
const POSITION_SHADE_WIN = "rgba(38, 166, 154, 0.18)";
const POSITION_SHADE_LOSS = "rgba(239, 83, 80, 0.18)";

const state = {
  status: null,
  rows: [],
  total: 0,
  offset: 0,
  config: null,
  account: null, // GET /api/v1/account -> {file, file_exists, groups, error}
  chart: null,
  datasetRows: null,
  indicators: null,
  selected: {}, // overlay key -> visible
  overlayLines: [],
  oscCharts: [],
  rulesPayload: null, // GET /api/v1/rules -> {ruleset, allowed_features, ops, file}
  signalData: null, // GET /api/v1/signal -> {latest, series, counts, enabled_rules, ...}
  showSignals: true, // draw BUY/SELL markers on the price chart
  showOnlyExecuted: true, // hide BUY/SELL markers that never opened/closed a position (on by default)
  priceSeries: null, // candlestick series (markers live here)
  overlaySeries: [], // overlay/volume series added on top of the candles
  shadeSeries: null, // "position held" band series (painted behind the candles)
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) { /* ignore */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

async function refresh() {
  try {
    const s = await api("/api/v1/dataset/status");
    render(s);
  } catch (err) {
    $("download-msg").textContent = `Status check failed: ${err.message}`;
  }
}

function render(s) {
  state.status = s;
  const hasData = s.exists;
  $("no-data").hidden = hasData;
  $("dashboard").hidden = !hasData;
  $("right-panels").hidden = !hasData;

  if (hasData) {
    renderSummary(s);
    loadChart().then(() => { // chart first, then overlays/pane indicators
      loadIndicators();
      loadTable();
      loadDelta();
      loadSignals();
      loadBacktest();
    });
  } else {
    renderDownloadCard(s);
    const hs = $("header-summary");
    if (hs) { hs.hidden = true; hs.innerHTML = ""; }
  }
}

/* ---------- State A: download card ---------- */
function renderDownloadCard(s) {
  const btn = $("download-btn");
  const msg = $("download-msg");
  $("download-desc").textContent =
    `Asset ${s.symbol} (${s.interval} bars). The dataset doesn't exist yet — ` +
    `download the initial history to get started.`;
  btn.disabled = s.job.running;
  if (s.job.running) {
    msg.textContent = "Downloading…";
    setTimeout(refresh, POLL_MS); // poll until the job finishes
  } else if (s.job.last_error) {
    msg.textContent = `Last attempt failed: ${s.job.last_error}`;
  } else {
    msg.textContent = "";
  }
}

async function startBackfill() {
  const btn = $("download-btn");
  btn.disabled = true;
  $("download-msg").textContent = "Starting download…";
  try {
    const r = await api("/api/v1/dataset/backfill", { method: "POST" });
    if (r.started) {
      $("download-msg").textContent = "Downloading…";
      setTimeout(refresh, POLL_MS);
    } else {
      $("download-msg").textContent = r.reason || "Backfill already running.";
    }
  } catch (err) {
    $("download-msg").textContent = `Failed to start: ${err.message}`;
    btn.disabled = false;
  }
}

/* ---------- State B: summary (now in the header) + chart ---------- */
function barSizeLabel(code) {
  return ({
    "1h": "1 hour", "2h": "2 hours", "4h": "4 hours", "8h": "8 hours",
    "12h": "12 hours", "1d": "1 day", "1W": "1 week", "1M": "1 month",
  })[code] || code;
}

function periodLabel(years) {
  if (years == null || years === "") return "—";
  const n = Number(years);
  return Number.isFinite(n) ? `${n} year${n === 1 ? "" : "s"}` : String(years);
}

// The delta UI always counts BARS (a daily candle is one bar too).
function barWord(n) {
  return `bar${n === 1 ? "" : "s"}`;
}

function renderSummary(s) {
  const host = $("header-summary");
  if (!host) return;
  const last = s.last_price != null ? `$${s.last_price.toFixed(2)}` : "—";
  host.innerHTML = `
    <div class="stats">
      <div><span class="label">Symbol</span><span>${s.symbol}</span></div>
      <div><span class="label">Historical Period</span><span>${periodLabel(s.period_years)}</span></div>
      <div><span class="label">Historical Bar Size</span><span>${barSizeLabel(s.interval)}</span></div>
      <div><span class="label">Rows</span><span>${s.rows.toLocaleString()}</span></div>
      <div><span class="label">Last close</span><span>${last}</span></div>
    </div>`;
  host.hidden = false;
}

async function loadChart() {
  try {
    if (!state.datasetRows) {
      const d = await api("/api/v1/dataset/data?limit=0"); // all rows for the chart
      state.datasetRows = d.rows;
    }
    // A dataset with no usable bars has nothing to draw, and a blank pane is
    // indistinguishable from a broken chart — so say what is actually wrong
    // (this is the state you land in after deleting the data and before the
    // download finishes).
    const hasBars = (state.datasetRows || []).some(
      (r) => toNum(r.open) !== null && toNum(r.close) !== null
    );
    if (!hasBars) {
      if (state.chart) {
        forgetCrosshairAnchor(state.chart);
        state.chart.remove();
        state.chart = null;
      }
      $("chart-canvas").innerHTML =
        '<p class="muted chart-empty">No price data to chart yet — use ' +
        '“Download initial historical data” in the Historical Data panel, ' +
        'then reload.</p>';
      return;
    }
    buildMainChart();
  } catch (err) {
    $("chart-canvas").textContent = `Chart failed to load: ${err.message}`;
  }
}

// Coerce a numeric field, mapping null/undefined/""/NaN to null. Provider
// payloads can carry a placeholder row with no close (e.g. a session that has
// not settled yet); passing that straight to lightweight-charts makes it
// reject the payload and draw nothing at all.
function toNum(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function buildMainChart() {
  // Remember the current zoom/scroll so a rebuild (indicator or marker
  // toggle) does NOT reset the view back to 100%.
  const prevRange = state.chart ? state.chart.timeScale().getVisibleLogicalRange() : null;
  // Dispose any previous instance so stale overlay lines never linger.
  if (state.chart) {
    forgetCrosshairAnchor(state.chart); // a disposed chart must not be synced
    state.chart.remove();
    state.chart = null;
  }
  $("chart-canvas").innerHTML = "";

  const chart = LightweightCharts.createChart($("chart-canvas"), {
    height: 380,
    layout: { background: { color: "#11141a" }, textColor: "#cfd6e4" },
    grid: { vertLines: { color: "#22262f" }, horzLines: { color: "#22262f" } },
    timeScale: { timeVisible: false, borderColor: "#333a46" },
    rightPriceScale: { borderColor: "#333a46" },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    // Zoom happens ONLY on a trackpad/touch pinch (see onMainChartWheel).
    // A plain mouse wheel must never zoom the chart, and dragging the time
    // axis must not zoom either — moving the chart left/right only pans it.
    handleScroll: { mouseWheel: true }, // horizontal wheel/drag still pans
    handleScale: {
      mouseWheel: false,
      axisPressedMouseMove: { time: false, price: true },
    },
  });
  state.chart = chart;
  subscribeMainToOscTime(); // oscillator panes follow the main chart's zoom

  // Full-height "position held" band. It is drawn FIRST (before the candles)
  // so it paints as background; it is excluded from autoscale, so it can never
  // move the price range. applyPositionShades() fills it between each real
  // IN/OUT once the signal fills are loaded.
  let lo = Infinity, hi = -Infinity;
  (state.datasetRows || []).forEach((r) => {
    if (r.low < lo) lo = r.low;
    if (r.high > hi) hi = r.high;
  });
  state.shadeSeries = Number.isFinite(lo) && Number.isFinite(hi)
    ? chart.addHistogramSeries({
        priceScaleId: "right",
        base: lo,
        priceLineVisible: false,
        lastValueVisible: false,
        autoscaleInfoProvider: () => null, // never influence the price scale
      })
    : null;

  const series = chart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });
  state.priceSeries = series;
  state.overlaySeries = []; // overlay/volume series are toggled in place
  // Bars with a missing price are skipped, never passed through: a single null
  // makes lightweight-charts reject the whole payload and draw NOTHING, so one
  // blank row would leave the entire chart empty. The data layer filters these
  // out as well — this is the last line of defence.
  series.setData((state.datasetRows || [])
    .map((r) => ({
      // unix seconds for intraday, 'YYYY-MM-DD' for daily bars
      time: r.time != null ? r.time : r.date,
      open: toNum(r.open), high: toNum(r.high),
      low: toNum(r.low), close: toNum(r.close),
    }))
    .filter((b) => b.time != null && b.open !== null && b.high !== null
      && b.low !== null && b.close !== null));

  // Crosshair anchor: the close at each bar, so hovering an indicator pane can
  // place THIS chart's horizontal line on the price of that instant.
  state.priceValues = new Map();
  let lastClose = null;
  (state.datasetRows || []).forEach((r) => {
    const key = r.time != null ? r.time : r.date;
    const close = toNum(r.close);
    if (key == null || close === null) return;
    state.priceValues.set(key, close);
    lastClose = close;
  });
  registerCrosshairAnchor(chart, series, state.priceValues, lastClose);

  addPriceOverlays(chart);
  drawVolumeBars(chart);
  drawSignalMarkers();
  applyPositionShades(); // fill held periods behind the candles (once known)
  // Keep the user's current zoom when this was a refresh caused by a toggle;
  // only fit the full history on the very first draw (or a true data reset).
  if (prevRange) {
    chart.timeScale().setVisibleLogicalRange(prevRange);
  } else {
    chart.timeScale().fitContent();
  }
}

// Volume bars live at the BOTTOM of the main chart (same chart + time axis as
// the candles) — not in a separate chart — like a classic volume sub-pane.
// Returns true when volume bars were drawn (caller may rely on the margins).
function drawVolumeBars(chart) {
  const hist = (state.indicators?.overlays || []).filter(
    (o) => o.kind === "histogram" && state.selected[o.key]
  );
  if (!hist.length) {
    // No volume bars: candles get the full pane again.
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.04, bottom: 0.0 } });
    return false;
  }

  // Color each bar by the direction of its candle (up=green, down=red).
  const dir = new Map();
  (state.datasetRows || []).forEach((r) => {
    const key = r.time != null ? r.time : r.date;
    dir.set(key, r.close >= r.open ? "#26a69a" : "#ef5350");
  });

  // Shrink the price scale so candles use the top ~72% of the pane and leave
  // the bottom band empty for the volume bars underneath them.
  chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.04, bottom: 0.28 } });

  hist.forEach((o) => {
    const vs = chart.addHistogramSeries({
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      priceLineVisible: false,
      lastValueVisible: false,
    });
    const pts = ((o.lines && o.lines[0] && o.lines[0].data) || []).map((p) => ({
      time: p.time,
      value: p.value,
      color: dir.get(p.time) || "#4c8dff",
    }));
    vs.setData(pts);
    state.overlaySeries.push(vs);
  });
  // Volume fills exactly the reserved bottom band.
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.72, bottom: 0.0 } });
  return true;
}

/* ---------- Main chart wheel: pan vs zoom ---------- */
// The library treats ANY vertical wheel delta as zoom, so a two-finger swipe
// that carries even a small vertical component zooms while panning. We turn
// wheel-zoom OFF on the chart and zoom only on a real trackpad pinch, which
// the browser reports as a wheel event with ctrlKey set (kept by pinch, and
// also enabled via handleScale.pinch for touchscreens). Horizontal deltas
// keep panning through the library's own handler.
let _mainWheelBound = false;

function ensureMainChartWheel() {
  const el = $("chart-canvas");
  if (!el || _mainWheelBound) return;
  _mainWheelBound = true;
  el.addEventListener("wheel", onMainChartWheel, { passive: false });
}

function onMainChartWheel(ev) {
  if (!ev.ctrlKey) return; // plain wheel never zooms — leave panning to the library
  ev.preventDefault(); // stop the browser from zooming the whole page instead
  const chart = state.chart;
  if (!chart) return;
  const ts = chart.timeScale();
  const range = ts.getVisibleLogicalRange();
  if (!range || range.to <= range.from) return;
  const box = $("chart-canvas").getBoundingClientRect();
  if (!box.width) return;
  const x = Math.max(0, Math.min(box.width, ev.clientX - box.left));
  const anchor = range.from + (x / box.width) * (range.to - range.from);
  const span = range.to - range.from;
  const factor = Math.exp(-ev.deltaY * 0.005); // pinch out (negative delta) -> zoom in
  const newSpan = Math.max(1, span / factor);
  const newFrom = anchor - (anchor - range.from) * (newSpan / span);
  ts.setVisibleLogicalRange({ from: newFrom, to: newFrom + newSpan });
}

/* ---------- Strategy Signals panel (rule-based model test) ---------- */
async function loadSignals() {
  const host = $("signals");
  if (!host) return;
  try {
    const d = await api("/api/v1/signal");
    state.signalData = d;
    renderSignals(d);
    applySignalMarkers(); // markers only — the chart itself is not rebuilt
    applyPositionShades(); // ... and the IN/OUT background bands
  } catch (err) {
    host.innerHTML =
      `<h2>Strategy Signals</h2>` +
      `<p class="muted">Signals unavailable: ${escapeHtml(err.message)}</p>`;
  }
}

function renderSignals(d) {
  const host = $("signals");
  if (!host) return;
  if (!d || d.available === false) {
    host.innerHTML = `
      <div class="signal-head">
        <h2>Strategy Signals</h2>
      </div>
      <p class="muted signal-reason">${escapeHtml((d && d.reason) || "Signals unavailable for the current model type.")}</p>
      <p class="muted signal-counts">model type: ${escapeHtml((d && d.model_type) || "?")} · rule signals are not shown</p>`;
    return;
  }
  const latest = d.latest;
  const sig = latest ? latest.signal : "HOLD";
  const badgeClass = sig === "BUY" ? "buy" : sig === "SELL" ? "sell" : "hold";
  const conf = latest && latest.confidence != null ? latest.confidence : null;
  const when = latest ? latest.time : null;
  const reason = latest && latest.reason ? latest.reason : "";
  const counts = d.counts || {};
  host.innerHTML = `
    <div class="signal-head">
      <h2>Strategy Signals</h2>
      <span class="signal-toggles">
        <label class="signal-toggle" title="Draw BUY/SELL markers on the price chart">
          <input type="checkbox" id="signals-toggle" onchange="onSignalsToggle()" ${state.showSignals ? "checked" : ""}>
          markers on chart
        </label>
        <label class="signal-toggle" title="Show only the BUY/SELL that actually opened or closed a position">
          <input type="checkbox" id="exec-signals-toggle" onchange="onExecToggle()" ${state.showOnlyExecuted ? "checked" : ""}>
          hide unexecuted signals
        </label>
      </span>
    </div>
    <div class="signal-latest">
      <span class="signal-badge ${badgeClass}">${sig}</span>
      <span class="signal-meta">
        ${when ? `<span class="label">as of</span><span>${escapeHtml(when)}</span>` : ""}
        ${conf != null ? `<span class="label">conf</span><span>${conf}</span>` : ""}
      </span>
    </div>
    ${reason ? `<p class="muted signal-reason">${escapeHtml(reason)}</p>` : ""}
    <p class="muted signal-counts">over the dataset: ${counts.BUY ?? 0} BUY · ${counts.SELL ?? 0} SELL · ${counts.HOLD ?? 0} HOLD</p>
    ${fillsNote(d)}`;
}

// The fills line: how many positions were actually taken, why they ended, and
// how many entries the risk layer refused. Mirrors the Backtest panel, because
// both replay the same risk layer over the same signals.
function fillsNote(d) {
  const risk = d.risk || {};
  const rounds = (d.fills || []).filter((f) => f.kind === "close").length;
  const vetoes = (d.vetoed || []).length;
  if (!rounds && !vetoes) return "";
  const bits = [];
  const sized = risk.applied && risk.weight != null && Math.abs(risk.weight - 1) > 1e-9
    ? ` at ${(risk.weight * 100).toFixed(0)}% of the account`
    : "";
  if (risk.applied === false) {
    bits.push("<b>risk layer off</b>: the raw strategy's positions");
  } else {
    const why = [];
    if (risk.stop_exits) why.push(`${risk.stop_exits} stopped out`);
    if (risk.take_exits) why.push(`${risk.take_exits} take profit`);
    if (risk.signal_exits) why.push(`${risk.signal_exits} on the opposite signal`);
    if (risk.forced_exits) why.push(`${risk.forced_exits} still open at the end`);
    if (why.length) bits.push(why.join(" · "));
    if (vetoes) {
      bits.push(`${vetoes} entr${vetoes === 1 ? "y" : "ies"} vetoed by the circuit breaker`);
    }
  }
  const shaded = " — the shaded background marks the periods a position was actually " +
    "held (green = the round trip made money, red = it lost" +
    (risk.applied && (risk.stop_exits || risk.take_exits) ? ", ✕ marks a stop/take exit" : "") +
    ")";
  return `<p class="muted signal-fills">actual fills: ${rounds} round-trip(s)${sized}` +
    (bits.length ? ` — ${bits.join(" · ")}` : "") +
    `${shaded}</p>`;
}

function computeSignalMarkers() {
  if (!state.showSignals || !state.signalData) return [];
  const d = state.signalData;
  // Only the raw BUY/SELL decisions are drawn as markers. Actual position
  // opens/closes are conveyed by the background shade (applyPositionShades),
  // so a redundant IN/OUT marker never clutters a candle. One marker per bar.
  // "hide unexecuted signals" (showOnlyExecuted) keeps only the decisions
  // whose bar sits right before a real fill — the BUY/SELL that actually
  // opened/closed a position.
  const executed = state.showOnlyExecuted ? executedSignalTimes() : null;
  const seen = new Set();
  const out = [];
  (d.series || [])
    .filter((p) => p.signal === "BUY" || p.signal === "SELL")
    .forEach((p) => {
      if (seen.has(p.time)) return; // at most one marker per bar
      if (executed && !executed.has(String(p.time))) return; // never filled
      seen.add(p.time);
      out.push({
        time: p.time,
        text: p.signal,
        color: p.signal === "BUY" ? "#26a69a" : "#ef5350",
        position: p.signal === "BUY" ? "belowBar" : "aboveBar",
        shape: p.signal === "BUY" ? "arrowUp" : "arrowDown",
      });
    });
  // Where the risk layer closed a position instead of the opposite signal: a
  // stop (red) or a take profit (green), drawn on the side the price moved to,
  // so a stopped-out leg is obvious at a glance.
  (d.fills || []).forEach((f) => {
    if (f.kind !== "close" || (f.reason !== "stop" && f.reason !== "take")) return;
    const long = f.side !== "short";
    const stop = f.reason === "stop";
    out.push({
      time: f.time,
      text: "✕",
      color: stop ? "#ef5350" : "#26a69a",
      position: long === stop ? "belowBar" : "aboveBar",
      shape: "circle",
    });
  });
  // Entries the circuit breaker refused: no position was ever opened, so these
  // are NOT fills — a muted "veto" says the strategy went quiet on purpose.
  (d.vetoed || []).forEach((v) => {
    out.push({
      time: v.time,
      text: "veto",
      color: "#9e9e9e",
      position: "inBar",
      shape: "square",
    });
  });
  out.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return out;
}

// Times of the BUY/SELL decisions that actually became a fill. A position
// opens/closes at the NEXT bar's open after the signal, so the executed signal
// is the dataset bar immediately before each fill. Only entries and
// signal-driven exits qualify: a stop or take-profit exit fires mid-position
// with no decision of its own, so it proves nothing about the bar before it.
function executedSignalTimes() {
  const executed = new Set();
  const rows = state.datasetRows || [];
  const idx = new Map();
  rows.forEach((r, i) => idx.set(String(r.time != null ? r.time : r.date), i));
  (state.signalData ? state.signalData.fills || [] : []).forEach((f) => {
    if (f.kind !== "open" && f.reason !== "signal") return;
    const i = idx.get(String(f.time));
    if (i != null && i > 0) {
      const prev = rows[i - 1];
      executed.add(String(prev.time != null ? prev.time : prev.date));
    }
  });
  return executed;
}

function applySignalMarkers() {
  // Markers live on the candlestick series — swapping them never rebuilds
  // the chart, so the user's zoom/scroll is preserved.
  if (state.priceSeries) state.priceSeries.setMarkers(computeSignalMarkers());
}

function drawSignalMarkers() {
  applySignalMarkers();
}

function onSignalsToggle() {
  const t = $("signals-toggle");
  state.showSignals = t ? t.checked : false;
  applySignalMarkers();
  applyPositionShades(); // shade held periods only while markers are shown
}

function onExecToggle() {
  const t = $("exec-signals-toggle");
  state.showOnlyExecuted = t ? t.checked : false;
  applySignalMarkers(); // markers only — the held-period shade is unaffected
}

// Shade the chart background from each real position open to close with a
// faint fill. It consumes the SAME risk-layer fills the Backtest panel and the
// report use, so a leg that was stopped out early is shaded only up to its
// stop, and a vetoed entry is never shaded at all. The band's colour reports the
// round trip's outcome: green made money, red lost, blue still open at the end.
function applyPositionShades() {
  const shade = state.shadeSeries;
  if (!shade) return;
  const rows = state.datasetRows;
  const fills = state.signalData ? state.signalData.fills : null;
  if (!state.showSignals || !rows || !fills || !fills.length) {
    shade.setData([]);
    return;
  }
  let lo = Infinity, hi = -Infinity;
  rows.forEach((r) => {
    if (r.low < lo) lo = r.low;
    if (r.high > hi) hi = r.high;
  });
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
    shade.setData([]);
    return;
  }
  const rowTime = (r) => (r.time != null ? r.time : r.date);
  const idx = new Map();
  rows.forEach((r, i) => idx.set(String(rowTime(r)), i));
  const pts = [];
  let openIdx = null;
  const emit = (o, c, color) => {
    if (o == null || c == null || c < o) return;
    for (let i = o; i <= c; i++) {
      pts.push({ time: rowTime(rows[i]), value: hi, color: color || POSITION_SHADE });
    }
  };
  // The outcome is carried by the round trip's CLOSE fill; a fill without one
  // (an older payload) keeps the neutral tint rather than guessing.
  const shadeFor = (f) => (f.win === true ? POSITION_SHADE_WIN
    : f.win === false ? POSITION_SHADE_LOSS : POSITION_SHADE);
  fills.forEach((f) => {
    const i = idx.get(String(f.time));
    if (i == null) return; // fill time not on this dataset (shouldn't happen)
    if (f.kind === "open") {
      openIdx = i;
    } else {
      emit(openIdx, i, shadeFor(f));
      openIdx = null;
    }
  });
  if (openIdx != null) emit(openIdx, rows.length - 1); // still held — outcome unknown
  shade.setData(pts);
}

async function loadTable() {
  try {
    const d = await api(`/api/v1/dataset/data?limit=${PAGE_SIZE}&offset=${state.offset}`);
    state.total = d.total;
    state.rows = d.rows;
    renderTable();
  } catch (err) {
    $("data-table").tBodies[0].innerHTML = `<tr><td colspan="6">${err.message}</td></tr>`;
  }
}

function renderTable() {
  const body = $("data-table").tBodies[0];
  body.innerHTML = state.rows
    .map((r) => `<tr>
        <td>${r.datetime || r.date}</td>
        <td>${r.open.toFixed(2)}</td>
        <td>${r.high.toFixed(2)}</td>
        <td>${r.low.toFixed(2)}</td>
        <td>${r.close.toFixed(2)}</td>
        <td>${r.volume.toLocaleString()}</td>
      </tr>`)
    .join("");
  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + PAGE_SIZE, state.total);
  $("page-info").textContent = `${from}–${to} of ${state.total}`;
  $("prev-btn").disabled = state.offset <= 0;
  $("next-btn").disabled = state.offset + PAGE_SIZE >= state.total;
}

function prevPage() {
  if (state.offset > 0) { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadTable(); }
}

function nextPage() {
  if (state.offset + PAGE_SIZE < state.total) { state.offset += PAGE_SIZE; loadTable(); }
}

/* ---------- Settings (.env) form ---------- */
async function loadConfig(silent) {
  try {
    const c = await api("/api/v1/config");
    state.config = c;
    renderConfig(c);
    if (!silent) {
      $("config-msg").textContent = c.file_exists
        ? `Editing ${c.file}`
        : `No ${c.file} yet — saving will create it.`;
    }
  } catch (err) {
    $("config-msg").textContent = `Failed to load config: ${err.message}`;
  }
}

function renderConfig(c) {
  const container = $("settings-fields");
  container.innerHTML = "";
  for (const section of c.sections) {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "cfg-section";
    const legend = document.createElement("legend");
    legend.textContent = section.name;
    fieldset.appendChild(legend);
    for (const f of section.fields) {
      fieldset.appendChild(fieldInput(f));
    }
    container.appendChild(fieldset);
  }
}

function fieldInput(f, prefix) {
  prefix = prefix || "cfg";
  const wrap = document.createElement("div");
  wrap.className = "field" + (f.readonly ? " readonly" : "");
  wrap.dataset.key = f.key;

  const label = document.createElement("label");
  label.htmlFor = prefix + "-" + f.key;
  label.textContent = f.label;
  label.title = f.key;
  if (f.sensitive) label.classList.add("secret");
  if (f.readonly) label.classList.add("locked");
  wrap.appendChild(label);

  let control;
  if (f.options && f.options.length) {
    control = document.createElement("select");
    for (const o of f.options) {
      const opt = document.createElement("option");
      const val = typeof o === "object" && o !== null ? o.value : o;
      const label = typeof o === "object" && o !== null ? (o.label != null ? o.label : o.value) : o;
      opt.value = val;
      opt.textContent = label;
      opt.selected = String(val) === String(f.value);
      control.appendChild(opt);
    }
  } else if (f.type === "bool") {
    control = document.createElement("input");
    control.type = "checkbox";
    control.className = "switch";
    control.checked = String(f.value) === "True";
  } else if (f.readonly) {
    control = document.createElement("input");
    control.type = "text";
    control.disabled = true;
    control.classList.add("ro");
    control.value = f.value == null ? "" : String(f.value);
  } else if (f.sensitive) {
    control = document.createElement("input");
    control.type = "password";
    control.placeholder = f.set ? f.value : "(unset)";
  } else {
    control = document.createElement("input");
    control.type = f.type === "int" || f.type === "float" ? "number" : "text";
    if (f.type === "int") control.step = "1";
    if (f.type === "float") control.step = "any";
    if (f.min != null) control.min = String(f.min);
    if (f.max != null) control.max = String(f.max);
    control.value = f.value == null ? "" : String(f.value);
  }
  control.id = prefix + "-" + f.key;
  control.name = f.key;
  wrap.appendChild(control);

  const hints = Array.isArray(f.hints) && f.hints.length
    ? f.hints
    : (f.description ? [f.description] : []);
  hints.forEach((txt) => {
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.textContent = txt;
    wrap.appendChild(hint);
  });
  if (f.readonly) {
    const hint = document.createElement("span");
    hint.className = "hint ro-note";
    hint.textContent = f.readonly_note || "Read-only — change it directly in .env";
    wrap.appendChild(hint);
  }
  // A credential pair gets its verdict and a Validate button, placed by the schema
  // (f.verify) rather than by a hard-coded key list.
  if (f.verify) {
    const creds = (state.account && state.account.credentials) || {};
    wrap.appendChild(credentialRow(f.verify, creds[f.verify.env]));
  }
  return wrap;
}

/* ---------- Alpaca credential verification ----------
   Having a key pair configured is not the same as having one that WORKS: keys get
   copied from the wrong account page, revoked, or paired with the other
   environment's secret. Nothing local can tell, so the popup offers to ask Alpaca
   and reports what it said — and turning trading on is gated on the answer.

   The badge reports a VERDICT and nothing else: no verdict about the pair in play
   means no badge at all, because "not checked" is not news and a label sitting there
   before anyone has asked is just noise. */
function credWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : `${d.toISOString().slice(11, 16)} UTC`;
}

function credentialRow(spec, cred) {
  const row = document.createElement("div");
  row.className = "cred-row";
  row.dataset.env = spec.env;
  const badge = document.createElement("span");
  badge.id = "cred-badge-" + spec.env;
  row.appendChild(badge);
  applyCredentialBadge(badge, cred || {});
  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "cred-verify-" + spec.env;
  btn.className = "ghost small";
  btn.textContent = `Validate ${spec.env} credentials`;
  btn.title = `Ask Alpaca whether these ${spec.env} credentials work`;
  btn.onclick = () => validateCredentials(spec, btn);
  row.appendChild(btn);
  return row;
}

function applyCredentialBadge(badge, cred) {
  if (!badge) return;
  const c = cred || {};
  // Only a verdict ABOUT the pair in play is worth showing.
  badge.hidden = !c.has_verdict;
  if (!c.has_verdict) {
    badge.className = "cred-badge";
    badge.textContent = "";
    badge.title = "";
    return;
  }
  if (c.verified) {
    const bits = ["✓ verified"];
    if (c.account_number) bits.push(c.account_number);
    const when = credWhen(c.checked_at);
    if (when) bits.push(when);
    badge.className = "cred-badge ok";
    badge.textContent = bits.join(" · ");
    badge.title = c.message || "These credentials were accepted by Alpaca.";
  } else {
    // The reason matters more than the verdict, but it can be long: short label,
    // full text on hover, and the popup's message area repeats it.
    badge.className = "cred-badge bad";
    badge.textContent = "⚠ not valid";
    badge.title = c.message || "Verification failed.";
  }
}

// Updates the badges IN PLACE. The popup must never be re-rendered here: the
// operator may have just typed new keys, and a re-render would discard them.
function renderCredentialState(creds, exceptEnv) {
  if (!creds) return;
  for (const env of Object.keys(creds)) {
    if (env === exceptEnv) continue; // its own verdict is applied separately
    applyCredentialBadge(document.getElementById("cred-badge-" + env), creds[env]);
  }
}

// Validates what is IN THE FORM, not only what has been saved: a pair is normally
// typed and then validated, and checking the stored values instead would answer
// "credentials are not supplied" about keys visibly sitting in the boxes. A blank
// field falls back to the stored value, exactly like a save does.
async function validateCredentials(spec, btn) {
  const env = spec.env;
  const keyEl = document.getElementById("acct-" + spec.key_key);
  const secretEl = document.getElementById("acct-" + spec.secret_key);
  const body = {
    env: env,
    key_id: keyEl ? keyEl.value : "",
    secret: secretEl ? secretEl.value : "",
  };
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/v1/account/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const res = r.result || r;
    renderCredentialState(r.credentials, env); // the other pair, from stored state
    applyCredentialBadge(document.getElementById("cred-badge-" + env), res);
    showAccountErrors(credentialMessage(env, res), res.ok ? "ok" : "warn");
    flashToast(res.message || "", res.ok ? "ok" : "warn");
    await loadTrading(); // a pass may just have opened the switch
  } catch (err) {
    showAccountErrors(`Could not verify ${env} credentials: ${err.message}`, "warn");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// What to say after a validation attempt: nothing to check, rejected, or accepted
// (with a nudge when the valid pair is not the saved one yet).
function credentialMessage(env, res) {
  const label = env.toUpperCase();
  if (!res.checked) return res.message || `Nothing to verify for ${label}.`;
  if (!res.ok) return `${label} credentials are NOT valid: ${res.message}`;
  const where = res.account_number ? ` (account ${res.account_number})` : "";
  const saved = res.saved === false
    ? " — press 💾 Save to store them, they are not the saved pair yet."
    : "";
  return `${label} credentials are valid${where}.${saved}`;
}

function showSaveErrors(text, kind) {
  const el = $("save-errors");
  if (!el) return;
  el.classList.toggle("warn", kind === "warn");
  el.textContent = text || "";
  el.hidden = !text;
}

async function saveConfig() {
  const btn = $("save-config");

  // 1) Read every field from the rendered form.
  const values = {};
  for (const section of state.config.sections) {
    for (const f of section.fields) {
      const el = document.getElementById("cfg-" + f.key);
      if (!el) continue;
      // checkbox switches submit True/False; empty password => keep existing
      values[f.key] = el.type === "checkbox" ? (el.checked ? "True" : "False") : el.value;
    }
  }

  // 1b) Range-check numeric fields up front so an out-of-bounds value (e.g. a
  // mis-typed Gate threshold) can't be submitted in the first place.
  const rangeErrors = [];
  const offenders = new Set();
  for (const section of state.config.sections) {
    for (const f of section.fields) {
      if (f.type !== "int" && f.type !== "float") continue;
      if (f.min == null && f.max == null) continue;
      const el = document.getElementById("cfg-" + f.key);
      if (!el) continue;
      const n = Number(el.value);
      if (!el.value.trim() || Number.isNaN(n)) {
        rangeErrors.push(`${f.label}: enter a number`);
        offenders.add(f.key);
        continue;
      }
      if (f.min != null && n < f.min) { rangeErrors.push(`${f.label}: min is ${f.min}`); offenders.add(f.key); }
      if (f.max != null && n > f.max) { rangeErrors.push(`${f.label}: max is ${f.max}`); offenders.add(f.key); }
    }
  }
  showSaveErrors("");
  if (rangeErrors.length) {
    showSaveErrors("✗ Not saved — " + rangeErrors.join(" · "));
    // Flag every offending field in red; clear the flag as soon as it's edited.
    offenders.forEach((key) => {
      const el = document.getElementById("cfg-" + key);
      if (!el) return;
      el.classList.add("invalid");
      const clearInvalid = () => {
        el.classList.remove("invalid");
        el.removeEventListener("input", clearInvalid);
      };
      el.addEventListener("input", clearInvalid);
    });
    return;
  }

  // 2) Changing the instrument is significant — confirm before saving.
  const oldInstrument = currentInstrument();
  const newInstrument = (values["INSTRUMENT"] || "").trim().toUpperCase();
  const instrumentChanged =
    !!newInstrument && !!oldInstrument && newInstrument !== oldInstrument.toUpperCase();

  if (instrumentChanged) {
    const ok = await confirmDialog({
      title: "Change trading instrument?",
      messageHtml:
        `<p>You're attempting to change the instrument that the bot is configured to work with — ` +
        `from <b>${escapeHtml(oldInstrument.toUpperCase())}</b> to <b>${escapeHtml(newInstrument)}</b>.</p>` +
        `<p>Are you sure you want that?</p>` +
        `<p class="muted">The dashboard will switch to ${escapeHtml(newInstrument)}. If no historical data ` +
        `file exists for it yet, a new one will be downloaded via the “Download Historical Data” button.</p>`,
      confirmText: "Yes, switch",
      cancelText: "No",
    });
    if (!ok) {
      showSaveErrors("Save cancelled — instrument unchanged.", "warn");
      return;
    }
  }

  btn.disabled = true;
  showSaveErrors(""); // clear any earlier validation notice while saving
  try {
    const r = await api("/api/v1/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    if (r.ok) {
      await loadConfig(true); // re-read so secrets re-mask and values refresh
      if (r.instrument_changed) switchDataset(newInstrument); // reloads dashboard
      closeGlobalSettings(); // success -> dismiss the dialog
    } else {
      showSaveErrors((r.errors || []).join("; ") || r.message);
    }
  } catch (err) {
    showSaveErrors(`Save failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

function currentInstrument() {
  if (state.config) {
    for (const section of state.config.sections) {
      for (const f of section.fields) {
        if (f.key === "INSTRUMENT") return (f.value || "").trim();
      }
    }
  }
  return state.status && state.status.symbol ? state.status.symbol : "";
}

function switchDataset(symbol) {
  // Tear down everything tied to the previous instrument's dataset/chart.
  if (state.chart) {
    forgetCrosshairAnchor(state.chart); // a disposed chart must not be synced
    try { state.chart.remove(); } catch (_) { /* noop */ }
  }
  state.chart = null;
  (state.oscCharts || []).forEach((c) => {
    forgetCrosshairAnchor(c);
    try { c.remove(); } catch (_) { /* noop */ }
  });
  state.oscCharts = [];
  state.overlayLines = [];
  state.selected = {};
  state.indicators = null;
  state.datasetRows = null;
  state.rows = [];
  state.total = 0;
  state.offset = 0;

  const panes = $("indicator-panes");
  if (panes) panes.innerHTML = "";
  const canvas = $("chart-canvas");
  if (canvas) canvas.innerHTML = "";
  const toggles = $("indicator-toggles");
  if (toggles) toggles.innerHTML = "";

  // Re-query dataset status for the new instrument: if a Parquet file exists
  // for it we render the full dashboard; otherwise the portal falls back to
  // the "Download Historical Data" state.
  refresh();
}

/* ---------- Confirm dialog ---------- */
function confirmDialog(opts) {
  return new Promise((resolve) => {
    const backdrop = $("modal-backdrop");
    const title = $("modal-title");
    const message = $("modal-message");
    const confirmBtn = $("modal-confirm");
    const cancelBtn = $("modal-cancel");

    title.textContent = opts.title || "Are you sure?";
    message.innerHTML = opts.messageHtml || "";
    confirmBtn.textContent = opts.confirmText || "Yes";
    cancelBtn.textContent = opts.cancelText || "No";

    const close = (result) => {
      backdrop.hidden = true;
      confirmBtn.onclick = null;
      cancelBtn.onclick = null;
      backdrop.onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    };
    confirmBtn.onclick = () => close(true);
    cancelBtn.onclick = () => close(false);
    backdrop.onclick = (e) => { if (e.target === backdrop) close(false); };
    document.addEventListener("keydown", onKey);
    backdrop.hidden = false;
    confirmBtn.focus();
  });
}

/* ---------- Global (.env) settings dialog ---------- */
function openGlobalSettings() {
  const backdrop = $("global-settings-backdrop");
  if (!backdrop) return;
  if (!state.config) loadConfig(true); // render the fields before showing
  showSaveErrors(""); // no stale validation notice from a previous open/save
  backdrop.hidden = false;
}

function closeGlobalSettings() {
  const backdrop = $("global-settings-backdrop");
  if (backdrop) backdrop.hidden = true;
}

(function initGlobalSettingsModal() {
  const backdrop = $("global-settings-backdrop");
  if (!backdrop) return;
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeGlobalSettings();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !backdrop.hidden) closeGlobalSettings();
  });
})();

/* ---------- Account settings dialog (settings/account/account.json) ---------- */
async function loadAccount(silent) {
  try {
    const c = await api("/api/v1/account");
    state.account = c;
    renderAccount(c);
    if (!silent) {
      $("account-msg").textContent = c.file_exists
        ? `Editing ${c.file}`
        : `No ${c.file} yet — saving will create it.`;
    }
    if (c.error) showAccountErrors(c.error, "warn");
  } catch (err) {
    $("account-msg").textContent = `Failed to load account settings: ${err.message}`;
  }
}

function renderAccount(c) {
  const host = $("account-fields");
  if (!host) return;
  host.innerHTML = "";
  for (const group of c.groups || []) {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "cfg-section";
    const legend = document.createElement("legend");
    legend.textContent = group.name;
    fieldset.appendChild(legend);
    for (const f of group.fields) fieldset.appendChild(fieldInput(f, "acct"));
    host.appendChild(fieldset);
  }
  const fileEl = $("account-file");
  if (fileEl && c.file) fileEl.textContent = c.file;
}

function showAccountErrors(text, kind) {
  const el = $("account-errors");
  if (!el) return;
  el.classList.toggle("warn", kind === "warn");
  el.classList.toggle("ok", kind === "ok");
  el.textContent = text || "";
  el.hidden = !text;
}

function openAccountSettings() {
  const backdrop = $("account-settings-backdrop");
  if (!backdrop) return;
  if (!state.account) loadAccount(true); // render the fields before showing
  showAccountErrors(""); // no stale validation notice from a previous open/save
  backdrop.hidden = false;
}

function closeAccountSettings() {
  const backdrop = $("account-settings-backdrop");
  if (backdrop) backdrop.hidden = true;
}

/* ---------- Execution: env dropdown, trading switch, config lock ----------
   The header dropdown IS the paper/live control, and it is coloured by state
   (teal paper / red live / amber when orders would be refused), so "is this bot
   about to send REAL orders?" is always on screen. A paper and a live account are
   indistinguishable everywhere else — which is exactly how live orders get sent
   by accident.

   Trading ON freezes every configuration surface. The server enforces that with a
   409; applyConfigLock() only mirrors it so nothing is clickable that would be
   rejected. Everything here is non-throwing: a panel problem must not take the
   dashboard down, and it must never silently UNlock. */
async function loadTrading() {
  let d = null;
  try {
    d = await api("/api/v1/trading");
  } catch (_) {
    return; // keep the last known state rather than unlocking by accident
  }
  state.tradingPayload = d;
  state.tradingState = d.trading || {};
  state.executionStatus = d.execution || {};
  state.tradingLocked = !!d.locked;
  renderEnvSelect(d);
  renderTradingControls(d);
  renderTradingPanel(d);
  applyConfigLock();
}

function renderEnvSelect(d, force) {
  const sel = $("exec-env");
  if (!sel) return;
  const exec = d.execution || {};
  const opts = d.env_options || [];
  if (opts.length && sel.options.length !== opts.length) {
    sel.innerHTML = "";
    for (const o of opts) {
      const opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label;
      sel.appendChild(opt);
    }
  }
  if (force || document.activeElement !== sel) sel.value = exec.env || "paper";
  // The class is CONSTANT: the pill is styled in exactly one way, in every state,
  // and matches the master switch. So nothing may ride on the class list — the
  // only places a state can show are the words and the status dot.
  sel.className = "exec-pill exec-select";
  sel.title = exec.ok
    ? `${exec.broker} · ${exec.env} — ${exec.base_url}`
    : `Orders would be REFUSED — ${exec.message}`;
}

/* ---------- status dots ----------
   Each pill ends with a dot that repeats what the words say, in colour:
     blue  = the calm state  (paper account, trading off)
     red   = the state that spends money or is live (live account, trading on)
   The red one blinks, because that is the state nobody should miss.

   The dot is part of the TEXT, not a styled element. The account control is a
   native <select>: its options can only contain text, and a select always sizes
   itself to its WIDEST option (width: min-content/fit-content make no difference —
   measured), so a positioned element could never sit at the end of the selected
   label. A glyph behaves the same in both pills, which is what keeps them one
   style. Emoji carry their own colour; a plain text glyph could only inherit the
   pill's text colour, which is identical in every state by design. */
const DOT_CALM = "🔵";
const DOT_ALERT = "🔴";
const DOT_ALERT_OFF = "⚫"; // the invisible half of the blink (dark on dark)
const DOT_PERIOD_MS = 700; // ~1.4 blinks/s: visible, and under the 3 Hz threshold
let _dotPhase = true; // is the alert dot showing right now?
let _dotTimer = null;
let _dotStateKey = ""; // which set of states the current phase belongs to

function prefersReducedMotion() {
  return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function statusDot(state) {
  if (state !== "live" && state !== "on") return DOT_CALM;
  // A blinking dot is motion, so it is the first thing to go when motion is
  // reduced: the alert colour still shows, it just stops flashing.
  if (prefersReducedMotion()) return DOT_ALERT;
  return _dotPhase ? DOT_ALERT : DOT_ALERT_OFF;
}

function renderStatusDots(d) {
  const p = d || state.tradingPayload;
  if (!p) return;
  const tr = p.trading || {};
  const exec = p.execution || {};
  // A state change starts with the dot LIT: inheriting a mid-blink phase would
  // leave a dot that has just turned red dark for up to a period, which reads as
  // "nothing happened".
  const key = `${tr.on}|${exec.live}|${exec.env}|${exec.ok}`;
  if (key !== _dotStateKey) {
    _dotStateKey = key;
    _dotPhase = true;
  }
  const btn = $("trading-toggle");
  if (btn) {
    btn.textContent = `${tr.on ? "⏹ Turn trading off" : "▶ Turn trading on"} ` +
      statusDot(tr.on ? "on" : "off");
  }
  const sel = $("exec-env");
  if (sel) {
    for (const o of p.env_options || []) {
      const opt = Array.from(sel.options).find((x) => x.value === o.value);
      if (!opt) continue;
      // Every option carries its own dot, so the open list shows what each choice
      // means. The account that cannot trade says so in words.
      const blocked = !exec.ok && o.value === (exec.env || "paper");
      opt.textContent = `${o.label}${blocked ? " — ⚠ no keys" : ""} ${statusDot(o.value)}`;
    }
  }
  syncDotTimer(!!(tr.on || exec.live));
}

// ONE timer for both pills, so their dots blink together, and it only runs while
// something is actually blinking.
function syncDotTimer(needed) {
  const should = needed && !prefersReducedMotion();
  if (should && !_dotTimer) {
    _dotTimer = setInterval(() => {
      _dotPhase = !_dotPhase;
      renderStatusDots();
    }, DOT_PERIOD_MS);
  } else if (!should && _dotTimer) {
    clearInterval(_dotTimer);
    _dotTimer = null;
  }
  if (!should) _dotPhase = true; // never leave a dot dark when nothing blinks
}

// A browser may restore a form's value on its own — bfcache, back/forward, a
// crash-recovery session restart — and fire `change` with no user involved. For
// the control that decides which account gets REAL orders, a restore must never
// be mistaken for a deliberate choice, so a change may only be persisted after a
// real gesture on the select itself.
let _envGesture = false;

function watchEnvSelect() {
  const sel = $("exec-env");
  if (!sel) return;
  const arm = () => { _envGesture = true; };
  sel.addEventListener("pointerdown", arm, { once: true });
  sel.addEventListener("keydown", arm, { once: true });
}

async function onEnvChange() {
  const sel = $("exec-env");
  if (!sel) return;
  if (!_envGesture) {
    // Not a choice anyone made: put the display back to what the server last
    // said and write nothing. With no payload yet there is no truth to restore,
    // so leave the control alone rather than guess a mode.
    if (state.tradingPayload) renderEnvSelect(state.tradingPayload, true);
    return;
  }
  const previous = (state.executionStatus || {}).env || "paper";
  const env = sel.value;
  if (env === previous) return;
  if (env === "live") {
    const ok = await confirmDialog({
      title: "Route orders to the LIVE account?",
      messageHtml:
        "Every order for this strategy will go to your <b>real</b> Alpaca account. " +
        "Nothing is sent until you turn trading on.",
      confirmText: "Use the live account",
    });
    if (!ok) {
      sel.value = previous;
      return;
    }
  }
  const r = await api("/api/v1/execution/env", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ env: env }),
  });
  if (r && r.ok === false) {
    flashToast((r.errors || []).join("; ") || r.message || "Could not change the environment", "warn");
    sel.value = previous;
  } else {
    flashToast(r && r.message ? r.message : "Environment updated", "ok");
  }
  await loadTrading();
}

async function toggleTrading() {
  const tr = state.tradingState || {};
  const exec = state.executionStatus || {};
  // Starting ALWAYS asks, in both environments. The wording is what differs: on the
  // live account the point is that real money is at stake, on paper it is simply
  // "this strategy starts acting on the next signal". Starting the bot is a
  // deliberate act whether or not the orders are simulated, and a confirmation that
  // only appears sometimes is a confirmation you stop reading.
  if (!tr.on) {
    const target = escapeHtml(exec.base_url || "");
    const ok = await confirmDialog(
      exec.live
        ? {
            title: "Start trading with REAL money?",
            messageHtml:
              `Orders will go to your live Alpaca account (<code>${target}</code>). ` +
              "You can stop at any time with <b>Turn trading off</b>.",
            confirmText: "Start live trading",
          }
        : {
            title: "Start trading on the paper account?",
            messageHtml:
              `Orders will be simulated — no real money. They go to <code>${target}</code>, ` +
              "and every configuration panel locks until you turn trading off.",
            confirmText: "Start paper trading",
          }
    );
    if (!ok) return;
  }
  const stopping = !!tr.on;
  let r = null;
  try {
    r = await api(stopping ? "/api/v1/trading/off" : "/api/v1/trading/on", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // The acknowledgement only means anything when STARTING on a live account;
      // sending one alongside a stop would read as if stopping needed consent.
      body: JSON.stringify(stopping ? {} : { confirm_live: !!exec.live }),
    });
  } catch (err) {
    flashToast(`Trading switch failed: ${err.message}`, "warn");
    return;
  }
  if (r && r.ok === false) {
    flashToast(r.message || "Trading could not be turned on", "warn");
  } else {
    flashToast(r && r.message ? r.message : "", "ok");
  }
  await loadTrading();
  // A lock change alters what is available, so let the panels that own specific
  // buttons recompute them (they re-enable only what is genuinely possible).
  if (state.rulesPayload && typeof renderStrategyBar === "function") renderStrategyBar();
}

// The two header controls. They are ONE pill: identical border, tint, background,
// font and geometry, in every state — only the words (and the status dot that ends
// them) differ. The label always names the ACTION; nothing is styled per state.
function renderTradingControls(d) {
  const exec = d.execution || {};
  const tr = d.trading || {};
  const ver = d.verification || {};
  const env = String(exec.env || "").toUpperCase();
  const btn = $("trading-toggle");
  if (btn) {
    btn.className = "exec-pill exec-toggle"; // constant, like the account pill
    btn.title = tr.on
      ? `Trading is ON (${env}) for ${d.strategy || "this strategy"} — click to stop`
      : !exec.ok
        ? `Trading cannot start — ${exec.message}`
        : !ver.verified
          // Configured keys are not verified keys: say so before the click, not
          // only after the refusal — and when a check has already FAILED, quote it,
          // because "not verified yet" would send the operator to press Validate
          // when what they actually need is to fix the keys.
          ? `Trading cannot start — ${ver.has_verdict && ver.message ? ver.message : `the ${env} credentials have not been verified yet (Account Settings → Validate)`}`
          : `Start sending orders for ${d.strategy || "the active strategy"}`;
    btn.disabled = false; // the off switch must always be reachable
  }
  renderStatusDots(d); // this owns the label text, dot included
}

function renderTradingPanel(d) {
  const panel = $("trading-panel");
  if (!panel) return;
  const tr = d.trading || {};
  const exec = d.execution || {};
  panel.hidden = !tr.on;
  if (!tr.on) return;
  const msg = $("trading-msg");
  if (msg) {
    msg.textContent = `${String(tr.env || "").toUpperCase()} account · ${exec.broker || "—"} · live since ${shortWhen(tr.since)}`;
  }
  const facts = $("trading-facts");
  if (facts) {
    facts.innerHTML = [
      execRow("Strategy", escapeHtml(d.strategy || "—")),
      execRow("Instrument", escapeHtml(d.instrument || "—")),
      execRow("Bar size", escapeHtml(d.bar_size || "—")),
      execRow("Endpoint", `<code>${escapeHtml(exec.base_url || "—")}</code>`),
    ].join("");
  }
}

function execRow(label, html) {
  return `<div class="rp-kv"><span class="label">${escapeHtml(label)}</span><span>${html}</span></div>`;
}

// "2026-09-15T09:12:31+00:00" -> "09:12 UTC on 2026-09-15"
function shortWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const hhmm = d.toISOString().slice(11, 16);
  return `${hhmm} UTC on ${d.toISOString().slice(0, 10)}`;
}

// Mirror of the server's lock: disable everything that would be refused with 409.
function applyConfigLock() {
  const locked = !!state.tradingLocked;
  document.body.classList.toggle("trading-on", locked);
  setBtBusyControls(!!state.btRunning); // spreads the lock through the shared list
  applyStrategyAddState(!!state.btRunning); // ＋ New: busy OR limit OR lock
  // The backtest card derives its own button states (Run is also data-gated), so
  // ask it to recompute — otherwise unlocking would leave Run disabled until the
  // next poll happened to re-render it.
  if (document.getElementById("backtest")) renderBacktest(btPayload, btDelta);
  const report = $("bt-report");
  if (report) report.disabled = locked;
  const deltaHost = $("delta-actions"); // "⬇ Fetch bars" / "↻ Retry" write to the dataset
  if (deltaHost) deltaHost.querySelectorAll("button").forEach((b) => { b.disabled = locked; });
  // The strategy bar's own buttons are owned by renderStrategyBar(); ask it to
  // recompute so unlocking re-enables exactly what is available again.
  if (state.rulesPayload && typeof renderStrategyBar === "function") renderStrategyBar();
}

async function saveAccount() {
  const btn = $("account-save");
  const c = state.account;
  if (!c) return;

  // Read every field from the rendered form (checkbox switches submit True/False;
  // an empty password field keeps the stored secret).
  const values = {};
  for (const group of c.groups || []) {
    for (const f of group.fields) {
      const el = document.getElementById("acct-" + f.key);
      if (!el) continue;
      values[f.key] = el.type === "checkbox" ? (el.checked ? "True" : "False") : el.value;
    }
  }

  if (btn) btn.disabled = true;
  showAccountErrors("");
  try {
    const r = await api("/api/v1/account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    if (r && r.ok === false) {
      showAccountErrors((r.errors || []).join("\n") || r.message || "Invalid values", "warn");
      return;
    }
    $("account-msg").textContent = r.message || "Saved";
    // The save itself checks any pair that had never been verified, so the first
    // report of a bad credential arrives right here.
    const checks = Object.entries(r.verifications || {});
    const bad = checks.filter(([, c]) => c.checked && !c.ok);
    const good = checks.filter(([, c]) => c.checked && c.ok);
    await loadAccount(true); // re-read so secrets re-mask and values refresh
    await loadTrading(); // the keys may have just changed, so re-resolve the target
    if (bad.length) {
      showAccountErrors(
        bad.map(([env, c]) => `${env.toUpperCase()} credentials are NOT valid: ${c.message}`).join("\n"),
        "warn"
      );
    } else if (good.length) {
      flashToast(`Verified ${good.map(([env]) => env.toUpperCase()).join(" and ")} credentials`, "ok");
    }
  } catch (err) {
    showAccountErrors(err.message, "warn");
  } finally {
    if (btn) btn.disabled = false;
  }
}

(function initAccountSettingsModal() {
  const backdrop = $("account-settings-backdrop");
  if (!backdrop) return;
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeAccountSettings();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !backdrop.hidden) closeAccountSettings();
  });
})();

/* ---------- Chart indicators (Option B viewer) ---------- */
async function loadIndicators() {
  const toggles = $("indicator-toggles");
  if (toggles) toggles.innerHTML = "";
  try {
    const bundle = await api("/api/v1/chart/indicators");
    state.indicators = bundle;
    renderIndicatorChips();
    applyIndicators();
  } catch (err) {
    if (toggles) toggles.innerHTML = `<span class="muted">Indicators unavailable: ${escapeHtml(err.message)}</span>`;
  }
}

function renderIndicatorChips() {
  const host = $("indicator-toggles");
  if (!host) return;
  const overlays = state.indicators ? state.indicators.overlays : [];
  if (!overlays.length) {
    host.innerHTML = `<span class="muted">No indicators enabled for this strategy — turn them on in the Strategy Configuration panel.</span>`;
    return;
  }
  state.selected = state.selected || {};
  host.innerHTML = "";
  overlays.forEach((o) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip-ind" + (state.selected[o.key] ? " active" : "");
    b.innerHTML =
      `<span class="dot" style="background:${o.color || "#4c8dff"}"></span>${o.label}`;
    b.onclick = () => {
      state.selected[o.key] = !state.selected[o.key];
      const turningOn = !!state.selected[o.key];
      b.classList.toggle("active", turningOn);
      applyIndicators();
      // Oscillator overlays render as panes below the chart, which can sit
      // below the fold — bring a freshly-enabled pane into view so the
      // toggle visibly does something.
      if (turningOn && o.scale === "osc") {
        const panes = document.querySelectorAll("#indicator-panes .osc-pane");
        const pane = Array.from(panes).find(
          (p) => p.querySelector(".osc-title")?.textContent === o.label
        );
        if (pane) pane.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    };
    host.appendChild(b);
  });
}

function applyIndicators() {
  // Toggle overlays/panes IN PLACE instead of rebuilding the whole chart, so
  // the user's zoom and scroll position are preserved across toggles.
  if (state.chart && state.datasetRows) {
    (state.overlaySeries || []).forEach((s) => {
      try { state.chart.removeSeries(s); } catch (_) { /* noop */ }
    });
    state.overlaySeries = [];
    const chart = state.chart;
    addPriceOverlays(chart);
    drawVolumeBars(chart); // draws bars or resets the bottom band
  } else if (state.chart || state.datasetRows) {
    buildMainChart(); // no live chart yet -> build it
  }
  drawOscPanes();
}

function addPriceOverlays(chart) {
  if (!state.indicators) return;
  state.indicators.overlays
    .filter((o) => o.scale === "price" && state.selected[o.key])
    .forEach((o) => {
      o.lines.forEach((line) => {
        const color =
          o.kind === "bands"
            ? (line.name === "upper" ? "#ef5350" : line.name === "lower" ? "#26a69a" : o.color)
            : o.color;
        const s = chart.addLineSeries({
          color, lineWidth: 1, priceLineVisible: false, lastValueVisible: true,
        });
        s.setData(line.data);
        state.overlaySeries.push(s);
      });
    });
}

/* Keep every oscillator pane's time scale in lock-step with the main price
   chart: zooming/panning the chart zooms/scrolls the indicator panes by the
   same amount of time, no matter the window (2 years, 3 months, days…).

   We sync by TIME range (not logical/index range): panes hold fewer bars than
   the main chart (RSI/ATR/momentum drop their warm-up rows), so a logical
   range would be clamped on the pane and the clamped value pushed back into
   the main chart — which moved the user's zoom when a pane was toggled on.
   Dates map 1:1 across every chart, so time-based sync is always exact. */
function _syncTimeRange(chart, range) {
  if (!chart || !range) return;
  const cur = chart.timeScale().getVisibleRange();
  if (cur && cur.from === range.from && cur.to === range.to) {
    return; // already in sync — breaks the echo loop between charts
  }
  chart.timeScale().setVisibleRange(range);
}

// Every chart that must share the main price chart's zoom: the oscillator
// panes AND the backtest equity-curve sparkline (when a result is shown).
// Read lazily each time so a just-created/removed satellite is picked up
// without needing to (re)subscribe the main chart.
function _satelliteCharts() {
  const list = (state.oscCharts || []).slice();
  if (btChart) list.push(btChart);
  return list;
}

function _mainRangeHandler(range) {
  if (!range) return;
  _satelliteCharts().forEach((oc) => _syncTimeRange(oc, range));
}

function subscribeMainToOscTime() {
  // The main chart drives the panes. Idempotent per chart instance.
  if (!state.chart) return;
  const ts = state.chart.timeScale();
  ts.unsubscribeVisibleTimeRangeChange(_mainRangeHandler);
  ts.subscribeVisibleTimeRangeChange(_mainRangeHandler);
}

function subscribeOscToMainTime(chart) {
  // Panning/zooming inside a pane also moves the main chart (two-way sync).
  chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
    if (!range || !state.chart || chart === state.chart) return;
    _syncTimeRange(state.chart, range);
  });
}

/* ---------- Crosshair sync (vertical time line + horizontal value) ----------
   Hovering ANY chart places every other chart's crosshair on the same TIME, so
   one instant can be read across the candles and all the indicator panes.
   lightweight-charts only draws a crosshair on the chart under the pointer, so
   the others are positioned programmatically via `setCrosshairPosition`.

   The horizontal line needs a VALUE and every chart has its own scale, so each
   synced line is anchored on that chart's OWN series value at that time — i.e.
   exactly what it shows when you hover that chart yourself. A warm-up gap in an
   anchor falls back to its first real value, so the VERTICAL line never breaks. */
const _crosshairAnchors = new Map(); // chart -> {series, values: Map, times, fallback}
let _crosshairSyncing = false;

function registerCrosshairAnchor(chart, series, values, fallback) {
  if (!chart || !series) return;
  // `values` is a time -> value Map populated in chronological order, so its
  // keys ARE the ascending time axis the nearest-previous lookup binary-searches.
  // Without them a chart with sparse data (the 600-point equity curve) could
  // only ever show its first value on the horizontal line.
  const times = values ? Array.from(values.keys()) : [];
  _crosshairAnchors.set(chart, {
    series: series, values: values, times: times, fallback: fallback,
  });
  chart.subscribeCrosshairMove((param) => _onCrosshairMove(chart, param));
}

function forgetCrosshairAnchor(chart) {
  if (chart) _crosshairAnchors.delete(chart);
}

// [{time, value}] (null values already dropped) -> {values, times, fallback}
function _crosshairValues(data) {
  const values = new Map();
  const times = [];
  let fallback = null;
  (data || []).forEach((p) => {
    if (p == null || p.value == null) return;
    if (!values.has(p.time)) times.push(p.time);
    values.set(p.time, p.value);
    if (fallback == null) fallback = p.value;
  });
  return { values: values, times: times, fallback: fallback };
}

// The anchor's value at `time`, falling back to the NEAREST point at or before
// it. That matters for charts whose data is thinner than the price series: the
// equity curve is capped at 600 points, so an exact hit is the exception — an
// exact-only lookup would leave its horizontal line parked on the first value.
function _crosshairValueAt(anchor, time) {
  const exact = anchor.values.get(time);
  if (exact != null) return exact;
  const times = anchor.times || [];
  let lo = 0;
  let hi = times.length - 1;
  let found = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= time) { found = times[mid]; lo = mid + 1; } else { hi = mid - 1; }
  }
  return found == null ? anchor.fallback : anchor.values.get(found);
}

function _onCrosshairMove(source, param) {
  if (!source || !_crosshairAnchors.size) return;
  // Re-entrancy guard. VERIFIED against 4.1.3: `setCrosshairPosition` emits no
  // crosshair event at all, so a programmatic placement cannot bounce back —
  // this only needs to cover a synchronous echo if a future version adds one,
  // which is why it expires on the next task. Do NOT swallow by pushed TIME:
  // a stationary mouse repeats the same bar time on every move, and dropping
  // those repeats freezes the crosshair on whichever chart was hovered first.
  if (_crosshairSyncing) return;

  const time = param && param.time != null ? param.time : null;
  _crosshairSyncing = true;
  try {
    _crosshairAnchors.forEach((anchor, chart) => {
      if (chart === source) return; // the hovered chart already tracks the mouse
      try {
        if (time == null) {
          chart.clearCrosshairPosition();
        } else {
          chart.setCrosshairPosition(_crosshairValueAt(anchor, time), time, anchor.series);
        }
      } catch (_) {
        /* series or point vanished in a rebuild — skip this chart */
      }
    });
  } finally {
    setTimeout(() => { _crosshairSyncing = false; }, 0);
  }
}

// Bumped on every drawOscPanes() so deferred pane-finalize callbacks from an
// older invocation never act on panes that have since been removed.
let _oscPaneGen = 0;

function drawOscPanes() {
  const host = $("indicator-panes");
  if (!host) return;
  (state.oscCharts || []).forEach((c) => {
    forgetCrosshairAnchor(c); // a removed chart must not be crosshair-synced
    try { c.remove(); } catch (_) { /* noop */ }
  });
  state.oscCharts = [];
  host.innerHTML = "";
  if (!state.indicators) return;
  state.indicators.overlays
    .filter((o) => o.scale === "osc" && o.kind !== "histogram" && state.selected[o.key])
    .forEach((o) => {
      const pane = document.createElement("div");
      pane.className = "osc-pane";
      const title = document.createElement("div");
      title.className = "osc-title";
      title.textContent = o.label;
      const canvas = document.createElement("div");
      canvas.className = "osc-canvas";
      pane.appendChild(title);
      pane.appendChild(canvas);
      host.appendChild(pane);

      const chart = LightweightCharts.createChart(canvas, {
        height: 110,
        layout: { background: { color: "transparent" }, textColor: "#8a93a6" },
        grid: { vertLines: { color: "#22262f" }, horzLines: { color: "#22262f" } },
        timeScale: { timeVisible: false, borderColor: "#333a46" },
        rightPriceScale: { borderColor: "#333a46" },
        // Same free-floating crosshair as the main chart, so the synced
        // horizontal line is not snapped to a bar's extremes.
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      });
      let anchorSeries = null;
      o.lines.forEach((line) => {
        // A line may carry its own kind / colour / price format: MACD draws its
        // histogram (bars) AND its two lines in ONE pane, so these cannot all
        // be taken from the overlay.
        const kind = line.kind || o.kind;
        const lineColor = line.color || o.color || "#4c8dff";
        const s =
          kind === "histogram"
            ? chart.addHistogramSeries({
                color: lineColor,
                priceFormat: line.priceFormat || { type: "volume" },
                priceLineVisible: false,
                lastValueVisible: true,
              })
            : chart.addLineSeries({ color: lineColor, lineWidth: 1 });
        s.setData(line.data);
        if (!anchorSeries) anchorSeries = s;
      });
      if (o.range) chart.priceScale("right").applyOptions({ minValue: o.range.min, maxValue: o.range.max });
      // Anchor the synced horizontal line on the pane's FIRST series (the MACD
      // histogram, the RSI line, …): that is the value this pane reads out.
      const anchor = _crosshairValues(o.lines[0] && o.lines[0].data);
      registerCrosshairAnchor(chart, anchorSeries, anchor.values, anchor.fallback);
      state.oscCharts.push(chart);
    });

  // Let freshly created panes paint once at their natural width BEFORE we (a)
  // snap them to the main chart's zoom and (b) attach the reverse sync. If we
  // do either synchronously, a pane emits its own initial right-aligned
  // default view during first layout — and because the sync is two-way, that
  // wrong range gets pushed back into the MAIN chart, resetting the user's
  // zoom every time an oscillator is toggled on. Waiting two frames lets that
  // initial emission pass harmlessly (no handler attached yet); only then do
  // we enable pane->main sync and pin each pane to the chart's current view.
  // The generation token makes stale callbacks from an earlier toggle a no-op.
  const gen = ++_oscPaneGen;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (gen !== _oscPaneGen) return; // a newer drawOscPanes superseded us
      if (!state.chart) return;
      const mainRange = state.chart.timeScale().getVisibleRange();
      (state.oscCharts || []).forEach((oc) => {
        subscribeOscToMainTime(oc);
        if (mainRange) _syncTimeRange(oc, mainRange);
      });
    });
  });
}

/* ---------- Collapsible cards (Historical Data) ---------- */
function toggleCollapse(head) {
  const card = head.closest(".collapsible");
  if (!card) return;
  const body = card.querySelector(".collapse-body");
  const chev = head.querySelector(".card-toggle");
  body.hidden = !body.hidden;
  if (chev) chev.textContent = body.hidden ? "+" : "−";
}

/* ---------- Historical Delta panel ---------- */
async function loadDelta() {
  const body = $("delta-body");
  if (!body) return;
  try {
    const s = await api("/api/v1/delta/status");
    renderDelta(s);
  } catch (err) {
    body.innerHTML = `<p class="muted">Delta check failed: ${escapeHtml(err.message)}</p>`;
  }
}

// The panel's action button lives in the card header (top-right), like the
// other panels; it is state-specific, so it is cleared before each render.
function setDeltaAction(html) {
  const host = $("delta-actions");
  if (!host) return;
  host.innerHTML = html || "";
  // While trading is on the server refuses data writes with 409, so don't offer them.
  if (state.tradingLocked) host.querySelectorAll("button").forEach((b) => { b.disabled = true; });
}

function renderDelta(s) {
  const body = $("delta-body");
  if (!body) return;
  setDeltaAction(""); // header action depends on the state below

  if (s.error) {
    setDeltaAction(`<button class="ghost small" onclick="loadDelta()">↻ Retry</button>`);
    body.innerHTML = `
      <div class="delta-head">
        <span class="delta-badge warn">⚠ Check failed</span>
        <p class="muted">${escapeHtml(s.error)}</p>
      </div>`;
    return;
  }
  if (s.exists === false) {
    body.innerHTML = `<p class="muted">No dataset yet — download the initial historical data first.</p>`;
    return;
  }

  if (s.synced) {
    const rows = (s.recent || [])
      .map(
        (r) =>
          `<tr><td>${r.date}</td><td>${r.open.toFixed(2)}</td><td>${r.high.toFixed(2)}</td>` +
          `<td>${r.low.toFixed(2)}</td><td>${r.close.toFixed(2)}</td><td>${r.volume.toLocaleString()}</td></tr>`
      )
      .join("");
    body.innerHTML = `
      <div class="delta-head">
        <span class="delta-badge ok">✓ All data synced</span>
        <span class="muted">through ${s.last_date} · ${s.rows.toLocaleString()} rows</span>
      </div>
      <h4 class="delta-title">Last 5 bars</h4>
      <div class="delta-table">
        <table>
          <thead><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="6" class="muted">—</td></tr>`}</tbody>
        </table>
      </div>`;
    return;
  }

  const chips = Array.from(new Set(s.missing || []))
    .map((d) => `<span class="chip date-chip">${d}</span>`)
    .join("");
  const missingCount = (s.missing || []).length;
  body.innerHTML = `
    <div class="delta-head">
      <span class="delta-badge warn">⚠ ${missingCount} ${barWord(missingCount)} missing</span>
      <p class="muted">Dataset is current through ${s.last_date}. Missing completed bars:</p>
      <div class="missing-chips">${chips}</div>
      <p id="delta-msg" class="muted"></p>
    </div>`;
  setDeltaAction(`<button id="sync-delta-btn" class="primary small" onclick="syncDelta()">⬇ Fetch bars</button>`);
}

async function syncDelta() {
  const btn = $("sync-delta-btn");
  const msg = $("delta-msg");
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = "Fetching bars…";
  try {
    const s = await api("/api/v1/delta/sync", { method: "POST" });
    renderDelta(s);
    // Dataset currency drives the Run-backtest gate — refresh it after a sync.
    btDelta = s;
    renderBacktest(btPayload, s);
    if (s.synced) refresh(); // dataset changed -> update summary/chart/table
  } catch (err) {
    if (msg) msg.textContent = `Failed: ${err.message}`;
    if (btn) btn.disabled = false;
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/* ---------- Strategy bar + Rules + per-strategy Configuration ---------- */
const RULES_VALUE_TARGET = "__value__";

function rulesStrategies() {
  return (state.rulesPayload && state.rulesPayload.strategies) || {};
}

function activeRuleset() {
  const s = rulesStrategies();
  return (state.activeName && s[state.activeName]) || null;
}

/* Show the number of rules set for the active strategy in the Rules panel
   title, e.g. "Rules (3)". Empty when there are no rules. */
function updateRulesCount() {
  const el = $("rules-count");
  if (!el) return;
  const rs = activeRuleset();
  const n = rs && Array.isArray(rs.rules) ? rs.rules.length : 0;
  el.textContent = n ? ` (${n})` : "";
}

function strategyConfigGroups() {
  return (state.rulesPayload && state.rulesPayload.config_groups) || [];
}

// The risk limits render in their own panel, not with the configuration fields.
function strategyRiskGroups() {
  return (state.rulesPayload && state.rulesPayload.risk_groups) || [];
}

function setMsg(text) {
  const m = $("rules-msg");
  if (m) m.textContent = text || "";
}

function setPMsg(text) {
  const m = $("pconfig-msg");
  if (m) m.textContent = text || "";
}

function setRiskMsg(text) {
  const m = $("risk-msg");
  if (m) m.textContent = text || "";
}

function setStrategyMsg(text) {
  const m = $("strategy-msg");
  if (m) m.textContent = text || "";
}

function setActionButtonsDisabled(disabled) {
  const off = !!disabled || !!state.tradingLocked; // the lock can never be undone here
  ["rules-add-buy", "rules-add-sell", "rules-reset", "save-rules", "save-pconfig", "save-risk"].forEach((id) => {
    const b = $(id);
    if (b) b.disabled = off;
  });
}

async function loadRules() {
  try {
    const d = await api("/api/v1/rules");
    state.rulesPayload = d;
    renderRules();
    renderPConfig();
    renderRiskPanel();
    renderStrategyBar();
    setMsg(
      d.error
        ? `⚠ ${d.error} — no file written.`
        : d.file_exists
          ? `Editing ${d.file}`
          : `New file will be created at ${d.file}.`
    );
    syncBtRules(); // rules payload is now loaded -> recompute the backtest gate
  } catch (err) {
    state.rulesPayload = null;
    setMsg(`Failed to load rules: ${err.message}`);
  }
}

/* Human-readable rendering of a strategy's rules (shown under the strategy
   name in the header bar). Each rule reads like a sentence instead of a
   formula, e.g. "Buy when the closing price is below the 50-period simple
   moving average and the 14-period RSI is below 30." */
const _RAW_FEATURE_PHRASES = {
  open: "the opening price",
  high: "the high price",
  low: "the low price",
  close: "the closing price",
  volume: "the trading volume",
};

const _INDICATOR_PHRASES = {
  sma: "simple moving average",
  ema: "exponential moving average",
  rsi: "RSI",
  atr: "average true range",
  bb_pctb: "Bollinger Band %B",
  mom: "momentum",
  vol: "volatility",
  vwap: "volume-weighted average price",
  vratio: "relative volume",
};

// MACD's three columns each carry a (fast, slow, signal) triple, so they are
// spelled out rather than derived from a single period.
const _MACD_PHRASES = {
  macd: "MACD line",
  macd_signal: "MACD signal line",
  macd_hist: "MACD histogram",
};

const _OP_PHRASES = {
  "<": "below",
  "<=": "at or below",
  ">": "above",
  ">=": "at or above",
  "==": "equal to",
  "!=": "not equal to",
  crosses_above: "crosses above",
  crosses_below: "crosses below",
};

function featurePhrase(name) {
  const raw = _RAW_FEATURE_PHRASES[name];
  if (raw) return raw;
  if (name === "volume_abs") return "the absolute trading volume";
  // MACD columns carry a (fast, slow, signal) triple, so they are matched
  // before the single-period pattern below.
  const macd = /^(macd|macd_signal|macd_hist)_(\d+)_(\d+)_(\d+)$/.exec(name);
  if (macd) return `the ${_MACD_PHRASES[macd[1]]} (${macd[2]}, ${macd[3]}, ${macd[4]})`;
  const m = /^([a-z_]+?)_(\d+)$/.exec(name);
  const label = _INDICATOR_PHRASES[m ? m[1] : name];
  if (m && label) return `the ${m[2]}-period ${label}`;
  if (label) return `the ${String(label).replace(/_/g, " ")}`;
  return `the ${String(name).replace(/_/g, " ")} value`;
}

function conditionSentence(c) {
  const opWord = _OP_PHRASES[c.op] || c.op || "related to";
  const target = c.ref
    ? featurePhrase(c.ref)
    : (c.value != null ? String(c.value) : "a fixed value");
  if (c.op === "crosses_above" || c.op === "crosses_below") {
    return `${featurePhrase(c.feature)} ${opWord} ${target}`;
  }
  return `${featurePhrase(c.feature)} is ${opWord} ${target}`;
}

function ruleToText(r) {
  const conds = (r.conditions || []).map(conditionSentence);
  if (!conds.length) return null;
  const joiner = r.mode === "any" ? " or " : " and ";
  const side = String(r.side || "BUY");
  const verdict = side.charAt(0) + side.slice(1).toLowerCase();
  const off = r.enabled === false ? " (disabled)" : "";
  return `${verdict} when ${conds.join(joiner)}${off}`;
}

// A rule “references a disabled feature” when any series it uses (feature or
// ref) is not in the active config's allowed list (e.g. sma_50 while
// FEATURE_SMA_ENABLED=False). Such a rule never fires.
function ruleUsesDisabledFeature(rule, allowed) {
  if (!rule || !rule.conditions || !allowed || !allowed.length) return false;
  const ok = (name) => allowed.indexOf(name) !== -1;
  return rule.conditions.some((c) => !ok(c.feature) || (c.ref && !ok(c.ref)));
}

/* ---------- Strategy header bar ---------- */
function renderStrategyBar() {
  const p = state.rulesPayload;
  const names = Object.keys(p ? p.strategies || {} : {});
  const sel = $("strategy-select");
  const delBtn = $("strategy-delete");
  const renameBtn = $("strategy-rename");
  if (sel) {
    const opts = names.length
      ? names.map((n) => _optHtml(n, n, p.active)).join("")
      : `<option value="">— none —</option>`;
    sel.innerHTML = opts;
    sel.value = p.active || "";
  }
  if (delBtn) delBtn.disabled = !p || !p.active || !!state.tradingLocked;
  if (renameBtn) renameBtn.disabled = !p || !p.active || !!state.tradingLocked;
  applyStrategyAddState(!!state.tradingLocked); // busy OR limit OR trading lock

  const rs = p && p.strategies ? p.strategies[p.active] : null;
  const chip = $("strategy-context");
  if (chip) {
    if (rs) {
      const cfg = rs.config || {};
      const instr = rs.instrument || cfg.INSTRUMENT || "";
      const bar = cfg.HISTORICAL_BAR_SIZE || "";
      chip.textContent = [instr, bar].filter(Boolean).join(" · ") || "";
    } else {
      chip.textContent = "";
    }
  }

  // Rules as plain text under the strategy name: BUY rules grouped first,
  // then SELL rules, each on its own line. Rules referencing a DISABLED
  // feature are tinted to show they won't fire.
  const rulesEl = $("strategy-rules");
  if (rulesEl) {
    const allowed = (p && p.allowed_features) || [];
    const sideOrder = { BUY: 0, SELL: 1 };
    const lines = (rs && rs.rules ? rs.rules : [])
      .slice()
      .sort((a, b) => {
        const oa = sideOrder[a.side] != null ? sideOrder[a.side] : 9;
        const ob = sideOrder[b.side] != null ? sideOrder[b.side] : 9;
        return oa - ob;
      })
      .filter((r) => (r.conditions || []).length);
    rulesEl.innerHTML = "";
    lines.forEach((r) => {
      const off = ruleUsesDisabledFeature(r, allowed);
      const line = document.createElement("div");
      line.className = "strategy-rule-line" + (off ? " feature-off" : "");
      line.appendChild(document.createTextNode(ruleToText(r) || ""));
      if (off) {
        const tag = document.createElement("span");
        tag.className = "rule-off-tag";
        tag.textContent = " (feature disabled — won't fire)";
        line.appendChild(tag);
      }
      rulesEl.appendChild(line);
    });
    rulesEl.hidden = !lines.length;
  }
}

async function onStrategySelect() {
  const sel = $("strategy-select");
  if (!sel || !sel.value) return;
  if (sel.value === (state.rulesPayload && state.rulesPayload.active)) return;
  setStrategyMsg("Switching strategy…");
  try {
    const r = await api("/api/v1/rules/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: sel.value }),
    });
    if (r.ok) {
      // Full reload: every panel re-renders in the new strategy's context.
      window.location.reload();
    } else {
      setStrategyMsg(r.message || "Could not switch strategy.");
      renderStrategyBar();
    }
  } catch (err) {
    setStrategyMsg(`Switch failed: ${err.message}`);
    renderStrategyBar();
  }
}

function toggleTopNew() {
  const addBtn = $("strategy-add");
  if (addBtn && addBtn.disabled) return; // limit reached (or a backtest is running)
  const row = $("strategy-new-row");
  const input = $("strategy-new-name");
  if (!row) return;
  cancelTopRename(); // only one inline editor at a time
  row.hidden = !row.hidden;
  if (!row.hidden) { if (input) input.focus(); }
}

function cancelTopNew() {
  const row = $("strategy-new-row");
  const input = $("strategy-new-name");
  if (row) row.hidden = true;
  if (input) input.value = "";
}

/* ---------- Strategy rename (inline in the header bar) ---------- */
function toggleTopRename() {
  const name = (state.activeName || "").trim();
  if (!name) { setStrategyMsg("Select a strategy to rename first."); return; }
  cancelTopNew(); // only one inline editor at a time
  const row = $("strategy-rename-row");
  const input = $("strategy-rename-name");
  if (!row) return;
  row.hidden = !row.hidden;
  if (!row.hidden && input) {
    input.value = name;
    input.focus();
    input.select();
  }
}

function cancelTopRename() {
  const row = $("strategy-rename-row");
  const input = $("strategy-rename-name");
  if (row) row.hidden = true;
  if (input) input.value = "";
}

async function renameTopStrategy() {
  const name = (state.activeName || "").trim();
  const input = $("strategy-rename-name");
  const newName = (input ? input.value : "").trim();
  if (!name) { setStrategyMsg("No strategy selected."); return; }
  if (!newName) { setStrategyMsg("Enter a new name for the strategy."); if (input) input.focus(); return; }
  if (newName === name) { setStrategyMsg("The new name is the same as the current name."); if (input) input.focus(); return; }
  setStrategyMsg("Renaming strategy…");
  try {
    const r = await api("/api/v1/rules/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, new_name: newName }),
    });
    if (r.ok) {
      // Full reload: the strategy (and its context) now live under the new name.
      window.location.reload();
    } else {
      setStrategyMsg(r.message || "Could not rename strategy.");
      if (input) input.focus();
    }
  } catch (err) {
    setStrategyMsg(`Rename failed: ${err.message}`);
  }
}

async function createTopStrategy() {
  const input = $("strategy-new-name");
  const name = (input ? input.value : "").trim();
  if (!name) { setStrategyMsg("Enter a strategy name first."); return; }
  setStrategyMsg("Creating strategy…");
  try {
    const r = await api("/api/v1/rules/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (r.ok) {
      window.location.reload();
    } else {
      setStrategyMsg(r.message || "Could not create strategy.");
    }
  } catch (err) {
    setStrategyMsg(`Create failed: ${err.message}`);
  }
}

function deleteActiveStrategy() {
  openDeleteStrategy(); // confirmation modal; reloads on success
}

/* ---------- Rules panel (active strategy's rules) ---------- */
function toggleRules(ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const body = $("rules-body");
  const btn = $("toggle-rules");
  if (!body) return;
  const collapsed = !body.hidden;
  body.hidden = collapsed;
  if (btn) {
    btn.textContent = collapsed ? "+" : "−";
    btn.title = collapsed ? "Expand rules" : "Collapse rules";
  }
}

function renderRules() {
  const p = state.rulesPayload;
  const host = $("rules-list");
  const names = Object.keys(p ? p.strategies || {} : {});
  if (!host) return;

  if (!p) {
    host.innerHTML = `<span class="muted">No rules loaded.</span>`;
    setActionButtonsDisabled(true);
    return;
  }
  if (!names.length) {
    host.innerHTML = `<span class="muted">No strategies yet — create one with “＋ New” in the Strategy bar above.</span>`;
    setActionButtonsDisabled(true);
    return;
  }

  setActionButtonsDisabled(false);
  if (!p.strategies[state.activeName]) {
    state.activeName = p.active && p.strategies[p.active] ? p.active : names[0];
  }
  updateRulesCount();
  const rs = p.strategies[state.activeName];
  if (!rs || !rs.rules || !rs.rules.length) {
    host.innerHTML = `<span class="muted">No rules in “${escapeHtml(state.activeName)}” yet — click “+ Buy” or “+ Sell”.</span>`;
    return;
  }
  host.innerHTML = rs.rules.map((r, i) => ruleCardHtml(r, i, p)).join("");
}

/* ---------- Configuration panel (per-strategy) ---------- */
function toggleConfigPanel(ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const body = $("pconfig-body");
  const btn = $("toggle-pconfig");
  if (!body) return;
  const collapsed = !body.hidden;
  body.hidden = collapsed;
  if (btn) {
    btn.textContent = collapsed ? "+" : "−";
    btn.title = collapsed ? "Expand configuration" : "Collapse configuration";
  }
}

function renderPConfig() {
  renderConfigGroups($("pconfig-groups"), strategyConfigGroups(), "scfg");
}

// Risk Management renders its own group(s) in a separate, collapsed panel.
function renderRiskPanel() {
  renderConfigGroups($("risk-groups"), strategyRiskGroups(), "rcfg");
}

/* Render groups as <fieldset>s into a host. A stored per-strategy value wins
   over the global default. ``prefix`` namespaces the input ids so the same key
   can never be rendered in two panels (which would make saving ambiguous). */
function renderConfigGroups(host, groups, prefix) {
  if (!host) return;
  const rs = activeRuleset();
  host.innerHTML = "";
  if (!rs) {
    host.innerHTML = `<p class="muted">No active strategy — create one with “＋ New” in the Strategy bar above.</p>`;
    return;
  }
  const stored = rs.config || {};
  groups.forEach((group) => {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "cfg-section";
    const legend = document.createElement("legend");
    legend.textContent = group.name;
    fieldset.appendChild(legend);
    group.fields.forEach((f) => {
      const value = stored[f.key] != null ? stored[f.key] : f.default_value;
      fieldset.appendChild(fieldInput(Object.assign({}, f, { value }), prefix));
    });
    host.appendChild(fieldset);
  });
}

/* ---------- Risk Management panel ---------- */
function toggleRiskPanel(ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const body = $("risk-body");
  const btn = $("toggle-risk");
  if (!body) return;
  const collapsed = !body.hidden;
  body.hidden = collapsed;
  if (btn) {
    btn.textContent = collapsed ? "+" : "−";
    btn.title = collapsed ? "Expand risk management" : "Collapse risk management";
  }
}

function saveRiskConfig() {
  saveStrategyFull("risk");
}

function syncStrategyConfig() {
  const rs = activeRuleset();
  if (!rs) return;
  rs.config = rs.config || {};
  // Both panels write into the SAME per-strategy config object.
  [["scfg", strategyConfigGroups()], ["rcfg", strategyRiskGroups()]].forEach(([prefix, groups]) => {
    groups.forEach((group) => {
      group.fields.forEach((f) => {
        const el = document.getElementById(prefix + "-" + f.key);
        if (!el) return;
        rs.config[f.key] = el.type === "checkbox" ? (el.checked ? "True" : "False") : el.value;
      });
    });
  });
}

function _optHtml(value, label, current) {
  const sel = value === current ? " selected" : "";
  return `<option value="${escapeHtml(value)}"${sel}>${escapeHtml(label)}</option>`;
}

// A condition compares its `feature` against a `ref` (another series) or a
// plain number ("value…"). A series can never be meaningfully compared with
// itself (the backend rejects feature == ref), so the ref picker only offers
// the OTHER series — no pointless "close < close" style comparisons.
function comparableRefs(feature, feats) {
  return feats.filter((f) => f !== feature);
}

// A stored series that the active config no longer produces (e.g. sma_50 while
// FEATURE_SMA_ENABLED=False) gets a "(disabled)" marker in the dropdown.
function optionLabel(f, feats) {
  return feats.indexOf(f) !== -1 ? f : `${f} (disabled)`;
}

function targetOptionsHtml(feature, feats, current, extra) {
  extra = extra || [];
  const refs = comparableRefs(feature, feats.concat(extra));
  // A stored/inline ref that equals the feature (or is no longer produced by
  // the active feature config) is invalid — fall back to the numeric target.
  const cur = current !== RULES_VALUE_TARGET && refs.indexOf(current) !== -1
    ? current
    : RULES_VALUE_TARGET;
  let html = _optHtml(RULES_VALUE_TARGET, "value…", cur);
  refs.forEach((f) => { html += _optHtml(f, `ref ${optionLabel(f, feats)}`, cur); });
  return { html, cur };
}

function conditionRowHtml(c, j, payload) {
  const feats = payload.allowed_features || [];
  // Keep any stored series that is currently DISABLED as an option, so the
  // row still shows it and Save preserves it — never silently rewrites it to
  // a different (enabled) feature. Such rules are flagged & never evaluated.
  const extra = [];
  [c.feature, c.ref].forEach((n) => {
    if (n && feats.indexOf(n) === -1 && extra.indexOf(n) === -1) extra.push(n);
  });
  const feature = c.feature || feats[0];
  const t = targetOptionsHtml(feature, feats, c.ref || RULES_VALUE_TARGET, extra);
  const numVal = c.value == null ? "" : String(c.value);
  const numDisabled = t.cur === RULES_VALUE_TARGET ? "" : " disabled";
  const featOpts = feats.concat(extra).map((f) => _optHtml(f, optionLabel(f, feats), feature)).join("");
  const opOpts = (payload.ops || []).map((o) => _optHtml(o, o, c.op)).join("");
  return `<div class="cond-row" data-j="${j}">
    <select class="c-feature">${featOpts}</select>
    <select class="c-op">${opOpts}</select>
    <select class="c-target">${t.html}</select>
    <input class="c-num" type="number" step="any" placeholder="threshold" value="${escapeHtml(numVal)}"${numDisabled}>
    <button type="button" class="ghost small btn-x" data-act="remove-cond" title="Remove condition">✕</button>
  </div>`;
}

function ruleCardHtml(r, i, payload) {
  const feats = payload.allowed_features || [];
  const offFeat = ruleUsesDisabledFeature(r, feats);
  const conds = r.conditions && r.conditions.length
    ? r.conditions
    : [{ feature: feats[0] || "close", op: "<", value: null, ref: feats[1] || null }];
  const rows = conds.map((c, j) => conditionRowHtml(c, j, payload)).join("");
  const sideSel = ["BUY", "SELL"].map((s) => _optHtml(s, s, r.side)).join("");
  const modeSel = ["all", "any"].map((m) => _optHtml(m, m, r.mode || "all")).join("");
  const conf = r.confidence == null ? 0.75 : r.confidence;
  return `<div class="rule-card${offFeat ? " feature-off" : ""}" data-index="${i}" data-side="${r.side || "BUY"}">
    <div class="rule-head">
      <span class="rule-title">
        <select class="r-side">${sideSel}</select>
        <span class="rule-mode">if <select class="r-mode">${modeSel}</select> condition(s) hold</span>
        ${offFeat ? `<span class="rule-off-tag" title="This rule references a feature that is disabled in the active config — it never fires">⚠ disabled feature</span>` : ""}
      </span>
      <span class="rule-controls">
        <label><input type="checkbox" class="r-enabled"${r.enabled === false ? "" : " checked"}> on</label>
        <label>conf <input type="number" class="r-conf" min="0" max="1" step="0.05" value="${conf}"></label>
        <button type="button" class="ghost small btn-x" data-act="remove-rule" title="Delete rule">🗑</button>
      </span>
    </div>
    <div class="cond-list">${rows}</div>
    <button type="button" class="ghost small cond-add" data-act="add-cond">+ condition</button>
  </div>`;
}

function syncRuleFromDom(card) {
  const rs = activeRuleset();
  if (!rs) return;
  const idx = Number(card.dataset.index);
  const rules = rs.rules;
  if (!rules || !rules[idx]) return;
  const side = card.querySelector(".r-side").value;
  const mode = card.querySelector(".r-mode").value;
  const enabled = card.querySelector(".r-enabled").checked;
  const conf = parseFloat(card.querySelector(".r-conf").value);
  const conditions = [];
  card.querySelectorAll(".cond-row").forEach((row) => {
    const feature = row.querySelector(".c-feature").value;
    const op = row.querySelector(".c-op").value;
    const target = row.querySelector(".c-target").value;
    let value = null;
    let ref = null;
    if (target === RULES_VALUE_TARGET) {
      const n = parseFloat(row.querySelector(".c-num").value);
      value = Number.isFinite(n) ? n : 0.0;
    } else {
      ref = target;
    }
    conditions.push({ feature, op, value, ref });
  });
  rules[idx] = {
    side,
    mode,
    enabled,
    confidence: Number.isFinite(conf) ? conf : 0.75,
    conditions,
  };
  card.dataset.side = side;
}

function refreshRowTarget(row) {
  // Rebuild a row's target/ref options after its `feature` changed, so a
  // series is never offered as a comparison against itself.
  const feats = (state.rulesPayload && state.rulesPayload.allowed_features) || [];
  const feature = row.querySelector(".c-feature").value;
  const t = targetOptionsHtml(feature, feats, row.querySelector(".c-target").value);
  const sel = row.querySelector(".c-target");
  const num = row.querySelector(".c-num");
  sel.innerHTML = t.html;
  sel.value = t.cur;
  if (num) num.disabled = t.cur !== RULES_VALUE_TARGET;
}

function onRulesChange(e) {
  const card = e.target.closest(".rule-card");
  if (!card) return;
  const row = e.target.closest(".cond-row");
  const t = e.target;
  if (row && t) {
    if (t.classList.contains("c-feature")) {
      refreshRowTarget(row); // valid refs depend on the chosen feature
    } else if (t.classList.contains("c-target")) {
      const num = row.querySelector(".c-num");
      if (num) num.disabled = t.value !== RULES_VALUE_TARGET; // numeric only for "value…"
    }
  }
  syncRuleFromDom(card);
}

function onRulesClick(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const card = btn.closest(".rule-card");
  const rs = activeRuleset();
  if (!rs) return;
  const rules = rs.rules || (rs.rules = []);
  const act = btn.dataset.act;

  if (act === "remove-rule") {
    rules.splice(Number(card.dataset.index), 1);
    renderRules();
    syncBtRules();
  } else if (act === "remove-cond") {
    syncRuleFromDom(card);
    const row = btn.closest(".cond-row");
    const j = Number(row.dataset.j);
    const idx = Number(card.dataset.index);
    if (rules[idx] && rules[idx].conditions) rules[idx].conditions.splice(j, 1);
    renderRules();
    syncBtRules();
  } else if (act === "add-cond") {
    syncRuleFromDom(card);
    const feats = (state.rulesPayload.allowed_features || []);
    rules[Number(card.dataset.index)].conditions.push({
      feature: feats[0] || "close", op: "<", value: null, ref: feats[1] || null,
    });
    renderRules();
    syncBtRules();
  }
}

function addRule(side) {
  const rs = activeRuleset();
  if (!rs) return;
  const feats = state.rulesPayload.allowed_features || [];
  rs.rules = rs.rules || [];
  const cond = { feature: feats[0] || "close", op: "<", value: null, ref: feats[1] || null };
  if (!cond.ref) { cond.value = 0.0; }
  rs.rules.push({ side, mode: "all", enabled: true, confidence: 0.75, conditions: [cond] });
  renderRules();
  syncBtRules();
}

/* One Save path for both panels: sync rules + config, persist the strategy. */
async function saveStrategyFull(kind) {
  const rs = activeRuleset();
  const ui = {
    pconfig: { btn: "save-pconfig", msg: setPMsg },
    risk: { btn: "save-risk", msg: setRiskMsg },
    rules: { btn: "save-rules", msg: setMsg },
  }[kind] || { btn: "save-rules", msg: setMsg };
  const btn = $(ui.btn);
  const localMsg = ui.msg;
  if (!rs) {
    localMsg("No active strategy to save.");
    return;
  }

  // Capture the PRE-save context: instrument, bar size, historical period.
  const DEFAULT_YEARS = "2"; // matches Settings' historical_lookback_years default
  const cfg = rs.config || {};
  const oldInstrument = (cfg.INSTRUMENT || rs.instrument || "").trim().toUpperCase();
  const oldBar = (cfg.HISTORICAL_BAR_SIZE || "").trim();
  const oldYears = cfg.HISTORICAL_LOOKBACK_YEARS != null
    ? String(cfg.HISTORICAL_LOOKBACK_YEARS).trim()
    : DEFAULT_YEARS;
  const instrInput = document.getElementById("scfg-INSTRUMENT");
  const barInput = document.getElementById("scfg-HISTORICAL_BAR_SIZE");
  const yearsInput = document.getElementById("scfg-HISTORICAL_LOOKBACK_YEARS");
  const newInstrument = instrInput ? (instrInput.value || "").trim().toUpperCase() : oldInstrument;
  const newBar = barInput ? (barInput.value || "").trim() : oldBar;
  const newYears = yearsInput ? String(yearsInput.value || "").trim() : oldYears;

  const instrumentChanged = newInstrument !== oldInstrument;
  const barChanged = newBar !== oldBar;
  // A stored/absent years value is compared to the effective default ("2"), so
  // changing the select (even on a legacy strategy) is recognized as a change.
  const yearsChanged = newYears != null && newYears !== oldYears;
  const historyChanged = barChanged || yearsChanged;

  const revert = () => {
    if (instrInput) instrInput.value = oldInstrument;
    if (barInput) barInput.value = oldBar;
    if (yearsInput) yearsInput.value = oldYears;
  };

  // ── Instrument change → confirm + reload into the new symbol's context ──
  if (instrumentChanged) {
    const ok = await confirmDialog({
      title: "Switch this strategy's instrument?",
      messageHtml:
        `<p>You are changing the instrument from <b>${escapeHtml(oldInstrument || "—")}</b> to ` +
        `<b>${escapeHtml(newInstrument)}</b>.</p>` +
        `<p>After saving, the dashboard <b>reloads</b> into the new instrument's context. If no ` +
        `historical data exists for it yet, the “Download historical data” panel will appear.</p>` +
        `<p class="muted">The change is saved to this strategy's config. Your rules are kept.</p>`,
      confirmText: "Save & switch",
      cancelText: "Cancel",
    });
    if (!ok) { revert(); localMsg("Save cancelled — instrument unchanged."); return; }
  } else if (historyChanged) {
    // ── Historical window / bar size change → delete old data & re-download ──
    const parts = [];
    if (newBar !== oldBar) parts.push(`bar size <b>${oldBar || "—"}</b> → <b>${newBar || "—"}</b>`);
    if (yearsChanged) parts.push(`history <b>${oldYears || "—"} year(s)</b> → <b>${newYears} year(s)</b>`);
    const ok = await confirmDialog({
      title: "Re-download historical data?",
      messageHtml:
        `<p>You changed the ${parts.join(" and ")}.</p>` +
        `<p>The existing historical data will be <b>deleted</b> and the new window will be ` +
        `<b>downloaded automatically</b> for <b>${escapeHtml(newInstrument || oldInstrument)}</b> ` +
        `(<b>${escapeHtml(newYears || "2")} year(s)</b> at <b>${escapeHtml(newBar || oldBar)}</b>).</p>` +
        `<p class="muted">A progress message is shown while this runs. Your rules are kept and are ` +
        `re-applied to the new dataset afterwards.</p>`,
      confirmText: "Save & re-download",
      cancelText: "Cancel",
    });
    if (!ok) { revert(); localMsg("Save cancelled — historical window unchanged."); return; }
  }

  if (btn) btn.disabled = true;
  localMsg("Saving…");
  try {
    document.querySelectorAll("#rules-list .rule-card").forEach((c) => syncRuleFromDom(c));
    syncStrategyConfig();
    const r = await api("/api/v1/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: state.activeName, ruleset: rs }),
    });
    if (r.ok) {
      if (instrumentChanged) {
        window.location.reload(); // dashboard now runs in the new instrument context
        return;
      }
      if (historyChanged) {
        await startHistoryRebuild(oldBar);
        return;
      }
      // Ordinary save → reload so every panel reflects the stored strategy.
      window.location.reload();
      return;
    }
    localMsg((r.errors || []).join("; ") || r.message);
  } catch (err) {
    localMsg(`Save failed: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function startHistoryRebuild(oldBar) {
  setPMsg("Preparing re-download…");
  setActionButtonsDisabled(true);
  try {
    const q = oldBar ? `?old_bar_size=${encodeURIComponent(oldBar)}` : "";
    const s = await api(`/api/v1/dataset/rebuild${q}`, { method: "POST" });
    if (!s.started) {
      setPMsg(s.reason || "Re-download could not start.");
      window.location.reload();
      return;
    }
    pollRebuild();
  } catch (err) {
    setPMsg(`Re-download failed to start: ${err.message}`);
    setActionButtonsDisabled(false);
  }
}

async function pollRebuild() {
  try {
    const s = await api("/api/v1/dataset/rebuild");
    if (!s) { window.location.reload(); return; }
    if (s.running) {
      setPMsg(`Re-downloading historical data… ${s.rows || 0} bar(s) so far${s.label ? ` (${s.label})` : ""}`);
      setTimeout(pollRebuild, POLL_MS);
      return;
    }
    if (s.last_error) {
      setPMsg(`Re-download failed: ${s.last_error}`);
      setActionButtonsDisabled(false);
      return;
    }
    window.location.reload(); // done → summary/chart show the new dataset
  } catch (err) {
    setPMsg(`Re-download status failed: ${err.message}`);
    setActionButtonsDisabled(false);
  }
}

function saveRules() { saveStrategyFull("rules"); }
function savePConfig() { saveStrategyFull("pconfig"); }

async function resetRules() {
  const name = (state.activeName || "").trim();
  if (!name) { setMsg("No active strategy to reset."); return; }
  const ok = await confirmDialog({
    title: "Reset this strategy's rules?",
    messageHtml:
      `<p>Replace the rules of <b>“${escapeHtml(name)}”</b> with the default BUY/SELL example?</p>` +
      `<p class="muted">Only this strategy's rules are replaced. Other strategies and this strategy's ` +
      `configuration are kept.</p>`,
    confirmText: "Reset rules",
    cancelText: "Cancel",
  });
  if (!ok) { setMsg("Reset cancelled."); return; }
  setMsg(`Resetting rules of “${escapeHtml(name)}”…`);
  try {
    const r = await api("/api/v1/rules/reset", { method: "POST" });
    if (r.ok) {
      window.location.reload();
    } else {
      setMsg(r.message || "Reset failed.");
    }
  } catch (err) {
    setMsg(`Reset failed: ${err.message}`);
  }
}

/* ---------- Strategy delete (soft) ---------- */
function openDeleteStrategy() {
  const name = (state.activeName || "").trim();
  const rs = activeRuleset();
  if (!name || !rs) return;
  const backdrop = $("delete-modal-backdrop");
  const msg = $("delete-modal-message");
  const err = $("delete-modal-error");
  const input = $("delete-confirm-name");
  const btn = $("delete-confirm-btn");
  if (msg) {
    msg.innerHTML =
      `<p>You are about to delete strategy <strong>“${escapeHtml(name)}”</strong>.</p>` +
      `<p class="muted">Deleting removes it from the dashboard and stops it being the active strategy. ` +
      `The JSON entry is kept (soft delete), so creating a strategy with the same name later restores it.</p>`;
  }
  if (err) { err.textContent = ""; err.hidden = true; }
  if (input) input.value = "";
  if (btn) btn.disabled = true;
  // Opt-in: also remove the strategy instrument's dataset file(s) on delete.
  const chk = $("delete-data-check");
  const chkText = $("delete-data-text");
  if (chk) chk.checked = false;
  if (chkText) {
    const instr = String(rs.instrument || (rs.config && rs.config.INSTRUMENT) || "").trim();
    chkText.innerHTML = instr
      ? `Also delete the historical data file(s) for <code>${escapeHtml(instr)}</code>.`
      : "Also delete this strategy's historical data file(s).";
  }
  if (backdrop) backdrop.hidden = false;
  if (input) input.focus();
}

function cancelDeleteStrategy() {
  const backdrop = $("delete-modal-backdrop");
  if (backdrop) backdrop.hidden = true;
}

function onDeleteNameInput() {
  const input = $("delete-confirm-name");
  const btn = $("delete-confirm-btn");
  const err = $("delete-modal-error");
  const match = !!(input && input.value.trim() === (state.activeName || ""));
  if (btn) btn.disabled = !match;
  if (err) { err.textContent = ""; err.hidden = true; }
}

async function confirmDeleteStrategy() {
  const name = (state.activeName || "").trim();
  const input = $("delete-confirm-name");
  const btn = $("delete-confirm-btn");
  const err = $("delete-modal-error");
  const backdrop = $("delete-modal-backdrop");
  const delData = !!$("delete-data-check")?.checked;
  if (!name || !input || input.value.trim() !== name) {
    if (err) {
      err.textContent = `Name does not match — type “${name}” to confirm.`;
      err.hidden = false;
    }
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/v1/rules/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, delete_data: delData }),
    });
    if (r.ok) {
      if (backdrop) backdrop.hidden = true;
      // The page reloads below — carry the dataset outcome over so the user
      // sees whether the file(s) were deleted or kept (shared by another
      // strategy).
      if (delData) {
        let note = r.message || "Strategy deleted";
        if (r.skipped) note = r.skipped;
        else if (r.removed && r.removed.length) note = `Deleted data file(s): ${r.removed.join(", ")}`;
        else if (!r.removed) note = "No dataset file found to delete.";
        try {
          sessionStorage.setItem("traider-toast", JSON.stringify({ text: note, kind: r.skipped ? "warn" : "ok" }));
        } catch (_) { /* storage unavailable */ }
      }
      window.location.reload(); // dashboard now reflects the next active strategy
    } else {
      if (err) { err.textContent = r.message || "Delete failed."; err.hidden = false; }
      if (btn) btn.disabled = false;
    }
  } catch (ex) {
    if (err) { err.textContent = `Delete failed: ${ex.message}`; err.hidden = false; }
    if (btn) btn.disabled = false;
  }
}

(function bindRulesHandlers() {
  const host = $("rules-list");
  if (host) {
    host.addEventListener("change", onRulesChange);
    host.addEventListener("click", onRulesClick);
  }
  const delBackdrop = $("delete-modal-backdrop");
  if (delBackdrop) {
    delBackdrop.addEventListener("click", (e) => {
      if (e.target === delBackdrop) cancelDeleteStrategy();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !delBackdrop.hidden) cancelDeleteStrategy();
    });
  }
})();

/* ---------- Backtest panel (below the chart) ---------- */
let btPollTimer = null;
let btChart = null;
let btDelta = null;   // latest /api/v1/delta/status (dataset currency)
let btPayload = null; // latest /api/v1/backtest payload

async function loadBacktest() {
  try {
    const b = await api("/api/v1/backtest");
    btPayload = b;
    let delta = null;
    try { delta = await api("/api/v1/delta/status"); } catch (_) { /* non-fatal */ }
    btDelta = delta;
    renderBacktest(b, delta);
    if (b.status && b.status.running) pollBacktest(1500);
  } catch (err) {
    const msg = $("bt-msg");
    const body = $("bt-body");
    if (msg) { msg.textContent = `Backtest panel unavailable: ${err.message}`; msg.style.color = "#ff9a94"; }
    if (body) body.hidden = true;
  }
}

function btDataReady(delta) {
  const d = delta || btDelta;
  if (!d) return false;
  // Only a dataset with no missing days (and no status error) unlocks Run.
  return d.exists === true && d.synced === true && !d.error && !(d.missing || []).length;
}

// Mutations are locked while a backtest is in flight so the strategy/config
// can't change underneath a run — AND while trading is ON, so a running strategy
// can never be reconfigured mid-flight. The server enforces both with a 409; this
// list only keeps the UI honest so nothing is clickable that would be rejected.
function setBtBusyControls(busy) {
  const off = !!busy || !!state.tradingLocked;
  [
    "save-config", // global (.env) settings
    "account-save", // account settings (broker, folders, backtest defaults)
    "save-pconfig", // strategy configuration
    "save-rules", // strategy rules
    "save-risk", // risk management
    "rules-add-buy", "rules-add-sell", "rules-reset", // rule editing (dead-ended without a save)
    "exec-env", // switching paper<->live mid-run is the worst case of all
    "strategy-create-btn", // ＋ New (inline Create)
    "strategy-rename", "strategy-rename-btn", // ✏ Rename
    "strategy-delete", // 🗑 Delete
    "strategy-select", // switch active strategy
  ].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = off;
  });
  applyStrategyAddState(off); // ＋ New also respects the strategy-count limit
}

// “＋ New” is locked while a backtest runs AND when the strategy-count limit
// (rules_service.MAX_STRATEGIES, surfaced in the rules payload) is reached.
// Hovering the disabled button explains why via a data-tip tooltip.
function applyStrategyAddState(busy) {
  const btn = $("strategy-add");
  if (!btn) return;
  const p = state.rulesPayload;
  const names = Object.keys(p ? p.strategies || {} : {});
  const max = (p && p.max_strategies) || 5;
  const limited = names.length >= max;
  btn.disabled = !!busy || limited || !!state.tradingLocked;
  if (limited) {
    // Single custom (two-line) tooltip only — clear the native title so the
    // browser doesn't also show its own one-line tooltip on top of it.
    btn.dataset.tip = `Strategy limit reached (${max}) — delete one to create another.`;
    btn.title = "";
    btn.removeAttribute("title");
  } else {
    delete btn.dataset.tip;
    btn.title = "Create a new strategy";
  }
}

async function refreshBtDataState() {
  try { btDelta = await api("/api/v1/delta/status"); } catch (_) { btDelta = null; }
  renderBacktest(btPayload, btDelta);
}

// Local re-render after the rules payload/cards change (no network).
function syncBtRules() {
  if (document.getElementById("backtest")) renderBacktest(btPayload, btDelta);
}

// Number of rules currently set for the displayed strategy (live from the
// Rules panel payload, which addRule/remove-rule keep in sync).
function currentRuleCount() {
  const rs = activeRuleset();
  return rs && Array.isArray(rs.rules) ? rs.rules.length : 0;
}

function renderBacktest(d, delta) {
  if (d) btPayload = d;
  const runBtn = $("bt-run");
  const msgEl = $("bt-msg");
  const body = $("bt-body");
  const guide = $("bt-guide");
  const noRules = $("bt-norules");
  const warnEl = $("bt-datawarn");
  const spin = $("bt-spinner");
  const running = !!(d && d.status && d.status.running);
  state.btRunning = running; // remembered so the trading lock can re-apply it
  setBtBusyControls(running);
  if (spin) spin.hidden = !running;

  // Run is enabled only when: data is fully synced AND at least one rule is set.
  // Trading ON always wins — the backtesting panel is frozen then.
  const ready = btDataReady(delta);
  const rulesCount = currentRuleCount();
  const canRun = !running && ready && rulesCount > 0;
  if (runBtn) runBtn.disabled = !canRun || !!state.tradingLocked;

  // Warnings reflect CURRENT conditions, whether or not a backtest ran before.
  const dd = delta || btDelta;
  let dataTxt = "";
  if (!running && dd) {
    if (dd.error) dataTxt = `⚠ Could not verify the dataset is up to date (${dd.error}) — Run backtest stays disabled.`;
    else if (dd.exists === false) dataTxt = "⚠ No historical data for this strategy yet — download it before backtesting.";
    else if (dd.exists === true && !dd.synced) {
      const uniq = Array.from(new Set(dd.missing || []));
      const n = (dd.missing || []).length;
      dataTxt = `⚠ Historical data is missing ${n} completed ${barWord(n)}` +
        `${uniq.length ? `: ${uniq.join(", ")}` : ""}. ` +
        "Use Historical Delta → “Fetch bars” to sync; " +
        "Run backtest stays disabled until data is up to date.";
    }
  }
  if (warnEl) { warnEl.textContent = dataTxt; warnEl.hidden = !dataTxt; }
  if (noRules) noRules.hidden = running || rulesCount > 0;

  if (!msgEl || !body || !guide) return;
  const r = (d && d.result) || null;
  syncBtReportBtn(d);

  if (running) {
    body.hidden = true;
    guide.hidden = true;
    msgEl.textContent = "Running backtest…";
    msgEl.style.color = "";
    return;
  }

  if (r) {
    // A backtest has already been run -> show its results (warnings stay above).
    guide.hidden = true;
    if (!r.ok || r.error) {
      body.hidden = true;
      msgEl.textContent = r.error || "Backtest failed.";
      msgEl.style.color = "#ff9a94";
      return;
    }
    body.hidden = false;
    msgEl.textContent = "";
    msgEl.style.color = "";
    renderBtGate(r.gate);
    renderBtMetrics(r.metrics, r.gate);
    const notes = $("bt-notes");
    if (notes) notes.textContent = (r.notes || []).join(" · ");
    drawBtCurve(r.equity_curve);
    return;
  }

  // No run yet: when everything is ready show the how-it-works guide, else the
  // warnings above are the explanation.
  body.hidden = true;
  const showGuide = ready && rulesCount > 0;
  guide.hidden = !showGuide;
  msgEl.textContent = "";
  msgEl.style.color = "";
}

function fmtPct(v) {
  if (v == null) return "—";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
}

function renderBtGate(gate) {
  const el = $("bt-gate");
  if (!el) return;
  if (!gate) { el.hidden = true; return; }
  el.hidden = false;
  el.className = "bt-gate";
  // Only the outcome word — the individual gates live on the metric boxes
  // below (tinted green/red, with a hover hint on each).
  el.innerHTML =
    `<span class="bt-gate-title">` +
    `${state.activeName ? `<span class="bt-gate-name">${escapeHtml(state.activeName)} ·</span>` : ""}` +
    `<span class="bt-gate-result ${gate.pass ? "pass" : "fail"}">` +
    `${gate.pass ? "SUCCESS" : "FAIL"}</span>` +
    `</span>`;
}

// Short description of the condition that makes a gate pass (box hover hint).
function gateHint(key, g) {
  const t = (x) => (typeof x === "number" ? Number(x).toFixed(2) : x);
  if (key === "sharpe") return `Gate passes when Sharpe is at least ${t(g.target)}`;
  if (key === "max_drawdown") return `Gate passes when max drawdown is at most ${t(g.target)}%`;
  if (key === "win_rate") return `Gate passes when win rate is at least ${t(g.target)}%`;
  if (key === "worst_week") return `Gate passes when the worst week loss is at most ${t(g.target)}%`;
  return `Gate target ${t(g.target)}`;
}

function renderBtMetrics(m, gate) {
  const el = $("bt-metrics");
  if (!el) return;
  if (!m) { el.innerHTML = ""; return; }
  const gateInfo = (k) => (gate && gate.checks ? gate.checks[k] : null);
  // A stat tile that maps to a Gate check is tinted green/red and its hover
  // hint explains the passing condition.
  const stat = (label, value, cls, gateKey) => {
    const g = gateKey ? gateInfo(gateKey) : null;
    const gcls = g ? (g.pass ? " gate-pass" : " gate-fail") : "";
    const tip = g ? ` data-tip="${escapeHtml(gateHint(gateKey, g))}"` : "";
    return `<div class="bt-stat${gcls}"${tip}>` +
      `<span class="label">${label}</span><span class="value ${cls || ""}">${value}</span></div>`;
  };
  const pf = (m.profit_factor == null) ? "—" : (Number.isFinite(m.profit_factor) ? m.profit_factor.toFixed(2) : "∞");
  el.innerHTML = [
    stat("Total return", fmtPct(m.total_return_pct), m.total_return_pct >= 0 ? "pos" : "neg"),
    stat("Ann. return", fmtPct(m.annualized_return_pct)),
    stat("Ann. vol", fmtPct(m.annualized_vol_pct)),
    stat("Sharpe", (m.sharpe ?? 0).toFixed(2), "", "sharpe"),
    stat("Max drawdown", fmtPct(-(m.max_drawdown_pct ?? 0)), "", "max_drawdown"),
    stat("Worst week", fmtPct(m.worst_week_pct), "", "worst_week"),
    stat("Win rate", (m.win_rate_pct ?? 0).toFixed(1) + "%", "", "win_rate"),
    stat("Trades", m.num_trades ?? 0),
    stat("Avg win", fmtPct(m.avg_win_pct)),
    stat("Avg loss", fmtPct(m.avg_loss_pct)),
    stat("Profit factor", pf),
    stat("Exposure", (m.exposure_pct ?? 0) + "%"),
  ].join("");
}

function drawBtCurve(points) {
  const host = $("bt-curve");
  if (!host) return;
  host.innerHTML = "";
  if (btChart) {
    forgetCrosshairAnchor(btChart);
    try { btChart.remove(); } catch (_) { /* noop */ }
    btChart = null;
  }
  if (!points || !points.length) { host.textContent = "—"; return; }
  try {
    const chart = LightweightCharts.createChart(host, {
      height: 140,
      layout: { background: { color: "transparent" }, textColor: "#8a93a6" },
      grid: { vertLines: { color: "#22262f" }, horzLines: { color: "#22262f" } },
      rightPriceScale: { borderColor: "#333a46" },
      timeScale: { borderColor: "#333a46", visible: false },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    const line = chart.addLineSeries({ color: "#4c8dff", lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
    const curve = points.map((p) => ({ time: p.time, value: p.equity }));
    line.setData(curve);
    btChart = chart;
    // The equity curve joins the crosshair sync too (its zoom already follows
    // the main chart), anchored on the equity value at each time.
    const anchor = _crosshairValues(curve);
    registerCrosshairAnchor(chart, line, anchor.values, anchor.fallback);

    // Link zoom with the main price chart, exactly like the oscillator panes:
    // wait for this chart's initial paint (2 frames) before attaching the
    // two-way time sync, so its own default right-aligned view can't be pushed
    // back into the main chart and reset the user's zoom. Then pin it to the
    // main chart's CURRENT visible range rather than a full-content fit.
    const main = state.chart;
    const mainRange = main ? main.timeScale().getVisibleRange() : null;
    if (main && mainRange) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          if (btChart !== chart || !state.chart) return; // superseded/rebuilt
          subscribeOscToMainTime(chart); // equity curve -> main chart
          _syncTimeRange(chart, mainRange); // main chart -> equity curve
        });
      });
    } else {
      chart.timeScale().fitContent();
    }
  } catch (_) {
    host.textContent = "Equity curve unavailable.";
  }
}

// The full-report button appears only once at least one run has been stored for
// this strategy: the report page reads runs from disk, and a result recorded
// before the run layout existed has none.
function syncBtReportBtn(d) {
  const btn = $("bt-report");
  if (!btn) return;
  const hasRuns = !!(d && d.run_count > 0);
  btn.hidden = !hasRuns;
  btn.title = hasRuns
    ? `Open the full report (${d.run_count} run${d.run_count === 1 ? "" : "s"} stored)`
    : "";
}

// Opens the standalone report page for the CURRENT strategy in THIS tab (the
// page has its own "← Back to dashboard" link). The run id is passed when the
// shown result has one, otherwise the page opens the newest stored run.
function openBacktestReport() {
  const qs = new URLSearchParams();
  if (state.activeName) qs.set("strategy", state.activeName);
  const r = btPayload && btPayload.result;
  if (r && r.run_id) qs.set("run_id", r.run_id);
  window.location.href = "/report?" + qs.toString();
}

async function runBacktest() {
  const msg = $("bt-msg");
  if (msg) msg.textContent = "Starting backtest…";
  try {
    const r = await api("/api/v1/backtest/run", { method: "POST" });
    if (r.started) {
      // Reflect the running state immediately (locks edits + shows spinner),
      // before the first status poll arrives.
      btPayload = { status: { running: true } };
      renderBacktest(btPayload, btDelta);
      pollBacktest(1500);
    } else {
      if (msg) msg.textContent = r.reason || "Backtest could not start.";
      renderBacktest(btPayload, btDelta);
    }
  } catch (err) {
    if (msg) msg.textContent = `Failed to start backtest: ${err.message}`;
    renderBacktest(btPayload, btDelta);
  }
}

function pollBacktest(ms) {
  clearTimeout(btPollTimer);
  btPollTimer = setTimeout(async () => {
    try {
      const d = await api("/api/v1/backtest");
      const wasRunning = !!(d.status && d.status.running);
      renderBacktest(d, btDelta);
      if (d.status && d.status.running) {
        pollBacktest(1500);
      } else if (wasRunning) {
        refreshBtDataState(); // run finished -> re-check dataset currency
      }
    } catch (_) { /* transient */ pollBacktest(3000); }
  }, ms);
}


/* ---------- Transient toast ---------- */
function flashToast(text, kind) {
  const t = $("toast");
  if (!t) return;
  t.textContent = text;
  t.className = kind === "warn" ? "warn" : kind === "ok" ? "ok" : "";
  t.hidden = false;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { t.hidden = true; }, 7000);
}

function showPendingToast() {
  try {
    const raw = sessionStorage.getItem("traider-toast");
    if (!raw) return;
    sessionStorage.removeItem("traider-toast");
    const m = JSON.parse(raw);
    if (m && m.text) flashToast(m.text, m.kind || "");
  } catch (_) { /* ignore malformed */ }
}

ensureMainChartWheel();
watchEnvSelect(); // arm the environment dropdown so only a real gesture can change it
showPendingToast();
refresh();
loadConfig();
loadAccount(true);
loadRules();
loadTrading(); // last: it applies the configuration lock on top of the rendered panels
