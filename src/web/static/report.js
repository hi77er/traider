/* TRAIDER — standalone backtest report page.
 *
 * Opened from the Backtest panel ("Open full report"). Shows every stored run
 * of ONE strategy and renders the full report for the selected one (newest by
 * default). The run menu is the only navigation; clicking a run re-fetches its
 * report and keeps the URL in sync so a refresh reopens the same run.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(window.location.search);
  const state = {
    strategy: params.get("strategy") || "",
    runId: params.get("run_id") || "",
    data: null,
    charts: [],
  };

  /* ---------- formatting ---------- */
  const esc = (v) =>
    String(v == null ? "" : v).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  const isNum = (v) => v != null && isFinite(Number(v));
  const pct = (v, d = 2) =>
    isNum(v) ? (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(d) + "%" : "—";
  const num = (v, d = 2) => (isNum(v) ? Number(v).toFixed(d) : "—");
  const int = (v) => (isNum(v) ? Math.round(Number(v)).toLocaleString() : "—");
  const signCls = (v) => (isNum(v) && Number(v) > 0 ? "pos" : isNum(v) && Number(v) < 0 ? "neg" : "");
  const pf = (v) => (v == null ? "—" : isFinite(Number(v)) ? Number(v).toFixed(2) : "∞");

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /** "20260910T114304Z-abc" or an ISO stamp -> "2026-09-10 11:43 UTC". */
  function when(v) {
    if (!v) return "—";
    const s = String(v);
    const m = s.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/);
    if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC`;
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      let detail = `HTTP ${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = String(body.detail);
      } catch (_) { /* not JSON — keep the status line */ }
      throw new Error(detail);
    }
    return res.json();
  }

  /* Transient message, bottom-centre (reuses the Strategy lab's #toast styling). */
  function flashToast(text, kind) {
    const el = $("toast");
    if (!el) return;
    el.textContent = text;
    el.className = kind || "";
    el.hidden = false;
    clearTimeout(flashToast._timer);
    flashToast._timer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  /* Promise-based confirmation dialog (same contract as the Strategy lab's, `kind` included: a
   * delete is a `danger`, and looks like one — see ``.modal.danger`` in style.css). */
  function confirmDialog(opts) {
    return new Promise((resolve) => {
      const backdrop = $("rp-confirm-backdrop");
      const box = backdrop ? backdrop.querySelector(".modal") : null;
      const title = $("rp-confirm-title");
      const message = $("rp-confirm-message");
      const okBtn = $("rp-confirm-ok");
      const cancelBtn = $("rp-confirm-cancel");
      const kind = opts.kind || "";
      if (box) {
        box.className = kind ? `modal ${kind}` : "modal";
        if (kind) box.setAttribute("data-icon", opts.icon || (kind === "danger" ? "🛑" : "⚠️"));
        else box.removeAttribute("data-icon");
      }
      title.textContent = opts.title || "Are you sure?";
      message.innerHTML = opts.messageHtml || "";
      okBtn.textContent = opts.confirmText || "Yes";
      okBtn.className = kind === "danger" ? "danger" : kind === "warn" ? "caution" : "primary";
      cancelBtn.textContent = opts.cancelText || "No";

      const close = (result) => {
        backdrop.hidden = true;
        okBtn.onclick = null;
        cancelBtn.onclick = null;
        backdrop.onclick = null;
        document.removeEventListener("keydown", onKey);
        resolve(result);
      };
      const onKey = (e) => {
        if (e.key === "Escape") close(false);
        if (e.key === "Enter") close(true);
      };
      okBtn.onclick = () => close(true);
      cancelBtn.onclick = () => close(false);
      backdrop.onclick = (e) => { if (e.target === backdrop) close(false); };
      document.addEventListener("keydown", onKey);
      backdrop.hidden = false;
      okBtn.focus();
    });
  }

  /* Delete the run currently on screen — from disk, after confirmation. */
  async function deleteReport() {
    const rep = (state.data && state.data.report) || null;
    const runId = (rep && rep.run_id) || state.runId || "";
    if (!runId) return;

    const ok = await confirmDialog({
      title: "Delete this report?",
      messageHtml:
        `<p>This removes the stored run <code>${esc(runId)}</code> for ` +
        `<b>${esc(state.strategy || "this strategy")}</b> from disk.</p>` +
        `<p class="muted">Re-running the backtest recreates the file; the stored numbers are ` +
        `gone.</p>`,
      confirmText: "Delete report",
      cancelText: "Cancel",
      kind: "danger",
      icon: "🗑",
    });
    if (!ok) return;

    const btn = $("rp-delete");
    if (btn) btn.disabled = true;
    try {
      const qs = new URLSearchParams();
      if (state.strategy) qs.set("strategy", state.strategy);
      qs.set("run_id", runId);
      const res = await api("/api/v1/report/run?" + qs.toString(), { method: "DELETE" });
      if (!res.ok) throw new Error(res.message || "The report could not be deleted.");
      state.runId = "";
      await load(""); // newest remaining run, or the empty state
      flashToast(res.message || `Deleted report ${runId}.`, res.message ? "warn" : "ok");
    } catch (err) {
      flashToast(`Delete failed: ${err.message}`, "warn");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* Bound with a listener (this file is an IIFE, so inline onclick cannot reach
     its functions). */
  function bindDelete() {
    const btn = $("rp-delete");
    if (btn) btn.addEventListener("click", deleteReport);
  }

  function showError(msg) {
    const el = $("rp-error");
    el.textContent = "⚠ " + msg;
    el.hidden = false;
  }
  function hideError() {
    $("rp-error").hidden = true;
  }
  function clearCharts() {
    state.charts.forEach((c) => {
      crossAnchors.delete(c); // a disposed chart must never be synced again
      try { c.remove(); } catch (_) { /* noop */ }
    });
    state.charts = [];
  }

  /* ---------- loading ---------- */
  async function load(runId) {
    const qs = new URLSearchParams();
    if (state.strategy) qs.set("strategy", state.strategy);
    if (runId) qs.set("run_id", runId);
    const delBtn = $("rp-delete");

    let data;
    try {
      data = await api("/api/v1/report/run?" + qs.toString());
    } catch (err) {
      showError("Could not load the report: " + err.message);
      return;
    }
    state.data = data;
    if (data.strategy) state.strategy = data.strategy;
    $("rp-strategy").textContent = data.strategy || "no strategy";
    document.title = `TRAIDER — Report · ${data.strategy || ""}`;

    const runs = data.runs || [];
    renderMenu(runs, data.run_id, data.strategy);

    const rep = data.report;
    if (!rep) {
      clearCharts();
      $("rp-content").hidden = true;
      $("rp-empty").hidden = false;
      $("rp-empty-msg").textContent = data.error || "No report available.";
      hideError();
      if (delBtn) delBtn.hidden = true; // nothing on screen to delete
      return;
    }
    if (data.error) showError(data.error);
    else hideError();
    $("rp-empty").hidden = true;
    $("rp-content").hidden = false;
    if (delBtn) delBtn.hidden = !rep.run_id;
    renderReport(rep);

    // Keep the URL in sync so a refresh reopens the run being viewed.
    const url = new URL(window.location.href);
    url.searchParams.set("strategy", state.strategy);
    if (rep.run_id) url.searchParams.set("run_id", rep.run_id);
    history.replaceState(null, "", url);
  }

  /* ---------- run menu ---------- */
  function renderMenu(runs, currentId, strategy) {
    const host = $("rp-runs");
    host.innerHTML = "";
    if (!runs.length) {
      host.innerHTML = `<p class="muted rp-menu-empty">No runs recorded for
        <b>${esc(strategy || "this strategy")}</b> yet.</p>`;
      return;
    }
    runs.forEach((r, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rp-run" + (r.run_id === currentId ? " active" : "");
      btn.title = r.run_id || "";
      btn.onclick = () => {
        if (r.run_id !== currentId) load(r.run_id);
      };
      btn.innerHTML =
        `<span class="rp-run-top">` +
        `<span class="rp-run-when">${esc(when(r.generated_at || r.run_id))}</span>` +
        `<span class="rp-run-right">` +
        (i === 0 ? `<span class="rp-run-tag">latest</span>` : "") +
        `<span class="rp-badge ${r.gate_pass ? "pass" : "fail"}">${r.gate_pass ? "SUCCESS" : "FAIL"}</span>` +
        `</span></span>` +
        `<span class="rp-run-sub">Sharpe ${num(r.sharpe)} · ${pct(r.total_return_pct)} · ${int(r.num_trades)} trades</span>` +
        `<span class="rp-run-sub muted">${esc(r.symbol || "")} ${esc(r.bar_size || "")}` +
        `${r.rows ? " · " + int(r.rows) + " bars" : ""}` +
        `${r.allow_short ? " · short enabled" : ""}</span>`;
      host.appendChild(btn);
    });
  }

  /* ---------- pieces ---------- */
  function gateHint(key, check) {
    const t = (x) => (isNum(x) ? Number(x).toFixed(2) : x);
    if (key === "sharpe") return `Gate passes when Sharpe is at least ${t(check.target)}`;
    if (key === "max_drawdown") return `Gate passes when max drawdown is at most ${t(check.target)}%`;
    if (key === "win_rate") return `Gate passes when win rate is at least ${t(check.target)}%`;
    if (key === "worst_week") return `Gate passes when the worst week loss is at most ${t(check.target)}%`;
    return `Gate target ${t(check.target)}`;
  }

  function tile(label, value, cls, gate, key) {
    const check = gate && gate.checks ? gate.checks[key] : null;
    const gcls = check ? (check.pass ? " gate-pass" : " gate-fail") : "";
    const tip = check ? ` data-tip="${esc(gateHint(key, check))}"` : "";
    return `<div class="bt-stat${gcls}"${tip}>` +
      `<span class="label">${esc(label)}</span>` +
      `<span class="value ${cls || ""}">${value}</span></div>`;
  }

  function renderHeader(rep) {
    const m = rep.metrics || {};
    const gate = rep.gate || {};
    const inst = `${rep.symbol || "?"} · ${rep.bar_size || "?"}`;

    $("rp-title").textContent = `${rep.strategy || "Strategy"} — ${inst}`;
    $("rp-sub").innerHTML =
      `run <code>${esc(rep.run_id || "—")}</code> · generated ${esc(when(rep.generated_at))}` +
      ` · ${int(rep.rows)} bars · ${esc(rep.start || "?")} → ${esc(rep.end || "?")}` +
      ` · model ${esc(rep.model_type || "?")}`;

    const gateEl = $("rp-gate");
    const hasGate = !!(gate && typeof gate.pass === "boolean");
    gateEl.hidden = !hasGate;
    gateEl.className = hasGate ? `rp-verdict ${gate.pass ? "pass" : "fail"}` : "rp-verdict";
    gateEl.textContent = hasGate ? (gate.pass ? "SUCCESS" : "FAIL") : "";

    const held = rep.trade_stats || {};
    const bench = rep.benchmark || {};
    const ddstats = rep.drawdown_stats || {};
    $("rp-kpis").innerHTML = [
      tile("Total return", pct(m.total_return_pct), signCls(m.total_return_pct)),
      tile("Buy &amp; hold", pct(bench.total_return_pct), signCls(bench.total_return_pct)),
      tile("Excess vs B&amp;H", pct(bench.excess_return_pct), signCls(bench.excess_return_pct)),
      tile("Ann. return", pct(m.annualized_return_pct)),
      tile("Ann. vol", pct(m.annualized_vol_pct)),
      tile("Sharpe", num(m.sharpe), "", gate, "sharpe"),
      tile("Max drawdown", pct(-(m.max_drawdown_pct || 0)), "", gate, "max_drawdown"),
      tile("Worst week", pct(m.worst_week_pct), "", gate, "worst_week"),
      tile("Win rate", num(m.win_rate_pct, 1) === "—" ? "—" : num(m.win_rate_pct, 1) + "%", "", gate, "win_rate"),
      tile("Trades", int(m.num_trades)),
      tile("Long / short", `${int(held.longs)} / ${int(held.shorts)}`),
      tile("Avg win", pct(m.avg_win_pct)),
      tile("Avg loss", pct(m.avg_loss_pct)),
      tile("Best trade", pct(held.best_pct), "pos"),
      tile("Worst trade", pct(held.worst_pct), "neg"),
      tile("Profit factor", pf(m.profit_factor)),
      tile("Exposure", isNum(m.exposure_pct) ? num(m.exposure_pct, 1) + "%" : "—"),
      tile("Max win streak", int(held.max_win_streak)),
      tile("Max loss streak", int(held.max_loss_streak)),
      tile("Avg bars held", num(held.avg_bars, 1)),
      tile("Longest drawdown", int(ddstats.longest_bars) + " bars"),
      tile("Years", num(m.years, 2)),
    ].join("");

    const notes = (rep.notes || []).slice();
    $("rp-notes").textContent = notes.join(" · ");
  }

  /* ---------- risk layer ---------- */
  const EXIT_LABELS = {
    stop: "Stop loss",
    take: "Take profit",
    signal: "Opposite signal",
    forced: "Still open at the end",
  };
  const EXIT_TIPS = {
    stop: "The position hit the stop loss and was closed at that level",
    take: "The position reached the take profit and was closed at that level",
    signal: "The opposite rule closed the position",
    forced: "The data ended while the position was open — closed at the last close",
  };
  const exitLabel = (r) => EXIT_LABELS[r] || r;
  const exitBadge = (r) => {
    const key = r || "signal";
    return `<span class="rp-exit ${esc(key)}" title="${esc(EXIT_TIPS[key] || key)}">` +
      `${esc(exitLabel(key))}</span>`;
  };
  const num0 = (v) => (isNum(v) ? num(v, 0) : "—");

  /* A vetoed bar's stamp. The engine stores a human label; older runs only have
     the chart key (a UTC unix int intraday), so format that as a fallback. */
  function vetoedWhen(v) {
    if (v.label) return String(v.label);
    if (typeof v.time === "number" && isFinite(v.time)) {
      const d = new Date(v.time * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
        `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    }
    return String(v.time || "").slice(0, 16);
  }

  /* What the risk layer did: how it sized positions, how they were closed, and
     which entries it refused. Derived from the stored run, so it also renders
     for runs recorded with the risk layer off (or before it existed). */
  function renderRisk(rep) {
    const risk = rep.risk || {};
    const exits = rep.exits || { total: 0, rows: [] };
    const rows = exits.rows || [];
    const byReason = {};
    rows.forEach((r) => { byReason[r.reason] = r; });
    const vetoed = risk.vetoed || [];
    const on = risk.applied === true;

    // ── the settings + what they produced, in one line ──
    const went = rows
      .map((r) => `${int(r.count)} ${exitLabel(r.reason).toLowerCase()}`)
      .join(", ");
    // Built from what is actually SET: a run may configure a stop and nothing else,
    // and printing "null% risk per trade" would suggest a setting that is not there.
    const sizing = [];
    if (risk.risk_limit_percent !== null && risk.risk_limit_percent !== undefined) {
      sizing.push(`${num(risk.risk_limit_percent, 2)}% risk per trade`);
    }
    if (risk.stop_loss_percent !== null && risk.stop_loss_percent !== undefined) {
      sizing.push(`${num(risk.stop_loss_percent, 2)}% stop`);
    }
    if (risk.take_profit_percent !== null && risk.take_profit_percent !== undefined) {
      sizing.push(`${num(risk.take_profit_percent, 2)}% take profit`);
    }
    const exposure = risk.max_exposure_percent === null || risk.max_exposure_percent === undefined
      ? "the whole account"
      : `max exposure ${num(risk.max_exposure_percent, 0)}%`;
    $("rp-risk-note").innerHTML = on
      ? `Positions sized at <b>${num(Number(risk.weight) * 100, 0)}% of the account</b>` +
        (sizing.length ? ` (${sizing.join(", ")}; ${exposure}). ` : ` (${exposure}). `) +
        (exits.total ? `The ${int(exits.total)} round trips ended: ${went}.` : "No round trips were taken.")
      : `<b>No risk settings configured</b> — these are the raw strategy's numbers: no sizing, ` +
        `no stop loss, no take profit and no exposure cap.` +
        (exits.total ? ` The ${int(exits.total)} round trips ended: ${went}.` : "");

    // ── tiles: the settings actually used (the outcome counts live in the
    //    table below, so repeating them here would just duplicate it) ──
    const tiles = [];
    if (on) {
      tiles.push(tile("Position size", `${num(Number(risk.weight) * 100, 0)}% of account`));
      tiles.push(tile("Risk per trade", `${num(risk.risk_limit_percent, 2)}%`));
      tiles.push(tile("Stop loss", `${num(risk.stop_loss_percent, 2)}%`));
      tiles.push(tile("Take profit", `${num(risk.take_profit_percent, 2)}%`));
      tiles.push(tile("Sizing mode", esc(String(risk.sizing_mode || "—").replace(/_/g, " "))));
      tiles.push(tile("Max exposure", `${num(risk.max_exposure_percent, 0)}% of account`));
    } else {
      tiles.push(tile("Risk settings", "none configured"));
    }
    $("rp-risk-tiles").innerHTML = tiles.join("");

    // ── how the round trips ended: every reason is listed even at zero, so a
    //    missing stop/take row is visibly "0" rather than absent ──
    const head =
      `<thead><tr><th>Exit</th><th>Trades</th><th>Share</th><th>Win rate</th>` +
      `<th>Avg return</th><th>Total</th><th>Avg bars</th></tr></thead>`;
    const zero = (reason) => `<td>${exitBadge(reason)}</td>` +
      `<td class="rp-cell muted">0</td><td class="rp-cell muted">0.0%</td>` +
      `<td class="rp-cell muted">—</td><td class="rp-cell muted">—</td>` +
      `<td class="rp-cell muted">—</td><td class="rp-cell muted">—</td>`;
    const body = ["stop", "take", "signal"]
      .map((reason) => {
        const r = byReason[reason];
        if (!r) return `<tr>${zero(reason)}</tr>`;
        return `<tr>` +
          `<td>${exitBadge(r.reason)}</td>` +
          `<td class="rp-cell">${int(r.count)}</td>` +
          `<td class="rp-cell muted">${num(r.pct, 1)}%</td>` +
          `<td class="rp-cell">${num(r.win_rate_pct, 1)}%</td>` +
          `<td class="rp-cell ${signCls(r.avg_ret_pct)}">${pct(r.avg_ret_pct)}</td>` +
          `<td class="rp-cell ${signCls(r.total_ret_pct)}">${pct(r.total_ret_pct)}</td>` +
          `<td class="rp-cell muted">${num(r.avg_bars, 1)}</td></tr>`;
      })
      .join("");
    // Positions still open when the data ran out are rare, so they only appear
    // when they actually happened.
    const forced = byReason.forced
      ? `<tr><td>${exitBadge("forced")}</td>` +
        `<td class="rp-cell">${int(byReason.forced.count)}</td>` +
        `<td class="rp-cell muted">${num(byReason.forced.pct, 1)}%</td>` +
        `<td class="rp-cell">${num(byReason.forced.win_rate_pct, 1)}%</td>` +
        `<td class="rp-cell ${signCls(byReason.forced.avg_ret_pct)}">${pct(byReason.forced.avg_ret_pct)}</td>` +
        `<td class="rp-cell ${signCls(byReason.forced.total_ret_pct)}">${pct(byReason.forced.total_ret_pct)}</td>` +
        `<td class="rp-cell muted">${num(byReason.forced.avg_bars, 1)}</td></tr>`
      : "";
    $("rp-exits").innerHTML = head + `<tbody>${body}${forced}</tbody>`;

    // ── the entries the risk layer refused (always listed, 0 included) ──
    const head2 = `<thead><tr><th>Bar</th><th>Side</th><th>Why</th></tr></thead>`;
    $("rp-vetoed").innerHTML = head2 + (vetoed.length
      ? `<tbody>` + vetoed.map((v) => `<tr>` +
          `<td class="muted">${esc(vetoedWhen(v))}</td>` +
          `<td><span class="rp-side ${esc(v.side || "long")}">${esc(v.side || "long")}</span></td>` +
          `<td class="muted">${esc(String(v.reason || "").replace(/_/g, " "))}</td></tr>`).join("") +
        `</tbody>`
      : `<tbody><tr><td colspan="3" class="muted">` +
        `0 — ${on ? "every entry was taken" : "no risk settings are configured"}.</td></tr></tbody>`);
  }

  /* ---------- crosshair sync (both report charts) ----------
     Hovering either chart shows the same vertical TIME line (and a horizontal
     value line) on the other, so the drawdown at an instant lines up with the
     equity at that same instant. lightweight-charts only draws a crosshair on
     the chart under the pointer, so the other is positioned with
     `setCrosshairPosition`, anchored on its OWN series value at that time.

     VERIFIED against 4.1.3: `setCrosshairPosition` emits no crosshair event, so
     the guard must expire on the next task rather than keying off the time just
     pushed — a stationary mouse repeats one bar time, and swallowing those
     repeats would freeze the crosshair on whichever chart was hovered first. */
  const crossAnchors = new Map(); // chart -> {series, values: Map, times, fallback}
  let crossSyncing = false;

  function registerCrosshair(chart, series, data) {
    if (!chart || !series) return;
    const values = new Map();
    const times = [];
    let fallback = null;
    (data || []).forEach((p) => {
      if (p == null || p.value == null) return;
      if (!values.has(p.time)) times.push(p.time);
      values.set(p.time, p.value);
      if (fallback == null) fallback = p.value;
    });
    // The Map is built in chronological order, so its keys are the ascending
    // time axis the nearest-previous lookup binary-searches.
    crossAnchors.set(chart, {
      series: series, values: values, times: times, fallback: fallback,
    });
    chart.subscribeCrosshairMove((param) => onCrosshairMove(chart, param));
  }

  function crosshairValueAt(anchor, time) {
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

  function onCrosshairMove(source, param) {
    if (!source || !crossAnchors.size || crossSyncing) return;
    const time = param && param.time != null ? param.time : null;
    crossSyncing = true;
    try {
      crossAnchors.forEach((anchor, chart) => {
        if (chart === source) return; // the hovered chart tracks the mouse itself
        try {
          if (time == null) {
            chart.clearCrosshairPosition();
          } else {
            chart.setCrosshairPosition(crosshairValueAt(anchor, time), time, anchor.series);
          }
        } catch (_) { /* series or point vanished in a rebuild — skip it */ }
      });
    } finally {
      setTimeout(() => { crossSyncing = false; }, 0);
    }
  }

  /* Time-axis options for THIS run's bars (see chart_time.js): the report knows its own
     bar size and the zone the bars were stamped in, so every chart in the report
     labels each candle with the period it actually is — the minute, the hour, the day.

     The `ChartTime` guard is for a stale cached page: an axis option is never worth
     blanking every chart over. */
  function runZone() {
    const rep = (state.data && state.data.report) || {};
    return ((rep.inputs || {}).settings || {}).market_timezone || "UTC";
  }

  function axisTimeScale(extra) {
    const base = Object.assign({}, extra);
    if (typeof ChartTime === "undefined") return base;
    const rep = (state.data && state.data.report) || {};
    return Object.assign(base, ChartTime.timeScaleOptions(rep.bar_size || "", runZone()));
  }

  // ...and the matching crosshair-label options, so the crosshair names a bar the same
  // way the report's own tables do.
  function axisLocalization() {
    return typeof ChartTime === "undefined" ? {} : ChartTime.localizationOptions(runZone());
  }

  function chart(host, height) {
    const c = LightweightCharts.createChart(host, {
      height,
      layout: { background: { color: "transparent" }, textColor: "#8a93a6" },
      grid: { vertLines: { color: "#22262f" }, horzLines: { color: "#22262f" } },
      rightPriceScale: { borderColor: "#333a46" },
      // Each candle is labelled with the bar it actually IS, in exchange-local time.
      timeScale: axisTimeScale({ borderColor: "#333a46", minBarSpacing: ChartZoom.MIN_BAR_SPACING }),
      localization: axisLocalization(),
      // Same wheel policy as the dashboard (see chart_zoom.js): a plain wheel
      // pans, only a pinch zooms, and the zoom stops at this chart's own data
      // rather than being clamped by the library after the fact.
      handleScroll: { mouseWheel: true },
      handleScale: {
        mouseWheel: false,
        pinch: false, // the library's own pinch is a second, unbounded zoomer (see chart_zoom.js)
        axisPressedMouseMove: { time: false, price: true },
      },
      // Free-floating crosshair, so the synced horizontal line is not snapped to
      // a sample's extremes, with the boxed time label under the vertical line
      // (see chart_time.js).
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    state.charts.push(c);
    return c;
  }

  /* Keep the report's charts in lock-step: moving or zooming either one moves the
     other. They are synced by LOGICAL range (bar indices), not by time range: a
     time range cannot express the empty space before the first bar or after the
     last one — the library clamps such a request to the data — so dragging one
     chart past the end of its series left the other standing still. Both series
     are built from the same equity-curve time keys, so index for index they ARE
     the same bars and no conversion is needed. */
  function linkRanges(charts) {
    const pushed = new WeakMap();
    charts.forEach((chart) => {
      chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (!range) return;
        const asked = pushed.get(chart);
        if (asked && Math.abs(asked.from - range.from) < 0.01
          && Math.abs(asked.to - range.to) < 0.01) {
          pushed.delete(chart);
          return; // our own push coming back — never echo it onwards
        }
        charts.forEach((other) => {
          if (other === chart) return;
          const moved = { from: range.from, to: range.to };
          pushed.set(other, moved);
          other.timeScale().setVisibleLogicalRange(moved);
        });
      });
    });
  }

  /* ---------- collapsible panels (both collapsed by default) ---------- */
  function togglePanel(bodyId, btnId, label) {
    const body = $(bodyId);
    const btn = $(btnId);
    if (!body) return;
    body.hidden = !body.hidden;
    if (btn) {
      btn.textContent = body.hidden ? "+" : "−";
      btn.title = body.hidden ? `Expand ${label}` : `Collapse ${label}`;
    }
  }

  /* Bound with listeners (not inline onclick) because this file is an IIFE, so
     its functions are not reachable from an HTML attribute. */
  function bindPanel(headId, btnId, bodyId, label) {
    const head = $(headId);
    const btn = $(btnId);
    const toggle = () => togglePanel(bodyId, btnId, label);
    if (head) head.addEventListener("click", toggle);
    if (btn) {
      // The button sits inside the head: stop the click reaching the head's
      // handler so it does not toggle twice.
      btn.addEventListener("click", (ev) => { ev.stopPropagation(); toggle(); });
    }
  }

  function renderCharts(rep) {
    clearCharts();
    const linked = [];

    const eqHost = $("rp-equity");
    eqHost.innerHTML = "";
    const equity = rep.equity_curve || [];
    if (equity.length) {
      const c = chart(eqHost, 300);
      const strat = c.addLineSeries({
        color: "#4c8dff", lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
      });
      const eqPoints = equity.map((p) => ({ time: p.time, value: p.equity }));
      strat.setData(eqPoints);
      ChartZoom.bind(eqHost, () => ({ chart: c, barCount: eqPoints.length }));
      // Anchor the synced horizontal line on the strategy's equity, so hovering
      // the drawdown chart reads the equity of that instant (and vice versa).
      registerCrosshair(c, strat, eqPoints);
      const bench = rep.benchmark_curve || [];
      if (bench.length) {
        const b = c.addLineSeries({
          color: "#8a93a6", lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: true,
        });
        b.setData(bench.map((p) => ({ time: p.time, value: p.equity })));
      }
      $("rp-legend-bench").hidden = bench.length === 0;
      linked.push(c);
    } else {
      eqHost.textContent = "No equity curve recorded.";
    }
    $("rp-excess").textContent = isNum(rep.benchmark && rep.benchmark.excess_return_pct)
      ? `Strategy vs buy & hold over the window: ${pct(rep.benchmark.excess_return_pct)}`
      : "";

    const ddHost = $("rp-drawdown");
    ddHost.innerHTML = "";
    const dd = rep.drawdown || [];
    if (dd.length) {
      const c = chart(ddHost, 160);
      const area = c.addAreaSeries({
        lineColor: "#ef5350",
        topColor: "rgba(239, 83, 80, 0.30)",
        bottomColor: "rgba(239, 83, 80, 0.02)",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      const ddPoints = dd.map((p) => ({ time: p.time, value: p.dd_pct }));
      area.setData(ddPoints);
      ChartZoom.bind(ddHost, () => ({ chart: c, barCount: ddPoints.length }));
      registerCrosshair(c, area, ddPoints);
      linked.push(c);
    } else {
      ddHost.textContent = "No drawdown series.";
    }

    // Link the charts, then fit the first — the others follow it.
    if (linked.length > 1) linkRanges(linked);
    // Fit it AFTER the layout has given the chart a width. A fitContent() on a
    // chart that has not been laid out yet does nothing useful and the library
    // falls back to its default right-aligned view, which then disagrees with the
    // chart it is linked to — one chart showing the whole run and the other its
    // last few months. Two frames is the same wait the dashboard uses.
    if (linked.length) {
      const fit = linked[0];
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          if (!state.charts.includes(fit)) return; // a newer render superseded us
          fit.timeScale().fitContent();
        });
      });
    }

    const s = rep.drawdown_stats || {};
    $("rp-dd-note").textContent =
      `Worst drawdown ${pct(s.max_dd_pct)}` +
      (s.max_dd_time ? ` on ${s.max_dd_time}` : "") +
      ` · longest underwater stretch ${int(s.longest_bars)} bars` +
      (s.longest_end ? ` (to ${s.longest_end})` : "") +
      ` · currently ${s.recovered ? "at a new peak" : pct(s.current_dd_pct) + " below the peak"}.`;
  }

  function renderMonthly(rep) {
    const matrix = rep.monthly_matrix || { rows: [], month_labels: MONTHS };
    const labels = matrix.month_labels || MONTHS;
    const rows = matrix.rows || [];
    const head =
      `<thead><tr><th class="rp-th-year">Year</th>` +
      labels.map((m) => `<th>${esc(m)}</th>`).join("") +
      `<th class="rp-th-total">Year</th></tr></thead>`;
    const body = rows
      .map((r) => {
        const cells = labels
          .map((_, i) => {
            const v = (r.months || {})[String(i + 1)];
            return isNum(v)
              ? `<td class="rp-cell ${signCls(v)}">${num(v, 1)}</td>`
              : `<td class="rp-cell muted">·</td>`;
          })
          .join("");
        return `<tr><th class="rp-th-year">${r.year}</th>${cells}` +
          `<td class="rp-cell rp-total ${signCls(r.total_pct)}">${num(r.total_pct, 1)}</td></tr>`;
      })
      .join("");
    $("rp-monthly").innerHTML = head + `<tbody>${body || `<tr><td colspan="14" class="muted">No monthly data.</td></tr>`}</tbody>`;
  }

  function renderYearly(rep) {
    const rows = rep.yearly || [];
    const body = rows
      .map((r) => `<tr><th class="rp-th-year">${r.year}</th>` +
        `<td class="rp-cell ${signCls(r.ret_pct)}">${pct(r.ret_pct)}</td>` +
        `<td class="rp-cell muted">${int(r.bars)} bars</td>` +
        `<td class="rp-cell muted">${esc(r.start_date || "")} → ${esc(r.end_date || "")}</td></tr>`)
      .join("");
    $("rp-yearly").innerHTML =
      `<thead><tr><th class="rp-th-year">Year</th><th>Return</th><th>Bars</th><th>Covered</th></tr></thead>` +
      `<tbody>${body || `<tr><td colspan="4" class="muted">No yearly data.</td></tr>`}</tbody>`;
  }

  function renderDistribution(rep) {
    const dist = rep.distribution || { buckets: [] };
    const buckets = dist.buckets || [];
    const max = Math.max(1, ...buckets.map((b) => b.count));
    const bars = buckets
      .map((b) => {
        const neg = b.label.trim().startsWith("<") || b.label.includes("-");
        const w = Math.round((b.count / max) * 100);
        return `<div class="rp-dist-row">` +
          `<span class="rp-dist-label">${esc(b.label)}</span>` +
          `<span class="rp-dist-track"><span class="rp-dist-bar ${neg ? "neg" : "pos"}" style="width:${w}%"></span></span>` +
          `<span class="rp-dist-count">${b.count}${b.count ? ` <span class="muted">(${num(b.pct, 0)}%)</span>` : ""}</span>` +
          `</div>`;
      })
      .join("");
    const head = `<div class="rp-dist-sum">` +
      `${int(dist.total)} closed trades · best ${pct(dist.best_pct)} · worst ${pct(dist.worst_pct)}` +
      ` · median ${pct(dist.median_pct)} · mean ${pct(dist.mean_pct)} · σ ${num(dist.stdev_pct)}` +
      `</div>`;
    $("rp-dist").innerHTML = head + bars;
  }

  function renderTrades(rep) {
    const trades = rep.trades || [];
    const stats = rep.trade_stats || {};
    const exits = rep.exits || { rows: [] };
    const how = (exits.rows || [])
      .filter((r) => r.reason === "stop" || r.reason === "take")
      .map((r) => `${int(r.count)} ${exitLabel(r.reason).toLowerCase()}`)
      .join(", ");
    $("rp-trades-note").textContent = trades.length
      ? `All ${int(trades.length)} round-trips (no truncation) — ` +
        `${int(stats.wins)} winners, ${int(stats.losses)} losers, ` +
        `${int(stats.longs)} long / ${int(stats.shorts)} short` +
        (how ? ` · ${how}` : "") + "."
      : "No trades were taken over this window.";

    if (!trades.length) {
      $("rp-trades").innerHTML = `<tbody><tr><td class="muted">No trades.</td></tr></tbody>`;
      return;
    }
    const body = trades
      .map((t) => {
        const side = String(t.side || "long");
        return `<tr>` +
          `<td class="muted">${int(t.i)}</td>` +
          `<td><span class="rp-side ${side}">${esc(side)}</span></td>` +
          `<td class="muted">${esc((t.entry_time || "").slice(0, 16))}</td>` +
          `<td class="muted">${esc((t.exit_time || "").slice(0, 16))}</td>` +
          `<td>${exitBadge(t.exit_reason)}</td>` +
          `<td class="muted">${int(t.bars)}</td>` +
          `<td>${num(t.entry_price, 2)}</td>` +
          `<td>${num(t.exit_price, 2)}</td>` +
          `<td class="rp-cell ${signCls(t.ret_pct)}">${pct(t.ret_pct)}</td>` +
          `</tr>`;
      })
      .join("");
    $("rp-trades").innerHTML =
      `<thead><tr><th>#</th><th>Side</th><th>Entry time</th><th>Exit time</th>` +
      `<th>Exit</th><th>Bars</th><th>Entry px</th><th>Exit px</th><th>Return</th></tr></thead>` +
      `<tbody>${body}</tbody>`;
  }

  function conditionText(c) {
    const rhs = c.value != null ? c.value : c.ref;
    return `${c.feature} ${c.op} ${rhs}`;
  }

  function renderInputs(rep) {
    const inputs = rep.inputs || {};
    const win = inputs.window || {};
    const costs = inputs.costs || {};
    const rules = inputs.rules || [];
    const skipped = inputs.skipped_rules || [];
    const settings = inputs.settings || {};
    const gate = rep.gate || {};

    const ruleRows = rules.length
      ? rules
          .map(
            (r) =>
              `<li><span class="rp-side ${String(r.side || "").toLowerCase() === "sell" ? "short" : "long"}">` +
              `${esc(r.side)}</span> <span class="muted">${r.mode === "any" ? "any of" : "all of"}</span> ` +
              `${esc((r.conditions || []).map(conditionText).join(", ") || "—")}` +
              `${r.enabled === false ? ' <span class="muted">(disabled)</span>' : ""}</li>`
          )
          .join("")
      : `<li class="muted">No rules were evaluated.</li>`;

    const kv = (label, value) =>
      `<div class="rp-kv"><span class="label">${esc(label)}</span><span>${value}</span></div>`;

    const gateRows = Object.keys(gate.checks || {})
      .map((k) => {
        const c = gate.checks[k];
        return kv(k.replace(/_/g, " "), `${num(c.value)} vs ${num(c.target)} ` +
          `<span class="${c.pass ? "pos" : "neg"}">${c.pass ? "pass" : "fail"}</span>`);
      })
      .join("");

    const settingsRows = Object.keys(settings)
      .sort()
      .map((k) => kv(k, esc(settings[k])))
      .join("");

    // Which environment the strategy was pointed at. Paper and live results
    // legitimately differ (paper simulates no slippage, fees or dividends), so a
    // run that does not say which one it was cannot be compared with another.
    const execution = inputs.execution || {};
    const executionCell = execution.env
      ? `<span class="rp-exec ${execution.live ? "live" : "paper"}">` +
        `${esc(String(execution.env).toUpperCase())}</span>` +
        `<span class="muted"> · ${esc(execution.broker || "—")}` +
        `${execution.configured === false ? " · not configured" : ""}</span>`
      : `<span class="muted">—</span>`;

    $("rp-inputs").innerHTML =
      `<h4 class="rp-sub-head">Rules that ran (${rules.length})` +
      `${skipped.length ? ` · ${skipped.length} skipped` : ""}</h4>` +
      `<ul class="rp-rules">${ruleRows}</ul>` +
      `<p class="muted note">Rules fingerprint <code>${esc(inputs.rules_hash || "—")}</code> — ` +
      `a different rule set or window produces a different run id.</p>` +
      `<div class="rp-kv-grid">` +
      kv("Instrument", esc(win.instrument || rep.symbol || "—")) +
      kv("Bar size", esc(win.bar_size || rep.bar_size || "—")) +
      kv("Model", esc(rep.model_type || "—")) +
      kv("Execution", executionCell) +
      kv("Allow short", inputs.allow_short ? "yes" : "no") +
      kv("Bars replayed", int(win.rows)) +
      kv("Window", `${esc(win.start || "?")} → ${esc(win.end || "?")}`) +
      kv("Bars / year", int(win.periods_per_year)) +
      kv("Slippage", isNum(costs.slippage) ? num(Number(costs.slippage) * 100, 3) + "%" : "—") +
      kv("Commission", isNum(costs.commission) ? num(Number(costs.commission) * 100, 3) + "%" : "—") +
      `</div>` +
      (gateRows ? `<h4 class="rp-sub-head">Gate thresholds</h4><div class="rp-kv-grid">${gateRows}</div>` : "") +
      `<details class="rp-details"><summary>Effective settings snapshot (${Object.keys(settings).length})</summary>` +
      `<div class="rp-kv-grid">${settingsRows || '<span class="muted">—</span>'}</div></details>`;
  }

  function renderReport(rep) {
    renderHeader(rep);
    renderRisk(rep);
    renderCharts(rep);
    renderMonthly(rep);
    renderYearly(rep);
    renderDistribution(rep);
    renderTrades(rep);
    renderInputs(rep);
  }

  /* ---------- boot ---------- */
  const back = $("rp-back");
  if (back && state.strategy) back.href = "/";

  bindPanel("rp-what-head", "rp-what-toggle", "rp-what-body", "what was tested");
  bindPanel("rp-risk-head", "rp-risk-toggle", "rp-risk-body", "the risk layer");
  bindDelete();
  load(state.runId || "");
})();
