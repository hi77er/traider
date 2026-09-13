/* TRAIDER market landing page — whole-market screens served by /api/v1/market. */
"use strict";

const ROWS_PER_PANEL = 25;
const MAX_PRESET_ROWS = 25; // Yahoo's preset endpoint caps its own window
const PAGE_STEP = 25;       // rows per "page" when browsing the whole market

// Cards that can be expanded/collapsed *and* start collapsed, so the page reads
// as an overview: the headline lists are visible at once and the two longest
// tables are one click away. Every other panel is always open.
const COLLAPSIBLE_CARDS = ["screener", "whole_market"];

const state = {
  overview: null,
  presets: null,
  screener: { rows: [], criteria: "", error: null, preset: null },
  paging: {},   // panelKey -> offset
  panelRows: {}, // panelKey -> last fetched rows (for the current page)
  collapsed: { screener: true, whole_market: true }, // card key -> collapsed?
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

/* ── formatting ──────────────────────────────────────────────────────── */
function esc(value) {
  if (value == null) return "";
  return String(value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtPrice(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtCompact(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e12) return `${(v / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

function fmtMoneyCompact(v) {
  const s = fmtCompact(v);
  return s === "—" ? s : `$${s}`;
}

function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return { text: "—", cls: "" };
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
  const sign = v > 0 ? "+" : "";
  return { text: `${sign}${v.toFixed(2)}%`, cls };
}

/* ── table rendering ─────────────────────────────────────────────────── */
function rowHtml(r) {
  const pct = fmtPct(r.change_percent);
  const name = r.name ? ` title="${esc(r.name)}"` : "";
  const exch = r.exchange ? ` title="${esc(r.name || r.symbol)} · ${esc(r.exchange)}"` : name;
  return `<tr>
    <td class="mkt-sym"${exch}>${esc(r.symbol)}</td>
    <td>${fmtPrice(r.price)}</td>
    <td class="${pct.cls}">${pct.text}</td>
    <td>${fmtCompact(r.volume)}</td>
    <td>${fmtMoneyCompact(r.market_cap)}</td>
    <td>${fmtMoneyCompact(r.dollar_volume)}</td>
  </tr>`;
}

function bodyHtml(rows, error, cols) {
  if (error) return `<tr><td colspan="${cols}" class="mkt-empty error">${esc(error)}</td></tr>`;
  if (!rows || !rows.length) return `<tr><td colspan="${cols}" class="mkt-empty">No matching symbols right now.</td></tr>`;
  return rows.map(rowHtml).join("");
}

/* ── collapse / expand ───────────────────────────────────────────────── */
function isCardCollapsed(key) {
  return !!state.collapsed[key];
}

function expandCard(key) {
  if (!isCardCollapsed(key)) return;
  state.collapsed[key] = false;
  applyCardState(key);
}

function toggleMarketCard(key) {
  state.collapsed[key] = !isCardCollapsed(key);
  applyCardState(key);
}

function applyCardState(key) {
  const collapsed = isCardCollapsed(key);
  if (key === "screener") {
    // Toggle in place — re-rendering would wipe the filter inputs.
    const body = $("scr-body");
    if (body) body.hidden = collapsed;
    const toggle = $("toggle-screener");
    if (toggle) toggle.textContent = collapsed ? "+" : "−";
    return;
  }
  // Panels hold no user input, so re-render to keep markup and state in sync.
  const panel = ((state.overview || {}).panels || []).find((p) => p.key === key);
  const card = $(`panel-${key}`);
  if (panel && card) card.outerHTML = panelCardHtml(panel);
}

/* ── panels ──────────────────────────────────────────────────────────── */
function panelCardHtml(p) {
  const key = p.key;
  const collapsible = COLLAPSIBLE_CARDS.indexOf(key) !== -1;
  const collapsed = isCardCollapsed(key);
  const wide = key === "whole_market";
  const offset = state.paging[key] || 0;
  const rows = state.panelRows[key] || p.rows;

  const pager = wide
    ? `<div class="settings-actions" onclick="event.stopPropagation()">
        <button class="ghost small" onclick="pagePanel('${esc(key)}', -${PAGE_STEP})"
                ${offset <= 0 ? "disabled" : ""}>← Prev</button>
        <button class="ghost small" onclick="pagePanel('${esc(key)}', ${PAGE_STEP})">Next →</button>
      </div>`
    : "";

  const head = collapsible
    ? `<div class="card-head" onclick="toggleMarketCard('${esc(key)}')" title="Expand / collapse">
        <span class="card-toggle" id="toggle-${esc(key)}">${collapsed ? "+" : "−"}</span>
        <h2>${esc(p.label)}</h2>
        ${pager}
      </div>`
    : `<div class="mkt-card-head"><h2>${esc(p.label)}</h2></div>`;

  const body = `
    <p class="mkt-desc">${esc(p.description || "")}</p>
    <p class="mkt-meta">${esc(p.criteria || "")}${wide
      ? ` · showing ${rows ? rows.length : 0} from offset ${offset}` : ""}</p>
    <div class="mkt-table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>Price</th><th>Chg %</th><th>Volume</th><th>Mkt Cap</th><th>$ Vol</th></tr></thead>
        <tbody>${bodyHtml(rows, p.error, 6)}</tbody>
      </table>
    </div>`;

  return `
  <section class="card mkt-card${wide ? " span-2" : ""}${collapsible ? " collapsible" : ""}" id="panel-${esc(key)}">
    ${head}
    ${collapsible ? `<div class="collapse-body"${collapsed ? " hidden" : ""}>${body}</div>` : body}
  </section>`;
}

function renderOverview(payload) {
  state.overview = payload;
  const host = $("mkt-cards");
  if (!payload.panels || !payload.panels.length) {
    host.innerHTML = `<p class="muted">No panels returned.</p>`;
    return;
  }
  host.innerHTML = payload.panels.map(panelCardHtml).join("");
}

async function loadOverview(force) {
  const btn = $("mkt-refresh");
  btn.disabled = true;
  try {
    const q = `?size=${ROWS_PER_PANEL}${force ? "&force=true" : ""}`;
    const payload = await api(`/api/v1/market/overview${q}`);
    renderOverview(payload);
    const at = payload.generated_at ? payload.generated_at.replace("T", " ").replace("+00:00", " UTC") : "—";
    $("mkt-updated").textContent = `${at}${payload.cached ? " (cached)" : ""}`;
    $("mkt-updated").classList.toggle("warn", !!(payload.errors && payload.errors.length));
    const err = $("mkt-error");
    if (payload.errors && payload.errors.length) {
      err.hidden = false;
      err.textContent = `Some panels failed — ${payload.errors.join(" · ")}`;
    } else {
      err.hidden = true;
      err.textContent = "";
    }
  } catch (e) {
    $("mkt-error").hidden = false;
    $("mkt-error").textContent = `Could not load the market panels: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function pagePanel(key, delta) {
  const offset = Math.max(0, (state.paging[key] || 0) + delta);
  state.paging[key] = offset;
  // Paging a table you cannot see makes no sense — the refetch renders it open.
  state.collapsed[key] = false;
  try {
    const d = await api(`/api/v1/market/panel/${encodeURIComponent(key)}?size=${PAGE_STEP}&offset=${offset}`);
    state.panelRows[key] = d.rows;
    const panel = (state.overview.panels || []).find((p) => p.key === key) || d;
    const card = $(`panel-${key}`);
    if (card) card.outerHTML = panelCardHtml({ ...panel, ...d });
  } catch (e) {
    state.panelRows[key] = [];
    const card = $(`panel-${key}`);
    if (card) card.outerHTML = panelCardHtml({ key, label: key, error: e.message, rows: [] });
  }
}

/* ── screener ────────────────────────────────────────────────────────── */
async function loadPresets() {
  try {
    const d = await api("/api/v1/market/presets");
    state.presets = d;
    const sel = $("scr-preset");
    sel.innerHTML = (d.presets || [])
      .map((p) => `<option value="${esc(p.key)}">${esc(p.label)}</option>`)
      .join("");
    if (d.default) sel.value = d.default;
    $("scr-count").textContent = `${(d.presets || []).length} presets`;
  } catch (e) {
    $("scr-msg").textContent = `Presets unavailable: ${e.message}`;
  }
}

function numOrNull(id) {
  const raw = $(id).value.trim();
  if (raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

// The Run button sits in the (always visible) header, so pressing it while the
// card is collapsed would otherwise fill a table nobody can see.
function runScreenerFromButton() {
  expandCard("screener");
  runScreener();
}

async function runScreener() {
  const btn = $("mkt-run");
  btn.disabled = true;
  $("scr-msg").textContent = "Running…";
  try {
    const params = new URLSearchParams();
    params.set("preset", $("scr-preset").value || "");
    params.set("size", String(MAX_PRESET_ROWS));
    const capMin = numOrNull("scr-cap-min");
    const capMax = numOrNull("scr-cap-max");
    // The inputs are in millions of USD.
    if (capMin != null) params.set("market_cap_min", String(Math.round(capMin * 1e6)));
    if (capMax != null) params.set("market_cap_max", String(Math.round(capMax * 1e6)));
    const minPrice = numOrNull("scr-min-price");
    if (minPrice != null) params.set("min_price", String(minPrice));
    const minVol = numOrNull("scr-min-volume");
    if (minVol != null) params.set("min_volume", String(Math.round(minVol)));

    const d = await api(`/api/v1/market/screen?${params.toString()}`);
    state.screener = d;
    $("scr-table").tBodies[0].innerHTML = bodyHtml(d.rows, d.error, 6);
    $("scr-criteria").textContent = d.criteria || "";
    $("scr-msg").textContent = d.error
      ? ""
      : `${d.count} row${d.count === 1 ? "" : "s"} · Yahoo ranks these; your filters are applied on top.`;
  } catch (e) {
    $("scr-table").tBodies[0].innerHTML = bodyHtml([], e.message, 6);
    $("scr-msg").textContent = "";
  } finally {
    btn.disabled = false;
  }
}

/* ── boot ────────────────────────────────────────────────────────────── */
async function refreshAll() {
  try { await api("/api/v1/market/refresh", { method: "POST" }); } catch (_) { /* cache reset is best-effort */ }
  state.paging = {};
  state.panelRows = {};
  await loadOverview(true);
  await runScreener();
}

(async function boot() {
  applyCardState("screener"); // the markup ships collapsed; keep the glyph in sync
  await loadPresets();
  await loadOverview(false);
  await runScreener();
})();
