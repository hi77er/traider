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
// A round trip that changed the equity by exactly nothing. Deliberately grey
// and distinct from POSITION_SHADE: blue means "no outcome recorded" (an older
// payload), grey means "recorded, and it was exactly break-even". Reading a
// zero as a loss is how a zero-weight run painted every profitable trade red.
const POSITION_SHADE_FLAT = "rgba(158, 158, 158, 0.20)";

const state = {
  status: null,
  rows: [],
  total: 0,
  offset: 0,
  // Row count of the dataset as of the last delta payload. A fetch is considered to
  // have changed something when this moves — which is not the same as `synced`
  // becoming true, because a fetch can add today's bars and still leave an older hole
  // the provider cannot fill. Keying the chart refresh on `synced` left the chart
  // showing the old window in exactly that case.
  deltaRows: null,
  // Whether the dashboard hides the intervals nobody traded (the "Hide all bars missing
  // due to no liquidity" toggle in the Historical Delta panel). ON by default: those bars
  // are listed for accounting, not as a to-do, and on a thin symbol they bury the handful
  // that can actually be fetched. Untick to see them again — the state survives
  // re-renders, which is why it lives here and not in the DOM.
  hideNoTrades: true,
  // The last delta payload, so that toggle can re-render the panel from what the server
  // already said instead of asking again.
  deltaStatus: null,
  // Size of the cached `datasetRows` copy the chart is drawn from (see
  // forgetDatasetRows). Null when nothing is cached.
  chartRows: null,
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
  // The broker half of the Trading panel, kept between the fast polls (which fetch none of it)
  // and across a FAILED read: the boxes show the last figures that were actually read.
  liveOrders: null,
  liveMarket: null,
  liveAccounts: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    // A dashboard process started before an endpoint existed still serves the CURRENT
    // app.js from disk, so the page is new and its routes are old. "404: Not Found" reads
    // like a typo in the URL; this says what actually happened — and it happens every time
    // the dashboard is left running across a change.
    if (res.status === 404) {
      throw new Error(`${path} is missing (404) — the dashboard is running older code than "
        + "this page, so restart it`);
    }
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

// Drop the chart's cached copy of the bars.
//
// `state.datasetRows` is fetched ONCE and kept (see loadChart) so that redrawing the
// dashboard costs no request. A cached copy of a dataset that has since GROWN is worse
// than no cache at all: `buildMainChart` would redraw the old window while every other
// panel — all of which refetch — moved on, so a fetch appeared to add bars to the
// table, the summary and the delta column but not to the chart.
//
// `state.chartRows` is the size of that copy. ANY change to the row count drops it,
// which is what makes this path-independent: it does not matter whether a delta fetch,
// the initial download or another process wrote the bars.
function forgetDatasetRows() {
  state.datasetRows = null;
  state.chartRows = null;
}

function render(s) {
  state.status = s;
  // Invalidate BEFORE loadChart() runs below, so the refetch happens in this same pass
  // rather than one reload later.
  if (typeof s.rows === "number" && state.chartRows !== null && state.chartRows !== s.rows) {
    forgetDatasetRows();
  }
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
    "1m": "1 minute", "2m": "2 minutes", "5m": "5 minutes", "15m": "15 minutes",
    "30m": "30 minutes", "1h": "1 hour", "2h": "2 hours", "4h": "4 hours",
    "8h": "8 hours", "12h": "12 hours", "1d": "1 day", "1W": "1 week",
    "1M": "1 month",
  })[code] || code;
}

/* A history window reads as a duration with its unit: "2y" -> "2 years",
   "30d" -> "30 days". A bare number is read as YEARS, because that is what the
   setting stored before the unit existed, so a legacy value still displays
   correctly. Anything unreadable is shown as-is rather than as a wrong guess. */
function periodLabel(period) {
  if (period == null || period === "") return "—";
  const m = /^(\d+)\s*([A-Za-z]*)$/.exec(String(period).trim());
  if (!m) return String(period);
  const n = Number(m[1]);
  const unit = m[2].toLowerCase();
  const word = (unit === "d" || unit === "day" || unit === "days") ? "day" : "year";
  return `${n} ${word}${n === 1 ? "" : "s"}`;
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
      <div><span class="label">Historical Period</span><span>${periodLabel(s.period_label || s.period || s.period_years)}</span></div>
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
      // Record what the cached copy is a copy OF, so a later render can tell that the
      // file has moved on (see forgetDatasetRows).
      state.chartRows = (d.rows || []).length;
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

/* The exchange's time zone, as the dataset status reports it.
 *
 * A server started before the payload carried this field says nothing, and the labels
 * would then be UTC — four hours off the table's numbers beside them. That is a restart
 * pending, not a chart bug, so say it once in the console rather than leaving the axis
 * quietly wrong. */
let _warnedZone = false;
function axisZone(s) {
  const zone = s.market_timezone;
  if (zone) return zone;
  if (!_warnedZone) {
    _warnedZone = true;
    console.warn(
      "TRAIDER: the dataset payload carries no market_timezone, so the time axis is " +
      "labelled in UTC. Restart the server to pick the setting up."
    );
  }
  return "UTC";
}

/* Time-axis options for the dataset on screen (see chart_time.js), merged onto a
   chart's own timeScale settings. The bar size and the exchange's time zone both come
   from the dataset status, which render() stores before any chart is built.

   The `ChartTime` guard is for a stale cached page: an axis option is never worth
   blanking every chart over. */
function axisTimeScale(extra) {
  const base = Object.assign({}, extra);
  if (typeof ChartTime === "undefined") return base;
  const s = state.status || {};
  return Object.assign(base, ChartTime.timeScaleOptions(s.interval || "", axisZone(s)));
}

// ...and the matching crosshair-label options, so the crosshair names a bar the same
// way the tables beside it do.
function axisLocalization() {
  const s = state.status || {};
  return typeof ChartTime === "undefined" ? {} : ChartTime.localizationOptions(axisZone(s));
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
    // Each candle is labelled with the bar it actually IS — its own minute, hour or
    // day — in exchange-local time (see chart_time.js).
    timeScale: axisTimeScale({ borderColor: "#333a46", minBarSpacing: ChartZoom.MIN_BAR_SPACING }),
    localization: axisLocalization(),
    rightPriceScale: { borderColor: "#333a46" },
    // Free-floating crosshair (not snapped to a bar's extremes). Its vertical line keeps
    // its boxed time label on the time axis: the axis names each candle's PERIOD, while
    // the label is what gives the exact instant under the pointer — including between
    // bars and in the whitespace past the last one (see chart_time.js).
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    // Zoom happens ONLY on a trackpad/touch pinch, clamped to the data (see
    // chart_zoom.js). A plain mouse wheel must never zoom the chart, and dragging
    // the time axis must not zoom either — moving the chart left/right only pans it.
    handleScroll: { mouseWheel: true }, // horizontal wheel/drag still pans
    handleScale: {
      mouseWheel: false,
      axisPressedMouseMove: { time: false, price: true },
    },
  });
  state.chart = chart;
  subscribeMainToOscTime(); // the price chart drives every other chart's range

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
  const bars = (state.datasetRows || [])
    .map((r) => ({
      // unix seconds for intraday, 'YYYY-MM-DD' for daily bars
      time: r.time != null ? r.time : r.date,
      open: toNum(r.open), high: toNum(r.high),
      low: toNum(r.low), close: toNum(r.close),
    }))
    .filter((b) => b.time != null && b.open !== null && b.high !== null
      && b.low !== null && b.close !== null);
  series.setData(bars);
  // How many bars there are to show, and which bars they are: the zoom stops at
  // exactly this many (so "zoom out as far as it goes" means the whole history is
  // on screen) and every other chart is placed on the same bars by these times.
  state.chartBarCount = bars.length;
  state.chartTimes = bars.map((b) => b.time);

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
// that carries even a small vertical component zooms while panning. That is why
// wheel-zoom is OFF on every chart here (handleScale.mouseWheel: false) and only
// a real trackpad pinch zooms — the browser reports it as a wheel event with
// ctrlKey set. The zoom itself, including where it stops, lives in
// chart_zoom.js and is shared with every pane and the report page.
function ensureMainChartWheel() {
  // One listener on the element, re-pointed at whatever chart occupies it now:
  // a rebuild replaces the chart, not the element.
  ChartZoom.bind($("chart-canvas"), () => ({
    chart: state.chart,
    barCount: state.chartBarCount || 0,
  }));
}

/* ---------- Strategy Signals panel (rule-based model test) ---------- */
async function loadSignals() {
  const host = $("signals");
  if (!host) return;
  syncSignalToggles();
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

/* The two signal toggles live in the Price History panel now — they change what is drawn on that
 * chart — and that panel is never re-rendered, so their checked state is SYNCED from the render
 * state here rather than baked into markup. A re-render would also drop the focus of a checkbox
 * the operator just clicked; syncing only ever sets `checked`, which cannot. */
function syncSignalToggles() {
  const markers = $("signals-toggle");
  const executed = $("exec-signals-toggle");
  if (markers) markers.checked = !!state.showSignals;
  if (executed) executed.checked = !!state.showOnlyExecuted;
}

/* Sentence case for the lines in the signals panel: they are read as prose under the badge, and
 * a sentence that starts lowercase reads as a fragment of the one above it. Applied at RENDER,
 * not at the source, because the same strings are fragments elsewhere — the model's reasons
 * ("no rule fired") are table cells in the report and the log, where the house style is
 * lowercase. Only the first character is touched; the rest of the string is left alone. */
function sentenceCase(text) {
  const s = String(text === null || text === undefined ? "" : text);
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function renderSignals(d) {
  const host = $("signals");
  if (!host) return;
  if (!d || d.available === false) {
    host.innerHTML = `
      <div class="signal-head">
        <h2>Strategy Signals</h2>
      </div>
      <p class="muted signal-reason">${escapeHtml(sentenceCase((d && d.reason) || "Signals unavailable for this model type."))}</p>
      <p class="muted signal-counts">Model: ${escapeHtml((d && d.model_type) || "?")} · Rule signals not shown</p>`;
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
    </div>
    <div class="signal-latest">
      <span class="signal-badge ${badgeClass}">${sig}</span>
      <span class="signal-meta">
        ${when ? `<span class="label">as of</span><span>${escapeHtml(when)}</span>` : ""}
        ${conf != null ? `<span class="label">conf</span><span>${conf}</span>` : ""}
        <!-- Which rule fired (or "no rule fired") sits on the same line as the reading it
             explains: as a paragraph under the badge it read as a separate remark, and the
             eye had to travel down and back to pair the two up. -->
        ${reason ? `<span class="muted signal-reason">${escapeHtml(sentenceCase(reason))}</span>` : ""}
      </span>
    </div>
    ${signalTiles(d)}`;
}

/* The counts as BOXES, the same `.bt-stat` the trading panel, the backtest and the report use:
 * three numbers are read by glancing at boxes, while a sentence has to be read to the end. The
 * grid is declared for this id — the shared rule is scoped to `#bt-metrics` — and with a narrower
 * minimum than the trading panel's, so all three fit the half-width slot.
 *
 * The risk STATE that used to be the fourth box is the Risk section under this one now: a box
 * saying "on"/"off" reported that the settings exist, and the settings themselves are what decides
 * whether these signals are the ones the bot would act on. */
function signalTiles(d) {
  const counts = d.counts || {};
  const tiles = [
    liveTile("Buy", String(counts.BUY ?? 0)),
    liveTile("Sell", String(counts.SELL ?? 0)),
    liveTile("Hold", String(counts.HOLD ?? 0)),
  ];
  return `<div class="signal-metrics">${tiles.join("")}</div>`;
}

/* One risk setting as a box: what it is set to, or — when it is EMPTY — what empty means, in the
 * schema's own words ("no stop", "the whole account").
 *
 * EMPTY MEANS NOT APPLIED is the rule the whole risk layer is built on (``src/strategy/config``):
 * there is deliberately no master switch, so a blank field is a decision rather than a gap. A box
 * that showed "—" for five of the eight settings would say nothing about what the bot does with
 * them, which is the one thing this section is for. */
function riskValue(field, value) {
  const text = String(value === null || value === undefined ? "" : value).trim();
  if (!text) return escapeHtml(field.empty_means || "—");
  if (field.type === "bool") return text === "True" ? "on" : "off";
  // A select ships its values as tokens ("fixed_risk"); the words in a box are not worth a second
  // schema, so the underscores go and nothing else is touched.
  return escapeHtml(text.replace(/_/g, " "));
}

function strategyRiskTiles() {
  const groups = strategyRiskGroups();
  const rs = activeRuleset();
  const stored = (rs && rs.config) || {};
  const tiles = [];
  for (const group of groups) {
    for (const f of group.fields || []) {
      // The same resolution the Risk Management panel makes: the strategy's stored value, or the
      // current default it would fall back to. One source, so the box and the field cannot
      // disagree.
      const value = stored[f.key] != null ? stored[f.key] : f.default_value;
      tiles.push(liveTile(f.label, riskValue(f, value), "", (f.hints || []).join(" ")));
    }
  }
  return tiles;
}

function renderStrategyRisk() {
  const host = $("strategy-risk");
  if (!host) return;
  const tiles = strategyRiskTiles();
  setIfChanged(host, tiles.length
    ? tiles.join("")
    : `<p class="muted">No active strategy — create one with “＋ New” in the Strategy bar above.</p>`);
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
  // Entries the risk layer refused: no position was ever opened, so these
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
  // (an older payload) keeps the neutral tint rather than guessing. `outcome`
  // is the three-state field — win / loss / flat — and `win` is the older
  // boolean, still honoured so a cached payload keeps its colours. A flat
  // round trip is grey: neither a win nor a loss.
  const shadeFor = (f) => {
    const outcome = f.outcome != null ? String(f.outcome)
      : (f.win === true ? "win" : f.win === false ? "loss" : "");
    if (outcome === "win") return POSITION_SHADE_WIN;
    if (outcome === "loss") return POSITION_SHADE_LOSS;
    if (outcome === "flat") return POSITION_SHADE_FLAT;
    return POSITION_SHADE;
  };
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

/* ---------- settings fields ----------
   ONE renderer for every settings form in the app. The global (.env) form it was
   written for is gone — global settings are edited in `.env` directly for now — so
   this is kept for the Account Settings popup and the strategy panel, which is what
   makes the two look and behave identically. */
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
    // A stored secret shows the MASK as the field's value, not as a faint grey
    // placeholder: "is a credential saved?" has to be answerable at a glance, and an
    // empty box reads as "nothing was saved" even when the pair is on file and was
    // just verified. The mask is exactly what the server sent, and submitting it back
    // means "unchanged" (the save path treats MASK and "" identically), so having it
    // in the field changes no behaviour — only what the operator can see.
    control.value = f.set ? String(f.value == null ? "" : f.value) : "";
    control.placeholder = "(unset)";
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
  // A credential pair gets a Validate button, placed by the schema (f.verify)
  // rather than by a hard-coded key list. The row renders CLEAN: a verdict is the
  // answer to a question someone asked, and nobody has asked one in this form yet.
  if (f.verify) {
    wrap.appendChild(credentialRow(f.verify));
  }
  return wrap;
}

/* ---------- Alpaca credential verification ----------
   Having a key pair configured is not the same as having one that WORKS: keys get
   copied from the wrong account page, revoked, or paired with the other
   environment's secret. Nothing local can tell, so the popup offers to ask Alpaca
   and reports what it said — and turning trading on is gated on the answer.

   The badge is a RESULT, not a state: it is painted by the answer to a check
   (the Validate button, or the check a save runs on a new pair) and by nothing
   else. A stored verdict is therefore not shown when the popup opens, because the
   box beside it is empty or masked and "⚠ not valid" next to what looks like no
   credentials reads as a bug — the row is clean until someone asks. */
function credWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : `${d.toISOString().slice(11, 16)} UTC`;
}

function credentialRow(spec) {
  const row = document.createElement("div");
  row.className = "cred-row";
  row.dataset.env = spec.env;
  const badge = document.createElement("span");
  badge.id = "cred-badge-" + spec.env;
  clearCredentialBadge(badge);
  row.appendChild(badge);
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

// An empty badge — the state a row is in until something is checked.
function clearCredentialBadge(badge) {
  if (!badge) return;
  badge.hidden = true;
  badge.className = "cred-badge";
  badge.textContent = "";
  badge.title = "";
}

// Clears every row. Called when the popup opens: the verdicts from the last visit
// are stale by definition (the keys may have changed since), so the operator gets
// one clean surface and an explicit reason to press Validate.
function clearCredentialBadges() {
  for (const env of ["paper", "live"]) {
    clearCredentialBadge(document.getElementById("cred-badge-" + env));
  }
}

function applyCredentialBadge(badge, cred) {
  if (!badge) return;
  const c = cred || {};
  // Only a verdict ABOUT the pair in play is worth showing. A check that had
  // nothing to look at ("both fields are needed") is an answer to the press, not a
  // verdict about a pair, so it leaves the row alone and speaks in the message area.
  if (!c.has_verdict) {
    clearCredentialBadge(badge);
    return;
  }
  if (c.verified) {
    const bits = ["✓ verified"];
    if (c.account_number) bits.push(c.account_number);
    const when = credWhen(c.checked_at);
    if (when) bits.push(when);
    badge.hidden = false;
    badge.className = "cred-badge ok";
    badge.textContent = bits.join(" · ");
    badge.title = c.message || "These credentials were accepted by Alpaca.";
  } else {
    // The reason matters more than the verdict, but it can be long: short label,
    // full text on hover, and the popup's message area repeats it.
    badge.hidden = false;
    badge.className = "cred-badge bad";
    badge.textContent = "⚠ not valid";
    badge.title = c.message || "Verification failed.";
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
    // Only the pair that was just checked: the response also carries the stored
    // state of every other pair, and painting that here is how a stale "not valid"
    // ended up next to a row nobody had asked about.
    applyCredentialBadge(document.getElementById("cred-badge-" + env), res);
    // "Nothing to validate" is information, not a failure: an empty form is answered
    // in the message area, in neither the warning nor the success colour.
    showAccountErrors(credentialMessage(env, res), res.checked ? (res.ok ? "ok" : "warn") : "");
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
  forgetDatasetRows(); // a different instrument's bars are not this chart's copy
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

/* ---------- Account settings dialog (data/account/account.json) ---------- */
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
  // ...and no stale verdict either: rows only carry a badge for a check performed
  // while this popup has been open.
  clearCredentialBadges();
  backdrop.hidden = false;
}

function closeAccountSettings() {
  const backdrop = $("account-settings-backdrop");
  if (backdrop) backdrop.hidden = true;
}

/* ---------- Execution: the switch, the mode, and the config lock ----------
   Which account orders would go to, whether any are being sent, and what is open are the Mode,
   Trading and Open boxes in the Trading panel — the panel is the only place any of the three is
   reported or changed. A paper and a live account are indistinguishable everywhere else, which is
   exactly how live orders get sent by accident.

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
  renderTradingPanel(d);
  // Local files only — see the Live section for why the broker half waits for the panel
  // to be opened.
  loadLive(false);
  // ...and ARM the poll. Without this the panel read the loop's files once and then sat still
  // until something else moved it — the ↻ button, folding the row, a tab switch — so the
  // account boxes, which come from the broker half on the slow cadence, never arrived at all
  // and simply looked missing. The first poll IS the slow half (`lastSlow` starts at 0), so
  // they land seconds after the page does.
  scheduleLivePoll();
  applyConfigLock();
  // Said once per page load, not on every poll: a server that is older than the files
  // it was started from will keep answering with the gate it loaded, and only a
  // restart fixes that. Silence would make the switch look trustworthy.
  const fresh = d.freshness || {};
  if (fresh.stale && !state.staleGateWarned) {
    state.staleGateWarned = true;
    flashToast(fresh.message, "warn");
  }
}

/* The one way into the mode: a click on the Mode box. The mode it moves FROM is the one the box
 * is showing, so the box and the account it is about can never disagree about which way "the
 * other one" is. The write itself is the shared module's, because the log page's box moves the
 * same mode — one confirmation, one endpoint, one wording. */
function onModeBoxClick() {
  const from = inPlayEnv() || "paper";
  return TraiderSwitch.flipEnv({
    api,
    confirmDialog,
    flashToast,
    env: from === "live" ? "paper" : "live",
    from: from,
    reload: async () => {
      await loadTrading();
      // ...and then read the OTHER ACCOUNT's figures at once, not on the next slow poll. The panel
      // has just changed which account it is about, while every box on screen still holds the one
      // we left: waiting up to a minute would show the paper account's equity under a live label,
      // which is the exact confusion this panel exists to prevent. `loadTrading` cannot do it — it
      // reads the switch, and deliberately takes only the files half of the live panel.
      await refreshLiveNow();
    },
  });
}

// The switch itself lives in ``trading_switch.js``: the trading log page carries the same
// control, and the confirmation that stands between a click and real orders is not something to
// keep two copies of. What is left here is the dashboard's own half — hand over the page's
// helpers, then re-read the state that was just changed.
async function toggleTrading() {
  const result = await TraiderSwitch.flip({
    api,
    confirmDialog,
    flashToast,
    trading: state.tradingState,
    execution: state.executionStatus,
  });
  if (!result.wrote) return;
  await loadTrading();
  // A lock change alters what is available, so let the panels that own specific
  // buttons recompute them (they re-enable only what is genuinely possible).
  if (state.rulesPayload && typeof renderStrategyBar === "function") renderStrategyBar();
}

async function stopAndFlatten() {
  const exec = state.executionStatus || {};
  const open = Number((state.tradingPayload || {}).open_count || 0);
  const ok = await confirmDialog({
    title: exec.live ? "Stop trading and close the position?" : "Stop trading and close the position?",
    messageHtml:
      (open ? `${open} position(s) will be closed at market, ` : "Trading will stop, ") +
      (exec.live
        ? "and the orders go to your <b>LIVE</b> account — this is real money."
        : "and the orders go to the paper account.") +
      " Trading goes off first, so a failed close still leaves the bot stopped.",
    confirmText: "Stop &amp; flatten",
  });
  if (!ok) return;
  let r = null;
  try {
    r = await api("/api/v1/trading/off-flatten", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // The acknowledgement is required for the LIVE flatten, and only for it: closing a
      // real position costs real money, while stopping must never need anything.
      body: JSON.stringify({ confirm_live: !!exec.live }),
    });
  } catch (err) {
    flashToast(`Stop & flatten failed: ${err.message}`, "warn");
    return;
  }
  if (r && r.ok === false) flashToast(r.message || "Stop & flatten did not finish", "warn");
  else flashToast(r && r.message ? r.message : "Trading is OFF", "ok");
  await loadTrading();
  if (state.rulesPayload && typeof renderStrategyBar === "function") renderStrategyBar();
}

function renderTradingPanel(d) {
  const tr = d.trading || {};
  const exec = d.execution || {};
  const open = Number(d.open_count || 0);
  // The card is ALWAYS on screen — it is the one place trading is described, and it replaced
  // a separate switch card that duplicated half of it — so nothing here hides it.
  //
  // The flatten button STAYS: it is not the switch (it closes what is open as well), and it is the
  // only place to reach that while trading is off. It follows what is OPEN rather than what is
  // armed, because a position held with trading OFF is exactly the state that needs it.
  const flatBtn = $("trading-flatten-btn");
  if (flatBtn) flatBtn.hidden = !open;

  const msg = $("trading-msg");
  if (msg) {
    // Only the armed sentence: which account and broker, and since when. The COUNT went to the
    // Open box, and off there is nothing left here to say — the Mode box names the account — so
    // the line collapses rather than repeat either of them.
    msg.textContent = tr.on
      ? `${String(tr.env || "").toUpperCase()} account · ${exec.broker || "—"} · since ${shortWhen(tr.since)}`
      : "";
    msg.hidden = !tr.on;
  }

  const facts = $("trading-facts");
  if (!facts) return;
  const rows = [];
  // The strategy, the instrument, the bar size and the endpoint are NOT listed here. Every one
  // of them is on screen already — the strategy bar names the strategy, the header summary
  // carries the instrument and the bar size, and the endpoint is the mode in the Mode box — so
  // the four rows were the panel restating its own context instead of reporting anything.
  // What is held, per account. Every account is LISTED, because something open in the account
  // this run is not pointed at is real and actionable whatever mode we are in, and it is what
  // stops the bot being armed on top of it — but only the traded account's FAULTS are reported:
  // a 401 on an account this run never touches is noise that reads like a fault, and its verdict
  // is already on the credential badge and in the Account popup.
  //
  // What each account is WORTH is a different question with a different answer: that panel, like
  // the log page's, shows the account being traded and no other, so an idle account's balance can
  // never be read as the one in play.
  const traded = String((exec && exec.env) || tr.env || "").toLowerCase();
  for (const account of d.positions || []) {
    const env = String(account.env || "").toLowerCase();
    for (const p of account.positions || []) {
      const qty = p.qty === undefined || p.qty === null ? "" : `${p.qty} `;
      rows.push(execRow(
        String(account.env || "").toUpperCase(),
        `${escapeHtml(p.symbol || "—")} · ${escapeHtml(qty + (p.side || ""))} · entry ${escapeHtml(p.avg_entry_price || "—")}`
      ));
    }
    if (account.known === false && (!traded || env === traded)) {
      rows.push(execRow(String(account.env || "").toUpperCase(), `<b>could not be read</b> — ${escapeHtml(account.reason || "")}`));
    }
  }
  facts.innerHTML = rows.join("");
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
    // A save can be partial: a credential pair the broker rejects is held back, and
    // the rest of the form still lands. So the feedback names what was left behind
    // instead of dressing the whole save as a failure — and a pair that merely could
    // not be reached was saved anyway, which the message says too.
    const checks = Object.entries(r.verifications || {});
    const unsaved = Object.entries(r.unsaved_pairs || {});
    const good = checks.filter(([, c]) => c.checked && c.ok);
    await loadAccount(true); // re-read so secrets re-mask and values refresh
    await loadTrading(); // the keys may have just changed, so re-resolve the target
    // The re-render wiped the rows, so repaint the verdicts THIS save produced —
    // they are answers to the save, which is a check someone asked for. Only the
    // pairs that were KEPT get one: a held-back pair is not what the row now holds.
    for (const [env, c] of good) {
      applyCredentialBadge(document.getElementById("cred-badge-" + env), c);
    }
    if (unsaved.length) {
      showAccountErrors(
        unsaved
          .map(([env, reason]) =>
            `${env.toUpperCase()} credentials are NOT valid (${reason}) — they were NOT saved. ` +
            "The rest of the form was saved; correct them and press Save again.")
          .join("\n"),
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

/* ---------- Every chart shows the same bars ----------
   The price chart, each indicator pane drawn separately and the backtest equity
   curve must show the SAME PERIOD at the same zoom: moving or zooming any of them
   moves all the others with it.

   They are synced by LOGICAL range (bar indices), not by time range. Two things a
   time range cannot do:

   * it cannot express the empty space before the first bar or after the last one.
     The library clamps such a request to the data, so dragging a pane past the end
     of its series left the price chart standing still ("the main chart stays
     unchanged"), and a price chart scrolled into whitespace clipped every pane;
   * it cannot express a range that no chart's data covers at all.

   Which BARS of the price chart each other chart is drawn on is DERIVED from the
   two time axes, never assumed: one entry per bar of the series, holding the price
   chart's index of that bar. A single offset is NOT enough, because a series is not
   always a shifted copy of the price series:

   * an indicator pane IS the price series minus a PREFIX (RSI drops its warm-up
     rows), which one offset describes exactly;
   * the equity curve the panel draws is a SUBSAMPLE of the run — the backtest
     service thins it to 600 points — and for most run lengths its final point is
     then appended on its own, because the last value must stay exact. Its bars are
     therefore 2, 2, 2, … price bars apart and finally 1: no constant offset, and
     not even a constant step. Requiring one is why the curve never joined the
     other charts.

   Listing every bar's position describes any monotonic series, including that one.
   A series is placed only when every one of its bars is found, in order, in the
   price chart's bars; otherwise it is left out of the sync rather than moved to
   the wrong bars. */
const _rangeMaps = new WeakMap(); // chart -> its bars' indices in the price chart
const _rangeTimes = new WeakMap(); // chart -> its bar times (until it is registered)
const _pushedRanges = new WeakMap(); // chart -> the range we just asked it for
// The price chart's own bars ARE the reference: its index is the index, whatever
// its bars turn out to be. A constant, so nothing can go stale — it is registered
// while the chart is still empty, before its bars are set.
const IDENTITY_BARS = { identity: true };

function _priceBarTimes() {
  return state.chartTimes || [];
}

// Where each bar of this series sits in the price chart's bars, or null when the
// series cannot be placed on them (then that chart is not synced at all).
function _barPlacement(times) {
  const main = _priceBarTimes();
  if (!times || times.length < 2 || main.length < 2) return null;
  const at = new Map();
  main.forEach((t, i) => { if (!at.has(t)) at.set(t, i); });
  const map = [];
  for (let i = 0; i < times.length; i++) {
    const index = at.get(times[i]);
    if (index === undefined) return null; // a bar the price chart does not have
    if (map.length && index <= map[map.length - 1]) return null; // out of order
    map.push(index);
  }
  return map;
}

// An index among the price chart's bars read as an index among this chart's own
// bars — and back again below. Between two neighbouring bars the two axes are
// linear, so a range is converted by locating the bars it falls between; beyond
// either end the nearest segment is extended, which is what keeps the empty space
// the user panned into part of the view.
function _ownIndexOf(map, priceIndex) {
  if (map.identity) return priceIndex;
  const n = map.length;
  if (n === 1) return 0;
  if (priceIndex <= map[0]) return (priceIndex - map[0]) / ((map[1] - map[0]) || 1);
  if (priceIndex >= map[n - 1]) {
    return (n - 1) + (priceIndex - map[n - 1]) / ((map[n - 1] - map[n - 2]) || 1);
  }
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (map[mid] <= priceIndex) lo = mid; else hi = mid;
  }
  return lo + (priceIndex - map[lo]) / ((map[hi] - map[lo]) || 1);
}

function _priceIndexOf(map, ownIndex) {
  if (map.identity) return ownIndex;
  const n = map.length;
  if (n === 1) return map[0];
  if (ownIndex <= 0) return map[0] + ownIndex * ((map[1] - map[0]) || 1);
  if (ownIndex >= n - 1) {
    return map[n - 1] + (ownIndex - (n - 1)) * ((map[n - 1] - map[n - 2]) || 1);
  }
  const lo = Math.floor(ownIndex);
  return map[lo] + (ownIndex - lo) * (map[lo + 1] - map[lo]);
}

// Join the sync: remember the mapping, then adopt the price chart's current view.
function registerRangeSync(chart, times) {
  if (!chart || !state.chart) return;
  const isPrice = chart === state.chart;
  // The price chart IS the reference: its own bar i is price bar i.
  const map = isPrice ? IDENTITY_BARS : _barPlacement(times || _rangeTimes.get(chart));
  if (!map) return;
  _rangeMaps.set(chart, map);
  chart.timeScale().subscribeVisibleLogicalRangeChange((r) => _onRangeChanged(chart, r));
  const adopt = () => {
    if (chart !== state.chart && !_rangeMaps.has(chart)) return; // disposed
    const main = state.chart && state.chart.timeScale().getVisibleLogicalRange();
    if (main) _mirrorRange(state.chart, main);
  };
  adopt();
  // ...and once more after the next frame: a freshly created chart is still
  // settling into its box, and a range applied against a width that then changes
  // comes out a few bars short of the price chart's.
  requestAnimationFrame(adopt);
}

// Push a range onto every other chart, converted into ITS indices.
function _mirrorRange(source, range) {
  const src = _rangeMaps.get(source);
  if (!src || !range) return;
  const from = _priceIndexOf(src, range.from);
  const to = _priceIndexOf(src, range.to);
  [state.chart].concat(_satelliteCharts()).forEach((other) => {
    if (!other || other === source) return;
    const map = _rangeMaps.get(other);
    if (!map) return;
    const moved = { from: _ownIndexOf(map, from), to: _ownIndexOf(map, to) };
    // A range the library cannot accept would throw inside the gesture that caused
    // it, so a chart that cannot express this range is left alone instead.
    if (!isFinite(moved.from) || !isFinite(moved.to)) return;
    _pushedRanges.set(other, moved);
    other.timeScale().setVisibleLogicalRange(moved);
  });
}

// Every chart that shares the price chart's view: the oscillator panes AND the
// backtest equity-curve sparkline (when a result is shown). Read lazily each time,
// so a just-created/removed satellite is picked up without re-subscribing.
function _satelliteCharts() {
  const list = (state.oscCharts || []).slice();
  if (btChart) list.push(btChart);
  return list;
}

function _onRangeChanged(chart, range) {
  if (!range) return;
  const pushed = _pushedRanges.get(chart);
  if (pushed && Math.abs(pushed.from - range.from) < 0.01
    && Math.abs(pushed.to - range.to) < 0.01) {
    _pushedRanges.delete(chart);
    return; // our own push coming back — never echo it onwards
  }
  // The price chart is always the master. A satellite drives the others only while
  // the user is working in it: its range also changes when the layout catches up
  // (its first paint, a resize), and that is not the user moving anything.
  if (chart !== state.chart && !_userTouchedPane(chart)) return;
  _mirrorRange(chart, range);
}

function subscribeMainToOscTime() {
  // Idempotent per chart instance: a rebuild makes a new chart, which subscribes once.
  if (!state.chart) return;
  _rangeMaps.set(state.chart, IDENTITY_BARS); // the reference itself
  const ts = state.chart.timeScale();
  ts.unsubscribeVisibleLogicalRangeChange(_mainRangeHandler);
  ts.subscribeVisibleLogicalRangeChange(_mainRangeHandler);
}

function _mainRangeHandler(range) {
  _onRangeChanged(state.chart, range);
}

// Marks a satellite (oscillator pane / equity curve) as user-driven for a moment,
// so its range changes are traced back to the gesture that caused them.
const _paneTouchedAt = new WeakMap();
const PANE_TOUCH_WINDOW_MS = 400;

function watchPaneInteraction(chart, el) {
  if (!chart || !el) return;
  const mark = () => _paneTouchedAt.set(chart, Date.now());
  ["wheel", "pointerdown", "pointermove", "touchstart", "touchmove"].forEach((type) =>
    el.addEventListener(type, mark, { passive: true })
  );
}

function _userTouchedPane(chart) {
  return Date.now() - (_paneTouchedAt.get(chart) || 0) <= PANE_TOUCH_WINDOW_MS;
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
        // The panes sit under the price chart and are range-synced to it, so their
        // axis must label the same bars the same way (see chart_time.js).
        timeScale: axisTimeScale({ borderColor: "#333a46", minBarSpacing: ChartZoom.MIN_BAR_SPACING }),
        localization: axisLocalization(),
        rightPriceScale: { borderColor: "#333a46" },
        // Same wheel policy as the main chart: a plain wheel pans, only a pinch
        // zooms, and ChartZoom stops it at this pane's own data.
        handleScroll: { mouseWheel: true },
        handleScale: {
          mouseWheel: false,
          axisPressedMouseMove: { time: false, price: true },
        },
        // Same free-floating crosshair as the main chart, so the synced horizontal line
        // is not snapped to a bar's extremes, and the same boxed time label under the
        // vertical line.
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      });
      watchPaneInteraction(chart, canvas); // gestures here drive the main chart
      // The pane's bar count is the UNION of its series' times: a pane's lines can
      // start at different points (RSI/ATR drop their warm-up rows, MACD draws a
      // histogram plus two lines), and the time axis holds all of them.
      const paneTimes = [];
      const paneSeen = new Set();
      o.lines.forEach((line) => (line.data || []).forEach((p) => {
        if (paneSeen.has(p.time)) return;
        paneSeen.add(p.time);
        paneTimes.push(p.time);
      }));
      paneTimes.sort();
      ChartZoom.bind(canvas, () => ({ chart: chart, barCount: paneTimes.length }));
      _rangeTimes.set(chart, paneTimes); // joined to the price chart's bars below
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

  // Let freshly created panes paint once at their natural width BEFORE we join them
  // to the price chart's view. A pane emits its own default (right-aligned) range
  // during first layout, and that emission must not be mistaken for the user moving
  // it — the touch guard in _onRangeChanged already ignores it, this just keeps the
  // adopted range from being the one the pane was about to abandon. The generation
  // token makes stale callbacks from an earlier toggle a no-op.
  const gen = ++_oscPaneGen;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (gen !== _oscPaneGen) return; // a newer drawOscPanes superseded us
      if (!state.chart) return;
      (state.oscCharts || []).forEach((oc) => registerRangeSync(oc));
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

// The one-way half of it, for when a panel has something to show. A collapse is a courtesy
// until it hides the thing you just asked for.
function expandCard(card) {
  if (!card) return;
  const body = card.querySelector(".collapse-body");
  const chev = card.querySelector(".card-toggle");
  if (body) body.hidden = false;
  if (chev) chev.textContent = "−";
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

/* How a listed gap is DRAWN, one entry per reason the data layer reports
   (``src/data/delta.py``, REASON_*). Three kinds of absence that are NOT interchangeable:

     fetchable    the provider HAS this bar and the file does not — press Fetch bars.
     no_trades    the provider traded that day but has no bar for this interval, so
                  nobody traded in it. Nothing to fetch, ever, and nothing to wait for.
     unconfirmed  a normal slot of a session we hold bars for, and no provider check has
                  answered for it yet.

   All three are LISTED — an interval with no trades is a real feature of the symbol, and
   an operator looking at holes in a chart needs to know which holes are theirs to fix —
   and each is drawn differently, so two of them can never be read as the same thing.
   `tests/test_web/test_delta_reasons.py` runs this table in node. */
const DELTA_CHIP = {
  fetchable: {
    cls: "chip date-chip",
    title: "the provider has this bar and the dataset does not — Fetch bars will add it",
  },
  no_trades: {
    cls: "chip date-chip no-trades",
    title:
      "no trades in this interval — the provider has no bar for it either, so there is " +
      "nothing to fetch",
  },
  unconfirmed: {
    cls: "chip date-chip inferred",
    title: "a normal slot of this session's grid, and no provider check has confirmed it yet",
  },
};

// An unknown reason is treated as the cautious one: unconfirmed still blocks a run, so a
// payload from a newer server cannot silently look harmless.
function deltaChip(reason) {
  return DELTA_CHIP[reason] || DELTA_CHIP.unconfirmed;
}

/* Is there anything for the hide-toggle to hide? Drawn only when there is: an empty
   control is a question with no subject. */
function showsNoTradeToggle(s) {
  return ((s && s.no_trades_bars_total) || 0) > 0;
}

/* The listed gaps the panel actually draws. Only the no-trades rows are ever hidden, and
   only when the operator asked: a fetchable bar is a to-do, and a view toggle must never
   be able to scroll it off the list. */
function visibleDeltaBars(s, hideNoTrades) {
  const bars = (s && s.missing_bars) || [];
  return bars.filter((b) => !(hideNoTrades && b.reason === "no_trades"));
}

/* How many untraded intervals the toggle is keeping out of the list right now — the
   payload's TOTAL, not the number of them the capped list happens to name. The toggle
   hides all 959 of IMCC's untraded bars even though only 500 fit in the list, and a
   count of 500 printed beside a toggle that says 959 is just wrong. */
function hiddenNoTradeCount(s, hideNoTrades) {
  return hideNoTrades ? (s && s.no_trades_bars_total) || 0 : 0;
}

/* The toggle's label carries the COUNT — "Hide all 959 bars missing due to no liquidity"
   — and flips to "Show all …" once they are out of the way, because the control now
   describes the way back. The number is the reason to press it: on a thin symbol the
   handful of bars that can be fetched is what is left once these are hidden. */
function noTradeToggleLabel(n, hidden) {
  const what = `${n.toLocaleString("en-US")} ${barWord(n)}`;
  return hidden
    ? `Show all ${what} missing due to no liquidity`
    : `Hide all ${what} missing due to no liquidity`;
}

/* The toggle itself, in its own row above the list it filters. */
function deltaToggleHtml(noTrades) {
  return (
    `<label class="delta-toggle" title="Intervals nobody traded in — the provider has ` +
    `no bar for them, so no fetch can produce one. Hiding them leaves only the bars that ` +
    `CAN still be fetched.">` +
    `<input type="checkbox" class="switch" id="hide-no-trades"` +
    `${state.hideNoTrades ? " checked" : ""} ` +
    `onchange="toggleNoTrades(this.checked)">` +
    `<span>${noTradeToggleLabel(noTrades, state.hideNoTrades)}</span></label>`
  );
}

function toggleNoTrades(on) {
  state.hideNoTrades = !!on;
  if (state.deltaStatus) renderDelta(state.deltaStatus);
}

/* The one line of prose above the list. It has to be ONE line — the panel is read at a
   glance and the chips already say which bar is which kind — and it says only what the
   reader is looking at right now. Max two short sentences, pinned by
   tests/test_web/test_delta_reasons.py. ``hidden`` is passed in, not counted off the
   list, so the number in the note is the same one the toggle's label shows. */
function deltaNoteText(s, shown, hidden) {
  const listed = (s && s.missing_bars) || [];
  const blocking =
    s && s.missing_bars_total != null ? s.missing_bars_total : listed.length;
  const noTrades = (s && s.no_trades_bars_total) || 0;
  const unconfirmed = shown.filter((b) => b.reason === "unconfirmed").length;

  let note = "";
  if (shown.length && blocking && noTrades) {
    note = "Struck chips have no trades anywhere; ⬇ Fetch bars adds the rest.";
  } else if (shown.length && blocking) {
    note =
      unconfirmed === blocking
        ? "Not checked against the provider yet — ⬇ Fetch bars retries."
        : "The provider has these — ⬇ Fetch bars adds them.";
  }
  // A list of nothing but untraded chips gets no line at all: the struck chips and the
  // "✓ Nothing to fetch" headline above already say it.
  if (hidden && shown.length) note += ` (${hidden.toLocaleString()} hidden)`;
  return note;
}

function renderDelta(s) {
  const body = $("delta-body");
  if (!body) return;
  // Remembered so the hide-toggle can redraw this panel from the payload already in hand.
  state.deltaStatus = s;
  setDeltaAction(""); // header action depends on the state below
  // Remember what the fetch is measured against (see syncDelta).
  state.deltaRows = typeof s.rows === "number" ? s.rows : null;

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

  const bars = s.missing_bars || [];
  const blocking = s.missing_bars_total != null ? s.missing_bars_total : bars.length;
  const noTrades = s.no_trades_bars_total || 0;
  const shown = visibleDeltaBars(s, state.hideNoTrades);
  // Against the payload's total, never the capped list — see hiddenNoTradeCount.
  const hiddenNoTrades = hiddenNoTradeCount(s, state.hideNoTrades);

  if (s.synced && !noTrades) {
    const rows = (s.recent || [])
      .map(
        (r) =>
          `<tr><td>${r.datetime || r.date}</td><td>${r.open.toFixed(2)}</td><td>${r.high.toFixed(2)}</td>` +
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
          <thead><tr><th>Date / time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="6" class="muted">—</td></tr>`}</tbody>
        </table>
      </div>`;
    return;
  }

  // A missing DAY and a missing BAR are different sizes of the same problem, so the
  // count that leads is the bar count: a session short by one hour has no missing day
  // at all and would otherwise read "✓ synced" while being incomplete. The bars
  // themselves are listed one per row of the grid — grouped by day so a 390-bar 1m hole
  // stays readable — because "3 days missing" does not tell you WHICH bars, and that is
  // the question the panel is being asked.
  const days = Array.from(new Set(s.missing || []));
  const byDay = new Map();
  shown.forEach((b) => {
    if (!byDay.has(b.date)) byDay.set(b.date, []);
    byDay.get(b.date).push(b);
  });

  const dayBlocks = Array.from(byDay.entries())
    .map(([day, rows]) => {
      const chips = rows
        .map((b) => {
          // The stock clock alone for an intraday grid; a daily bar has no time and
          // says so with its date, which is already the block heading.
          const label = b.time || "bar";
          const spec = deltaChip(b.reason);
          return `<span class="${spec.cls}" title="${escapeHtml(spec.title)}">${escapeHtml(label)}</span>`;
        })
        .join("");
      const held = rows.filter((b) => b.reason !== "no_trades").length;
      const note = held
        ? `${held} ${barWord(held)} missing`
        : `no trades in ${rows.length} ${barWord(rows.length)}`;
      return `<div class="delta-day">
          <div class="delta-day-head"><b>${escapeHtml(day)}</b>
            <span class="muted">${note}</span></div>
          <div class="missing-chips">${chips}</div>
        </div>`;
    })
    .join("");

  const more = blocking > 0 && blocking + noTrades > bars.length
    ? `<p class="muted">Showing ${bars.length.toLocaleString()} of ${(blocking + noTrades).toLocaleString()} listed gaps.</p>`
    : "";
  const dayLine = days.length
    ? `<span class="muted">${days.length} whole ${days.length === 1 ? "session" : "sessions"} absent.</span>`
    : "";
  const headline = blocking
    ? `<span class="delta-badge warn">⚠ ${blocking.toLocaleString()} ${barWord(blocking)} missing</span>`
    : `<span class="delta-badge ok">✓ Nothing to fetch</span>`;
  const note = deltaNoteText(s, shown, hiddenNoTrades);

  body.innerHTML = `
    <div class="delta-head">
      ${headline}
      <span class="muted">through ${s.last_date}</span>
      ${dayLine}
    </div>
    ${note ? `<p class="muted">${note}</p>` : ""}
    ${
      showsNoTradeToggle(s)
        ? `<div class="delta-toggle-row">${deltaToggleHtml(noTrades)}</div>`
        : ""
    }
    ${
      dayBlocks ||
      `<p class="muted">${
        hiddenNoTrades
          ? `All ${hiddenNoTrades.toLocaleString()} intervals with no trades are hidden by the toggle above.`
          : "No individual bars identified."
      }</p>`
    }
    ${more}
    <p id="delta-msg" class="muted"></p>`;
  setDeltaAction(
    blocking
      ? `<button id="sync-delta-btn" class="primary small" onclick="syncDelta()">⬇ Fetch bars</button>`
      : `<button id="sync-delta-btn" class="small" onclick="syncDelta()">⬇ Re-check</button>`
  );
}

// Did a delta fetch actually change the dataset?
//
// Everything drawn FROM the dataset — the price chart, its indicator panes, the data
// table, the signal series, the position bands — is read once when the page loads, so
// this is the question that decides whether a reload is owed.
//
// It is deliberately NOT "did the fetch come back synced". A fetch that pulls in
// today's bars while an older gap remains (the usual case: only the provider can
// settle a gap and it often cannot) has changed the dataset and must redraw the chart,
// but is still not "synced". Gating on `synced` left the chart showing the old window
// in exactly that case. Unknown on either side reloads, which is the safe default.
function fetchChangedDataset(before, after) {
  return before == null || after == null || after !== before;
}

async function syncDelta() {
  const btn = $("sync-delta-btn");
  const msg = $("delta-msg");
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = "Fetching bars…";
  const rowsBefore = state.deltaRows;
  try {
    const s = await api("/api/v1/delta/sync", { method: "POST" });
    renderDelta(s);
    // Dataset currency drives the Run-backtest gate — refresh it after a sync.
    btDelta = s;
    renderBacktest(btPayload, s);
    if (fetchChangedDataset(rowsBefore, s.rows)) refresh();
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

/* ---------- Live: what the loop is doing, and is the position protected ----------
   Two reads with different costs, so they are loaded differently. /api/v1/loop is
   local files and is refreshed with everything else; /api/v1/orders asks ALPACA, so it
   is fetched when the panel is opened or refreshed by hand — the dashboard must not
   generate broker traffic merely by being open. */
// The loop is a SEPARATE process and this dashboard never starts one — that is the whole
// point of the two-process split. So "armed" and "trading" are two different facts, and only
// one of them is a switch. The two are joined below in a visible LINE, not in a `title` — the
// embedded browser renders no native tooltip, which is how this stayed invisible.
const LOOP_COMMAND = "python -m src.main";
const LOOP_LOG = "data/loop.log";

function shortAge(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* How much is OPEN, and the other two trading boxes, now live in ``trading_switch.js``: the log
 * page shows the same three, and one box with two implementations is how the two screens start
 * disagreeing about what "open" means. This page hands over the payload and its own handler names. */

function showLiveError(message) {
  const state_ = $("live-state");
  if (state_) state_.textContent = message;
}

// The panel polls; the page does not. Two cadences, because the two halves cost very
// different amounts: the loop's records are local files, while the orders and the clock are
// broker calls. And it stops the moment nobody is looking — a collapsed panel or a
// backgrounded tab asking Alpaca every minute is a recurring cost with no reader.
const LIVE_POLL_MS = 5000;      // /loop — files only
const LIVE_SLOW_MS = 60000;     // /orders and /clock — broker calls
const LIVE_BACKOFF_MS = 30000;  // after a failure: slow down rather than hammer
const LIVE_MAX_FAILURES = 5;

const _livePoll = { timer: null, lastSlow: 0, failures: 0, token: 0 };

// Alpaca's clock returns the EXCHANGE's own wall time with its offset (…-04:00), so the 09:30
// in it is 09:30 in New York whatever this machine is set to. Reading the digits directly
// avoids converting it into the operator's zone and then calling the result the market's.
function exchangeClock(iso) {
  const parts = /(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ""));
  if (!parts) return "";
  const et = `${parts[4]}:${parts[5]}`;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return `${et} ET`;
  const local = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  // Only worth saying when the two differ: on a machine already set to New York, "09:30 ET
  // (09:30 here)" is noise.
  return local === et ? `${et} ET` : `${et} ET (${local} here)`;
}

function untilWhen(iso) {
  if (!iso) return "";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const minutes = Math.round((when.getTime() - Date.now()) / 60000);
  if (minutes <= 0) return "";
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return rest ? `in ${hours}h ${rest}m` : `in ${hours}h`;
}

// The answer to "do I need to come back, and when?". Shown whether or not a loop has ever
// run, which is the state a first test starts in — and deliberately silent rather than
// guessing when the clock could not be read.
function marketLine(market) {
  if (!market) return "";
  if (!market.ok || market.is_open === null || market.is_open === undefined) {
    return `<span class="warn">Exchange unknown — ${escapeHtml(market.message || "the clock could not be read")}</span>`;
  }
  const when = market.is_open ? market.next_close : market.next_open;
  const verb = market.is_open ? "closes" : "opens";
  const state = market.is_open ? "open" : "closed";
  return [`Exchange <b>${state}</b> — ${verb}`,
    escapeHtml(exchangeClock(when)), escapeHtml(untilWhen(when))].filter(Boolean).join(" ");
}

// Money, as the account block needs it. ``signed`` is for the day's change, where the
// difference between "+$0.00" and "-$0.00" is the whole point of the line.
function money(value, signed) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const body = Math.abs(number).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  return `${number < 0 ? "-" : (signed && number > 0 ? "+" : "")}$${body}`;
}

function signedPercent(value) {
  if (value === null || value === undefined) return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function livePanelVisible() {
  // The panel is always open now — it has no toggle of its own — so the only thing that can hide
  // it is the strategy bar's toggle, which folds the whole row below the strategy line. A hidden
  // panel is the same case as a collapsed one: no reader, so no broker traffic.
  const row = $("strategy-body");
  return !!$("live-body") && !(row && row.hidden) && !document.hidden;
}

function stopLivePoll() {
  if (_livePoll.timer) { clearTimeout(_livePoll.timer); _livePoll.timer = null; }
}

// Self-scheduling rather than setInterval: the next poll is queued only once this one has
// settled, so a slow broker makes the panel slower instead of stacking up requests.
function scheduleLivePoll(delay) {
  stopLivePoll();
  if (!livePanelVisible()) return;
  _livePoll.timer = setTimeout(runLivePoll, delay === undefined ? LIVE_POLL_MS : delay);
}

/* Take the BROKER half of the panel NOW, rather than waiting for the slow poll to come round.
 *
 * For the moments when what the panel is about has just changed under it — the mode switch to the
 * other account, a row that was folded open again, a tab coming back — because then every box on
 * screen is answering for the account or the moment we just left, and sitting on the old numbers
 * for up to a minute is the thing this panel exists to prevent. The slow cadence restarts from
 * this read, so the next scheduled one is a full interval away rather than immediately after. */
async function refreshLiveNow() {
  await loadLive(true);
  _livePoll.lastSlow = Date.now();
  scheduleLivePoll();
}

async function runLivePoll() {
  _livePoll.timer = null;
  if (!livePanelVisible()) return;   // collapsed or backgrounded since it was scheduled
  const slow = Date.now() - _livePoll.lastSlow >= LIVE_SLOW_MS;
  if (slow) _livePoll.lastSlow = Date.now();
  const ok = await loadLive(slow, { quiet: true });
  _livePoll.failures = ok ? 0 : _livePoll.failures + 1;
  if (_livePoll.failures >= LIVE_MAX_FAILURES) return;  // the operator can ask again
  scheduleLivePoll(_livePoll.failures ? LIVE_BACKOFF_MS : LIVE_POLL_MS);
}

// Only write when the text actually changed. Re-rendering an unchanged panel every few
// seconds is invisible except in the ways it is not: it resets a blinking chip mid-blink and
// makes the relative ages flicker as you read them.
function setIfChanged(el, html) {
  if (el && el.innerHTML !== html) el.innerHTML = html;
}

async function loadLive(includeOrders, options) {
  const quiet = !!(options && options.quiet);
  // A poll and a ↻ can overlap. Only the newest render wins, so a slow response cannot land
  // on top of a fresher one and show the panel going backwards.
  const generation = ++_livePoll.token;
  let loop = null;
  try {
    loop = await api("/api/v1/loop");
  } catch (err) {
    if (!quiet) showLiveError(`could not read the loop's records: ${err.message}`);
    return false;
  }
  let orders = null;
  let market = null;
  let accounts = null;
  if (includeOrders) {
    try {
      orders = await api("/api/v1/orders");
    } catch (err) {
      orders = { ok: false, message: err.message };
    }
    try {
      market = await api("/api/v1/clock");
    } catch (err) {
      market = { ok: false, message: err.message };
    }
    try {
      accounts = await api("/api/v1/accounts");
    } catch (err) {
      accounts = { ok: false, message: err.message, accounts: [] };
    }
  }
  if (generation !== _livePoll.token) return false;
  renderLive(loop, orders, market, accounts);
  return true;
}

// The account the Trading panel is about: the switch's own read (`/api/v1/execution/status`,
// loaded with the header combo box) first, and only then the accounts payload, which arrives on
// the slower cadence. Empty when neither has been read yet, and empty is a real answer — it means
// "do not mark anything as this account's", not "assume paper".
function inPlayEnv() {
  return String(
    (state.executionStatus && state.executionStatus.env)
    || (state.liveAccounts && state.liveAccounts.env)
    || ""
  ).toLowerCase();
}

function renderLive(loop, orders, market, accounts) {
  const armed = !!(state.tradingState && state.tradingState.on);

  // The broker half is kept across the fast polls, which fetch none of it: without this the
  // 5-second loop read would re-render the panel with no orders, blanking the tiles that come
  // from Alpaca and replacing the protection verdict with "open the panel" until the next
  // slow poll put them back. It flapped, once a poll.
  if (orders) state.liveOrders = orders;
  if (market) state.liveMarket = market;
  if (accounts) {
    // A failed re-read is NOT an account worth zero. Overwriting a good snapshot with the empty
    // list a failure carries blanked every account box until the next slow poll happened to
    // succeed, and from the outside that reads as "the boxes are gone" rather than "that read
    // failed". So only a GOOD read is stored, and the last one stands until one lands.
    if (accounts.ok !== false) state.liveAccounts = accounts;
  }

  // The account the panel is about, so a line taken from the loop's own records can say when it
  // belongs to the other one.
  const inPlay = inPlayEnv();

  const lines = [];
  // The strategy and the account are NOT repeated here: this panel sits inside the strategy bar,
  // which already names the strategy, and the Mode box in the metrics below names the account.
  // Printing both again was the panel's longest line saying nothing it did not say elsewhere.
  //
  // Neither is the HOLDER — "pid 23710 on Kalins-MacBook-Pro.local (strategy 'GPRO'), started
  // 2026-09-20T21:46:11.587122+00:00" is a debugging string, and on screen it pushed the one
  // fact worth reading ("when does it wake next") off the end of the line. Which process holds
  // the claim is in the log and in the lease file; what the operator needs here is whether
  // anything is running, and when it will run again.
  if (loop.state === "overdue") {
    lines.push('<span class="bad">The process holding the loop is gone — nothing will tick</span>');
  } else if (loop.state === "running") {
    const wake = loop.next_wake ? shortWhen(loop.next_wake) : "";
    lines.push(wake ? `Next wake ${escapeHtml(wake)}` : "Running");
  }
  // Arming writes trading.json; nothing ticks until a process runs the loop. Saying nothing
  // here is how "trading is ON" became a promise this screen could not keep: the operator
  // flips the switch, the chip keeps saying "stopped", and the reasonable reading is that the
  // switch did nothing.
  // Arming STARTS the loop (``src/web/services/loop_control``), so this is no longer "you forgot
  // to start it" — it means the start failed, or the loop started and exited, and either way
  // the reason is in the loop's own log. Naming the file is the difference between a dead end
  // and a next step. It covers both cases on purpose: a loop that exits at once (a refusal in
  // the tick, code older than the files on disk) is not a start that failed, and claiming it
  // was would send the operator looking in the wrong place.
  if (armed && (loop.state === "stopped" || loop.state === "never")) {
    lines.push(
      `<span class="warn">Trading is armed, but no loop is running — nothing will tick. `
      + `See <code>${escapeHtml(LOOP_LOG)}</code>; start one with `
      + `<code>${escapeHtml(LOOP_COMMAND)}</code></span>`
    );
  }
  if (loop.has_run) {
    const action = loop.last_action || "?";
    // A tick whose verdict came from the SWITCH itself ("off — trading is OFF") does not repeat
    // the switch: the Trading box in the metrics below says ON or OFF, and the reason for the
    // verdict is the same two words. Every other reason is a fact about the bar and stays.
    const fromSwitch = loop.last_tick ? loop.last_tick.stage === "switch" : action === "off";
    const reason = loop.last_reason && !fromSwitch ? ` — ${escapeHtml(loop.last_reason)}` : "";
    // The tick and the exchange share ONE row: they answer the same question ("is anything
    // happening, and can it?"), and as two stacked lines they read as two unrelated facts with
    // the bar's timestamp wedged between them.
    const bar = loop.last_tick && loop.last_tick.bar ? ` · bar ${escapeHtml(loop.last_tick.bar)}` : "";
    // This one line is the loop's own record, and the loop writes it for ONE environment: after
    // a mode switch it is the account we just left that ticked last. Naming that account is what
    // keeps the line from being read as this account's — the same job the log page's tick table
    // does with its `account` column.
    const tickEnv = String((loop.last_tick && loop.last_tick.env) || "").toLowerCase();
    const belongsTo = !tickEnv || !inPlay || tickEnv === inPlay;
    const whose = belongsTo ? "" : ` <span class="muted">(${escapeHtml(tickEnv)} account)</span>`;
    lines.push(`Last tick <b>${escapeHtml(action)}</b>${reason} (${shortAge(loop.last_tick_age_seconds)})${bar}${whose}`);
  } else {
    lines.push("No tick recorded yet");
  }
  const marketText = marketLine(state.liveMarket);
  if (marketText) {
    // Onto the LAST line, which is the tick line whenever there is one — the guard is for the
    // case where every line above was skipped, so the exchange still gets said.
    if (lines.length) lines[lines.length - 1] += ` · ${marketText}`;
    else lines.push(marketText);
  }
  if (loop.last_refusal && loop.last_refusal.reason) {
    lines.push(`<span class="warn">last refusal: ${escapeHtml(loop.last_refusal.reason)}</span>`);
  }
  // A refusal can still have booked a trade, so what the last tick CLOSED is worth a line.
  const closed = (loop.last_tick && loop.last_tick.trades) || [];
  for (const trade of closed) {
    lines.push(`closed ${escapeHtml(trade.direction || "position")} at ${escapeHtml(trade.exit_price)} (${escapeHtml(trade.reason || "")})`);
  }
  setIfChanged($("live-state"), lines.map((line) => `<div>${line}</div>`).join(""));

  // From the last read, not from this render's argument: see above.
  renderProtection(state.liveOrders ? state.liveOrders.protection : null);
  renderLiveDetail(loop, state.liveOrders);
}

function renderProtection(verdict) {
  const host = $("live-protection");
  if (!host) return;
  if (!verdict) {
    host.className = "muted";
    host.textContent = "Resting exits not read yet (asks Alpaca).";
    return;
  }
  host.className = verdict.state === "unprotected" ? "bad" : "muted";
  if (verdict.state === "none") {
    host.textContent = "Flat — nothing to protect.";
    return;
  }
  if (verdict.state === "protected") {
    const levels = verdict.levels || {};
    const bits = Object.entries(levels)
      .filter(([, v]) => v && v.wanted)
      .map(([kind, v]) => `${kind} ${Number(v.wanted).toFixed(2)}`);
    host.textContent = `Protected: ${bits.join(", ")} — resting at the broker.`;
    return;
  }
  if (verdict.state === "naked") {
    host.textContent = verdict.message;
    host.className = "muted";
    return;
  }
  host.innerHTML = `<b>⚠ ${escapeHtml(verdict.message)}</b><br>`
    + "A level was set and no order is resting at it — check the broker.";
}

// One number in a box, the same `.bt-stat` the backtest KPIs and the report page use — the box
// itself is built by the shared module, which the log page uses too, so a dashboard box and a log
// box cannot drift apart. Everything this page puts in one is its own: the numbers, the account's
// figures, the counts.
const liveTile = TraiderSwitch.tile;

function renderLiveDetail(loop, orders) {
  const host = $("live-metrics");
  if (!host) return;
  const tiles = [];

  // -- the two settings the rest is read through --------------------------
  // FIRST, because they are the frame: which account an order would go to, and whether any are
  // being sent at all. Everything below is only as safe as these two.
  //
  // Both pulse when they are set the dangerous way — a live account, and trading armed — for the
  // reason the header's dots blink for those two states and no others: this dashboard is left
  // open, and these are the two settings a glance has to catch. The boxes re-render only when
  // their TEXT changes (``setIfChanged``), so the pulse runs without restarting on every poll.
  // The mode comes from the switch's own read and only falls back to the accounts payload: the
  // accounts are read on the slow poll, and "—" for a minute after opening the page is a box that
  // has stopped saying anything.
  //
  // Both are PRESSABLE, and each calls the handler the header's control for that state calls: the
  // mode box flips to the other account, the trading box flips the master switch. Acting from
  // where the state is read is the point — the box is what the operator is looking at when they
  // decide to change it.
  // The three trading boxes are built by the shared module (``trading_switch.js``), because the log
  // page shows the same three: the mode, the switch, and what is open. Each is handed the name of
  // this page's own handler for it.
  //
  // Trading ON freezes the MODE with the rest of the configuration. The switch itself is never
  // locked: stopping has to stay reachable.
  const locked = !!state.tradingLocked;
  const mode = inPlayEnv();
  tiles.push(TraiderSwitch.envTile(mode, locked, "onModeBoxClick()"));
  tiles.push(TraiderSwitch.tradeTile(state.tradingPayload, "toggleTrading()"));
  tiles.push(TraiderSwitch.openTile(state.tradingPayload, mode));

  // -- the account being traded -------------------------------------------
  // The one the panel is about, so its numbers come first. An account that cannot be read draws
  // no boxes rather than a row of dashes: an unreadable account is not a balance of zero.
  const accounts = state.liveAccounts || {};
  // A snapshot belongs to the account the panel was about when it was READ. Switch modes and it
  // is the account we just left — drawing it under the new mode is precisely the mix-up this
  // panel exists to prevent, and it is what a switch used to show for the second or so before
  // the broker answered. So a snapshot that is not about the account in play is not drawn at all;
  // it is kept, and becomes the right one again the moment the read for this mode lands.
  const staleSnapshot = !!(accounts.env && mode && accounts.env !== mode);
  const active = staleSnapshot
    ? null
    : ((accounts.accounts || []).find((row) => row.env === accounts.env) || null);
  if (active && active.known) {
    const change = active.day_pl === null || active.day_pl === undefined ? null : Number(active.day_pl);
    tiles.push(liveTile("Account",
      `${escapeHtml(active.env || "")}${active.account ? ` ${escapeHtml(active.account)}` : ""}`,
      active.blocked ? "neg" : "",
      active.blocked
        ? "the broker is refusing orders"
        : `status ${active.status || "unknown"}`));
    tiles.push(liveTile("Equity", escapeHtml(money(active.equity))));
    tiles.push(liveTile("Day",
      `${escapeHtml(money(active.day_pl, true))}${signedPercent(active.day_pl_pct) ? ` (${escapeHtml(signedPercent(active.day_pl_pct))})` : ""}`,
      change === null ? "" : (change < 0 ? "neg" : (change > 0 ? "pos" : "")),
      change === null
        ? "no previous close"
        : "equity vs the previous close; includes what is open"));
    tiles.push(liveTile("Cash", escapeHtml(money(active.cash))));
    tiles.push(liveTile("Buying power", escapeHtml(money(active.buying_power)),
      "", active.multiplier ? `${Number(active.multiplier)}× cash, at the broker` : ""));
    // The account's standing at the broker, as its own box rather than as the tooltip on the
    // Account box: "ACTIVE" or a block is a fact about whether anything can be sent at all, and
    // it is read at a glance next to the numbers it explains.
    tiles.push(liveTile("Status", escapeHtml(active.status || "—"),
      active.blocked ? "neg" : "",
      active.blocked ? "the broker is refusing orders" : "the account's standing at the broker"));
  }

  // -- what is held, and what protects it ---------------------------------
  // There is no position box: it said "flat" or the same thing the Open box says, from the loop's
  // own record rather than the broker's — two readings of one question, in two vocabularies. What
  // is held is the Open box's count, and the direction and entry price, when there is one, are in
  // the facts grid below with the account's own name against them.
  if (orders && orders.ok !== false) {
    tiles.push(liveTile("Working orders", String((orders.open || []).length),
      "", "orders still at the broker: an unfilled entry, or a bracket parent"));
    tiles.push(liveTile("Exit legs", String((orders.resting || []).length),
      "", "stop and limit legs at the broker — these close the position"));
    const newest = (orders.closed || [])[0];
    if (newest) {
      tiles.push(liveTile("Last fill",
        `${escapeHtml(newest.filled_qty || newest.qty || "")} @ ${escapeHtml(newest.filled_avg_price || "—")}`,
        "", `${newest.side || ""} · ${newest.status || ""}`.trim()));
    }
  }

  tiles.push(liveTile("Trades closed", String(((loop.last_tick && loop.last_tick.trades) || []).length),
    "", "round trips finished"));

  // The OTHER environment is deliberately not reported here. This panel is about the account
  // being TRADED (``accounts.env``), and a 401 on the one this run never touches is noise that
  // reads like a fault: it is on the credential badge and in the Account popup. Its balance is
  // shown nowhere at all — an idle account's equity presented beside the traded one's is a number
  // waiting to be read as the wrong account's, which is exactly what the log page did when the
  // switch moved to live with no live keys behind it. A position in the other account is not lost
  // either — it is listed above, with the account names against it, and it refuses an arming,
  // with the flatten button, in the switch panel.

  setIfChanged(host, tiles.join(""));
}

/* The strategy bar's toggle: it folds everything below the strategy line — the trading panel and
 * the signals/rules section together. ONE toggle for one subject, which is why the trading panel
 * no longer has one of its own: two buttons hiding the same panel from different places is how a
 * page ends up with a section nobody can find.
 *
 * Opening it is what asks the broker: the trading panel is the only thing here that wants Alpaca's
 * order list, so it pays for it rather than the page load doing so — and folding it away stops the
 * polling, because a hidden panel has no reader. */
function toggleStrategyBody() {
  const row = $("strategy-body");
  const btn = $("strategy-collapse");
  if (!row) return;
  row.hidden = !row.hidden;
  if (btn) {
    btn.textContent = row.hidden ? "+" : "−";
    btn.title = row.hidden ? "Expand the panels below" : "Collapse the panels below";
  }
  if (!row.hidden) {
    // Unfolded: whatever it was showing is as old as the time it was shut, so take the full
    // read now — and this is also what starts the poll for a page that had it folded.
    refreshLiveNow();
  } else {
    stopLivePoll();
  }
}

// A backgrounded tab is the same case as a folded row: no reader, so no polling. On the
// way back the panel is refreshed at once rather than waiting out the interval.
function handleLiveVisibility() {
  if (livePanelVisible()) {
    refreshLiveNow();
  } else {
    stopLivePoll();
  }
}

document.addEventListener("visibilitychange", handleLiveVisibility);
window.addEventListener("pagehide", stopLivePoll);

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
  ["rules-add-buy", "rules-add-sell", "rules-copy", "save-rules", "save-pconfig", "save-risk"].forEach((id) => {
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
    renderStrategyRisk();
    renderStrategyBar();
    // NO line about where the rules are stored. A load that succeeded has nothing to
    // report — "Editing <path>" is a description of the file, it said the same thing on
    // every single load, and it was the first thing under the header. What stays is the
    // one case that is NOT static news: a store that could not be read, which would
    // otherwise fail silently and leave the panel looking empty-but-fine.
    setMsg(d.error ? `⚠ ${d.error} — no file written.` : "");
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
      ? names.map((n) => _optHtml(n, strategyOptionLabel(n, (p.strategies || {})[n]), p.active)).join("")
      : `<option value="">— none —</option>`;
    sel.innerHTML = opts;
    sel.value = p.active || "";
  }
  if (delBtn) delBtn.disabled = !p || !p.active || !!state.tradingLocked;
  if (renameBtn) renameBtn.disabled = !p || !p.active || !!state.tradingLocked;
  applyStrategyAddState(!!state.tradingLocked); // busy OR limit OR trading lock

  const rs = p && p.strategies ? p.strategies[p.active] : null;

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
      // Which side the rule is, so the stylesheet can tell the two groups apart. They are read as
      // two lists — BUY first, then SELL — and with only the line height between them the first
      // SELL rule reads as more of the BUY list. Same attribute the Rules panel uses on its cards.
      if (r.side) line.dataset.side = r.side;
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

  // "Copy rules from" is available exactly when there is another strategy to copy from,
  // and never while trading is on (see setActionButtonsDisabled for the same rule).
  updateCopyButton();
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
  wireDependentOptions(groups, prefix);
}

/* ---------- one field's value space depending on another ----------
   The historical period is not independent of the bar size: a finer candle
   cannot reach as far back before the provider stops serving it, so each bar
   size offers its own periods. The schema ships both the list for the CURRENT
   bar size and the whole table, so the dropdown can be repopulated here the
   moment the bar size changes — no round trip, and the panel can never show a
   pair the rule forbids.

   The list alone says what is allowed, so nothing else is written beside it: a
   stored value the new bar size cannot use moves the selection onto the first
   allowed period, which is visible in the control itself. */
function wireDependentOptions(groups, prefix) {
  groups.forEach((group) => {
    (group.fields || []).forEach((f) => {
      if (!f.options_by || !f.depends_on) return;
      const controller = document.getElementById(prefix + "-" + f.depends_on);
      const select = document.getElementById(prefix + "-" + f.key);
      if (!controller || !select) return;

      const apply = () => {
        const options = f.options_by[controller.value] || f.options || [];
        if (!options.length) return;
        const wanted = String(select.value);
        select.innerHTML = options
          .map((o) => _optHtml(o.value, o.label, wanted))
          .join("");
        if (!options.some((o) => String(o.value) === wanted)) {
          select.value = options[0].value;
        }
      };
      controller.addEventListener("change", apply);
      apply();
    });
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

/* A strategy's name plus the two settings that decide WHAT it reads: the history window and the
 * bar size. The choice between two strategies is really a choice between two datasets, and the
 * name is only the author's shorthand for them — "AAPL - 1m - 5d" is in fact configured to read
 * 15 days, so the list says what each one reads rather than what it was called.
 *
 * This replaces the chip that used to sit beside the combo box: it described the ACTIVE strategy,
 * which is the one case where the two values are already on screen in the panels below. In the
 * dropdown they are what the choice is about, and they are there before the click, not after. */
function strategyOptionLabel(name, rs) {
  const cfg = (rs && rs.config) || {};
  const window_ = cfg.HISTORICAL_LOOKBACK || "";
  const bar = cfg.HISTORICAL_BAR_SIZE || "";
  const bits = [periodLabel(window_), barSizeLabel(bar)].filter((bit) => bit && bit !== "—");
  return bits.length ? `${name} · ${bits.join(" · ")}` : name;
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

/* ---------- Copy rules from another strategy ----------
   Building the same rulebook twice is the normal case: a second strategy on another
   symbol usually wants the first one's conditions as a starting point. So the panel can
   take EVERY rule of another strategy — enabled and disabled alike — and append them to
   this one's.

   It is a local edit like `+ Buy`/`+ Sell`, not a server operation: the rules land in the
   panel's in-memory copy of the active strategy and are persisted by 💾 Save, which is
   also what keeps the trading lock meaningful (the server refuses the write, so this
   refuses to pretend). Nothing is ever REMOVED here — existing rules stay exactly where
   they are, and the copies follow them in the source's own order. */

/* Strategies a copy can come FROM: every live strategy except the active one — and only
   the ones that actually HAVE rules. A source with an empty rulebook is not a choice,
   it is a dead end, so it is not offered at all. */
function copySources() {
  const all = rulesStrategies();
  // The payload's `active` is the fallback for the first render, before renderRules has
  // normalised state.activeName: copying a strategy into itself is never a feature.
  const me = state.activeName || (state.rulesPayload && state.rulesPayload.active);
  return Object.keys(all).filter((n) => n !== me && copiedCount(n) > 0);
}

/* The copy itself, as a pure function so it can be exercised without a DOM: what is
   already here, followed by deep copies of everything the source has. Cloning matters —
   the two strategies must not share rule objects, or editing one would silently rewrite
   the other (and a later Save would push the edit into a strategy the user never
   touched). */
function appendedRules(existing, incoming) {
  const cur = Array.isArray(existing) ? existing : [];
  const add = Array.isArray(incoming) ? incoming : [];
  return cur.concat(JSON.parse(JSON.stringify(add)));
}

function copiedCount(srcName) {
  const src = rulesStrategies()[srcName];
  return src && Array.isArray(src.rules) ? src.rules.length : 0;
}

/* The picker's contents. Every candidate is a button because clicking the strategy IS
   the action the user asked for. */
function renderCopySources(names) {
  const host = $("rules-copy-list");
  if (!host) return;
  const list = names || copySources();
  host.innerHTML = list.length
    ? list
        .map(
          (n) =>
            `<button type="button" class="ghost small copy-src" data-src="${escapeHtml(n)}" ` +
            `title="Append every rule of “${escapeHtml(n)}” to “${escapeHtml(state.activeName || "")}”">` +
            `${escapeHtml(n)} <span class="copy-n">(${copiedCount(n)})</span></button>`
        )
        .join("")
    : `<span class="muted">No other strategy has rules yet — add them with “+ Buy” / “+ Sell” in that strategy first.</span>`;
}

/* Enable/disable the header button. Called from renderStrategyBar, which runs both when
   the payload loads and whenever the trading lock changes, so the lock can never leave a
   stale clickable button behind. */
function updateCopyButton() {
  const btn = $("rules-copy");
  const names = copySources();
  if (btn) {
    btn.disabled = !names.length || !!state.tradingLocked;
    btn.title = names.length
      ? "Copy every rule (enabled and disabled) of another strategy into this one"
      : "No other strategy has rules to copy yet";
  }
  const row = $("rules-copy-row");
  if (row && !row.hidden) renderCopySources(names); // keep an open picker in step
}

function toggleCopyRules() {
  const row = $("rules-copy-row");
  if (!row) return;
  if (row.hidden) {
    ensureRulesOpen(); // the picker lives in the panel body, so show it
    renderCopySources();
  }
  row.hidden = !row.hidden;
}

/* Expand the Rules panel if it is collapsed — the picker is useless out of sight. */
function ensureRulesOpen() {
  const body = $("rules-body");
  const btn = $("toggle-rules");
  if (body && body.hidden) {
    body.hidden = false;
    if (btn) { btn.textContent = "−"; btn.title = "Collapse rules"; }
  }
}

function copyRulesFrom(srcName) {
  const rs = activeRuleset();
  const src = rulesStrategies()[srcName];
  if (!rs) { setMsg("No active strategy to copy into."); return; }
  if (!src) { setMsg(`Strategy “${srcName}” is not available.`); return; }
  const incoming = src.rules || [];
  if (!incoming.length) {
    setMsg(`“${srcName}” has no rules to copy.`);
    return;
  }
  const had = (rs.rules || []).length;
  rs.rules = appendedRules(rs.rules, incoming);
  renderRules();        // existing rules are kept; the copied ones follow them
  renderStrategyBar();  // the header's rule lines are part of the same view
  syncBtRules();
  const off = incoming.filter((r) => r.enabled === false).length;
  const dis = off ? `, ${off} of them disabled` : "";
  setMsg(
    `Copied ${incoming.length} rule${incoming.length === 1 ? "" : "s"}${dis} from “${srcName}” — ` +
    `“${state.activeName}” now has ${rs.rules.length}` +
    `${had ? ` (its own ${had} kept)` : ""}. Press 💾 Save to keep them.`
  );
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
  const DEFAULT_PERIOD = "2y"; // matches Settings' historical_lookback default
  const cfg = rs.config || {};
  const oldInstrument = (cfg.INSTRUMENT || rs.instrument || "").trim().toUpperCase();
  const oldBar = (cfg.HISTORICAL_BAR_SIZE || "").trim();
  const oldPeriod = cfg.HISTORICAL_LOOKBACK != null
    ? String(cfg.HISTORICAL_LOOKBACK).trim()
    : DEFAULT_PERIOD;
  const instrInput = document.getElementById("scfg-INSTRUMENT");
  const barInput = document.getElementById("scfg-HISTORICAL_BAR_SIZE");
  const periodInput = document.getElementById("scfg-HISTORICAL_LOOKBACK");
  const newInstrument = instrInput ? (instrInput.value || "").trim().toUpperCase() : oldInstrument;
  const newBar = barInput ? (barInput.value || "").trim() : oldBar;
  const newPeriod = periodInput ? String(periodInput.value || "").trim() : oldPeriod;

  const instrumentChanged = newInstrument !== oldInstrument;
  const barChanged = newBar !== oldBar;
  // A stored/absent period value is compared to the effective default ("2y"), so
  // changing the select (even on a legacy strategy) is recognized as a change.
  const periodChanged = newPeriod != null && newPeriod !== oldPeriod;
  const historyChanged = barChanged || periodChanged;

  const revert = () => {
    if (instrInput) instrInput.value = oldInstrument;
    if (barInput) barInput.value = oldBar;
    if (periodInput) periodInput.value = oldPeriod;
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
    // ── Historical window / bar size change → download the new window ──────
    const parts = [];
    if (newBar !== oldBar) parts.push(`bar size <b>${barSizeLabel(oldBar) || "—"}</b> → <b>${barSizeLabel(newBar) || "—"}</b>`);
    if (periodChanged) parts.push(`history <b>${periodLabel(oldPeriod)}</b> → <b>${periodLabel(newPeriod)}</b>`);
    const ok = await confirmDialog({
      title: "Download the new historical window?",
      messageHtml:
        `<p>You changed the ${parts.join(" and ")}.</p>` +
        `<p>The new window will be <b>downloaded automatically</b> for ` +
        `<b>${escapeHtml(newInstrument || oldInstrument)}</b> ` +
        `(<b>${escapeHtml(periodLabel(newPeriod) || "2 years")}</b> at <b>${escapeHtml(barSizeLabel(newBar || oldBar))}</b>).</p>` +
        `<p class="muted">Existing history is <b>kept and merged</b> — bars already downloaded ` +
        `(including those of other bar sizes and other strategies on this instrument) stay on disk, ` +
        `and only the new window is added. Your rules are kept and are ` +
        `re-applied to the dataset afterwards.</p>`,
      confirmText: "Save & download",
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

/* There is no "↺ Default rules" handler here any more: the button was never used, and a
   reset that overwrites a strategy's own rulebook is the one action in this panel that
   can only destroy work. The server endpoint it called is left in place (it is pinned by
   the trading-gate tests and is still the way a first strategy gets seeded) — the panel
   simply no longer offers it. */

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
    const bar = String((rs.config && rs.config.HISTORICAL_BAR_SIZE) || "").trim();
    const bars = bar ? ` (${escapeHtml(barSizeLabel(bar))})` : "";
    chkText.innerHTML = instr
      ? `Also delete the historical data file(s) for <code>${escapeHtml(instr)}</code>${bars}. ` +
        `Files another live strategy is still using are kept.`
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
  // The picker is rebuilt on every render, so the click is caught on its container
  // rather than on buttons that stop existing. Delegated, not inline: a strategy name
  // is user text, and it must never be interpolated into an onclick attribute.
  const copyHost = $("rules-copy-list");
  if (copyHost) {
    copyHost.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-src]");
      if (btn) copyRulesFrom(btn.dataset.src);
    });
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
    "rules-add-buy", "rules-add-sell", "rules-copy", // rule editing (dead-ended without a save)
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
      // Counted in BARS, like the Historical Delta panel it points at. Counting
      // whole days here reported "missing 0 completed bars" for a session that is
      // merely short by an hour — a number that contradicts the panel right below
      // it and hides the very gap the gate is refusing to run on.
      //
      // The session list comes from the BARS rather than from `missing`: a short
      // session has bars missing without being an absent day, so naming only the
      // absent days would point at one of three affected sessions and imply the
      // other two were fine.
      // Name the sessions the BLOCKING bars belong to. Untraded intervals spread across
      // most of a thin symbol's history, so letting them into this list named 29 sessions
      // for a five-bar gap — and told the operator to go and fix intervals that have no
      // bar at any provider.
      const fromBars = Array.from(
        new Set(
          (dd.missing_bars || [])
            .filter((b) => b.reason !== "no_trades")
            .map((b) => b.date)
        )
      );
      const days = fromBars.length ? fromBars : Array.from(new Set(dd.missing || []));
      const bars = dd.missing_bars_total != null
        ? dd.missing_bars_total
        : (dd.missing || []).length;
      const where = days.length
        ? ` across ${days.length} ${days.length === 1 ? "session" : "sessions"}: ${days.join(", ")}`
        : "";
      dataTxt = `⚠ Historical data is missing ${bars} completed ${barWord(bars)}${where}. ` +
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
    drawBtCurve(r.equity_curve, r.bar_size);
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

/* Runs are stored, so the one on show can predate a change of bar size — and then
   its bars share no time with the chart's at all, which means the curve cannot be
   placed on the chart's bars and cannot move with it. Say why, instead of leaving a
   curve that silently ignores every gesture made on the chart. */
function noteCurveBarSize(runBarSize) {
  const note = $("bt-curve-note");
  if (!note) return;
  const chartBar = (state.status && state.status.interval) || "";
  const runBar = String(runBarSize || "");
  if (!chartBar || !runBar || chartBar === runBar) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  note.textContent =
    `⚠ This run used ${barSizeLabel(runBar)} bars while the chart shows ` +
    `${barSizeLabel(chartBar)} bars, so the two have no bars in common and the ` +
    "curve cannot follow the chart. Run the backtest again to sync them.";
  note.hidden = false;
}

function drawBtCurve(points, runBarSize) {
  const host = $("bt-curve");
  if (!host) return;
  noteCurveBarSize(runBarSize);
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
      // The axis itself stays hidden (the curve rides the price chart's bars, which
      // already carry the labels), but the crosshair readout is shared with every
      // other chart, so it must name the instant the same way (see chart_time.js).
      timeScale: axisTimeScale({ borderColor: "#333a46", visible: false, minBarSpacing: ChartZoom.MIN_BAR_SPACING }),
      localization: axisLocalization(),
      // Same wheel policy as the price chart and the panes (see chart_zoom.js).
      handleScroll: { mouseWheel: true },
      handleScale: {
        mouseWheel: false,
        axisPressedMouseMove: { time: false, price: true },
      },
      // Same free-floating crosshair, and the same boxed time label under it: this curve
      // rides the price chart's bars.
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    const line = chart.addLineSeries({ color: "#4c8dff", lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
    const curve = points.map((p) => ({ time: p.time, value: p.equity }));
    line.setData(curve);
    btChart = chart;
    watchPaneInteraction(chart, host); // gestures here drive the main chart
    ChartZoom.bind(host, () => ({ chart: btChart === chart ? chart : null, barCount: curve.length }));
    // The equity curve joins the crosshair sync too (its zoom already follows
    // the main chart), anchored on the equity value at each time.
    const anchor = _crosshairValues(curve);
    registerCrosshairAnchor(chart, line, anchor.values, anchor.fallback);

    // Link the equity curve to the price chart's bars, exactly like the oscillator
    // panes: wait for this chart's initial paint (2 frames) so its own default
    // right-aligned view cannot be mistaken for the user moving it, then join it to
    // whatever the price chart is showing — not to a full-content fit.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (btChart !== chart || !state.chart) return; // superseded/rebuilt
        _rangeTimes.set(chart, curve.map((p) => p.time));
        registerRangeSync(chart);
      });
    });
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
  // A result that lands inside a shut panel looks like nothing happened, so asking for a run
  // is asking to see it.
  expandCard($("backtest"));
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
showPendingToast();
refresh();
loadAccount(true);
loadRules();
loadTrading(); // last: it applies the configuration lock on top of the rendered panels
