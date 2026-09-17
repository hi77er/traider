/* TRAIDER — the trading log page.
 *
 * Account state first, local context second. That order is not decoration: the broker is the
 * truth about what is held and what is working, and everything the loop wrote down is the
 * EXPLANATION of it. A page that led with its own records would let a stale or deleted log
 * pass for the state of the account.
 *
 * Nothing here writes, and nothing here can place or cancel an order.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(window.location.search);

  const state = {
    loop: null,
    log: null,
    positions: null,
    accounts: null,
    orders: null,
    trades: null,
  };

  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replace(/[&<>"']/g, (char) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
    ));

  async function api(path) {
    const res = await fetch(path);
    if (!res.ok) {
      // Same reason as the dashboard's wrapper: a process running older code answers 404 for
      // an endpoint this page was built against, and "404 Not Found" reads like a typo.
      if (res.status === 404) {
        throw new Error(`${path} is missing (404) — the dashboard is running older code than "
          + "this page, so restart it`);
      }
      throw new Error(`${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  function fail(message) {
    const box = $("lg-error");
    box.hidden = false;
    box.textContent = message;
  }

  function empty(text) {
    return `<p class="muted">${esc(text)}</p>`;
  }

  // Only write when the text actually changed. This page now re-reads itself while today is
  // showing, and a table rewritten on every poll is one you cannot select text in.
  function setIfChanged(el, html) {
    if (el && el.innerHTML !== html) el.innerHTML = html;
  }

  function table(headers, rows, rowFor) {
    if (!rows.length) return null;
    return `<table class="lg-table"><thead><tr>${headers
      .map((h) => `<th>${esc(h)}</th>`)
      .join("")}</tr></thead><tbody>${rows.map(rowFor).join("")}</tbody></table>`;
  }

  function cell(value, cls) {
    return `<td${cls ? ` class="${cls}"` : ""}>${esc(value)}</td>`;
  }

  // The ⚠ a tick carries when its bar was odd but usable (`src.data.quality`). An em dash when
  // there is nothing to say, so an empty cell never reads as a value that failed to load —
  // and the text is shown rather than hidden behind a tooltip, because "which bar was strange,
  // and how" is exactly what someone reading this table came to find out.
  function notesCell(notes) {
    const list = (notes || []).filter(Boolean);
    if (!list.length) return "<td>—</td>";
    return `<td class="warn">⚠ ${esc(list.join("; "))}</td>`;
  }

  function money(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(2) : String(value);
  }

  // A day's change is only readable WITH its sign: "0.00" and "+0.00" look like a missing
  // value and a flat day respectively.
  function signedMoney(value) {
    if (value === null || value === undefined || value === "") return "—";
    const text = money(value);
    return Number(value) > 0 ? `+${text}` : text;
  }

  // ``day_pl_pct`` arrives as a percentage already, so it does NOT go through ``percent()`` —
  // that one multiplies a FRACTION by a hundred, and using it here would report a 1% day as
  // 100%.
  function percentText(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? `${number > 0 ? "+" : ""}${number.toFixed(2)}%` : String(value);
  }

  function percent(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : String(value);
  }

  function stamp(value) {
    if (!value) return "—";
    const when = new Date(value);
    return Number.isNaN(when.getTime()) ? String(value) : when.toLocaleString();
  }

  function shortAge(seconds) {
    if (seconds === null || seconds === undefined) return "never";
    const s = Math.max(0, Math.round(Number(seconds)));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  function renderHeader() {
    const loop = state.loop || {};
    const chip = $("lg-state");
    // Same four states the dashboard's Live panel shows, from the same endpoint — a quiet
    // market and a dead loop look identical otherwise.
    const labels = {
      running: ["running", "good"],
      overdue: ["OVERDUE — nothing is honouring the claim", "bad"],
      stopped: ["stopped", "muted"],
      never: ["never ran", "muted"],
    };
    const [text, cls] = labels[loop.state] || labels.never;
    chip.textContent = text;
    chip.className = `chip ${cls}`;
    $("lg-strategy").textContent =
      `${loop.strategy || "?"} · ${loop.env || "?"} · last tick ${shortAge(loop.last_tick_age_seconds)}`;
    document.title = `TRAIDER — log · ${loop.strategy || ""}`.trim();
  }

  function renderDays() {
    const days = (state.log && state.log.days) || [];
    const current = state.log && state.log.day;
    if (!days.length) {
      $("lg-days").innerHTML = empty("no day has been recorded yet");
      return;
    }
    setIfChanged($("lg-days"), days
      .map((day) => `<button class="rp-run${day === current ? " active" : ""}" data-day="${esc(day)}">
          <span class="rp-run-id">${esc(day)}</span></button>`)
      .join(""));
    for (const button of $("lg-days").querySelectorAll("[data-day]")) {
      button.onclick = () => loadLog(button.dataset.day);
    }
  }

  function renderAccount() {
    const positions = state.positions || { positions: [] };
    const orders = state.orders || { open: [], closed: [], resting: [] };

    $("lg-env").textContent = `${positions.env || orders.env || ""} · ${positions.instrument || ""}`;

    renderAccounts();

    const verdict = orders.protection;
    const protection = $("lg-protection");
    if (!verdict) {
      protection.textContent = "";
    } else if (verdict.state === "unprotected") {
      protection.className = "bad";
      protection.innerHTML = `<b>⚠ ${esc(verdict.message)}</b> — a level was set for this
        position and no order is resting at it.`;
    } else if (verdict.state === "none") {
      protection.className = "muted";
      protection.textContent = "Nothing is held.";
    } else if (verdict.state === "naked") {
      protection.className = "muted";
      protection.textContent = verdict.message;
    } else {
      protection.className = "muted";
      const levels = Object.entries(verdict.levels || {})
        .filter(([, v]) => v && v.wanted)
        .map(([kind, v]) => `${kind} ${Number(v.wanted).toFixed(2)}`);
      protection.textContent = `Protected: ${levels.join(", ")} — resting at the broker.`;
    }

    const rows = [];
    for (const account of positions.positions || []) {
      for (const held of account.positions || []) {
        rows.push({ env: account.env, ...held });
      }
      if (account.known === false) {
        rows.push({ env: account.env, symbol: "—", unknown: true });
      }
    }
    setIfChanged($("lg-positions"), rows.length
      ? table(
        ["account", "symbol", "qty", "avg entry", "market value", "unrealized"],
        rows,
        (row) => row.unknown
          ? `<tr><td>${esc(row.env)}</td><td colspan="5" class="muted">
               could not be read — the credentials for this account were refused</td></tr>`
          : `<tr>${cell(row.env)}${cell(row.symbol)}${cell(row.qty)}
             ${cell(money(row.avg_entry_price))}${cell(money(row.market_value))}
             ${cell(money(row.unrealized_pl))}</tr>`
      )
      : empty(orders.ok === false
        ? `the broker could not be read (${orders.message || "no credentials"}) — anything held is unknown, not zero`
        : "nothing is held"));

    const working = [...(orders.open || []), ...(orders.resting || [])];
    setIfChanged($("lg-working"), orders.ok === false
      ? empty("the broker could not be read, so nothing is known about working orders")
      : (table(
        ["id", "client id", "side", "type", "qty", "filled", "avg price", "stop", "limit", "status"],
        working,
        (order) => `<tr>${cell(order.id)}${cell(order.client_order_id)}${cell(order.side)}
          ${cell(order.type)}${cell(order.qty)}${cell(order.filled_qty)}
          ${cell(money(order.filled_avg_price))}${cell(money(order.stop_price))}
          ${cell(money(order.limit_price))}${cell(order.status)}</tr>`
      ) || empty("no orders are working")));
  }

  // Both environments, in the order the payload gives them, so a paper account and a live one
  // can be compared on one screen. An account that could not be read gets its REASON in place
  // of the numbers rather than a row of dashes: "unknown" and "$0.00" are different answers
  // and the difference is the whole point of showing this at all.
  function renderAccounts() {
    const payload = state.accounts || { accounts: [] };
    const rows = payload.accounts || [];
    const note = payload.ok === false
      ? empty(`the accounts could not be read (${payload.message || "no reason given"})`)
      : empty("no account could be read");
    setIfChanged($("lg-accounts"), rows.length
      ? table(
        ["account", "equity", "day", "day %", "cash", "buying power", "status"],
        rows,
        (row) => {
          if (!row.known) {
            return `<tr><td>${esc(row.env)}</td><td colspan="6" class="muted">
              ${esc(row.reason || "could not be read")}</td></tr>`;
          }
          const change = Number(row.day_pl);
          const cls = row.blocked ? "bad" : (change < 0 ? "bad" : (change > 0 ? "good" : ""));
          // Environment FIRST: it is what distinguishes the rows, and it is the same order
          // the positions table below uses for the same reason.
          const who = [row.env, row.account].filter(Boolean).join(" ");
          const status = [row.status, row.blocked ? "BLOCKED" : ""].filter(Boolean).join(" ");
          return `<tr>${cell(who || row.env, cls)}${cell(money(row.equity))}
            ${cell(signedMoney(row.day_pl), cls)}${cell(percentText(row.day_pl_pct), cls)}
            ${cell(money(row.cash))}${cell(money(row.buying_power))}${cell(status)}</tr>`;
        }
      )
      : note);
  }

  function renderTicks() {
    const ticks = (state.log && state.log.ticks) || [];
    $("lg-day").textContent = state.log ? state.log.day : "";
    setIfChanged($("lg-ticks"), table(
      ["when", "action", "bar", "signal", "reason", "orders", "notes"],
      ticks,
      (tick) => {
        const ids = (tick.order_ids || []).length;
        const cls = tick.action === "refused" ? "bad" : "";
        return `<tr>${cell(stamp(tick.at))}${cell(tick.action, cls)}${cell(tick.bar)}
          ${cell(tick.signal)}${cell(tick.reason)}
          <td>${ids ? `${ids} — ${esc((tick.order_ids || []).join(", "))}` : "—"}</td>
          ${notesCell(tick.notes)}</tr>`;
      }
    ) || empty("nothing was decided on this day"));
  }

  function renderOrders() {
    const orders = (state.log && state.log.orders) || [];
    setIfChanged($("lg-orders"), table(
      ["when", "intent", "status", "price", "expected", "bar", "broker id", "client id"],
      orders,
      (order) => `<tr>${cell(stamp(order.at))}${cell(order.intent)}${cell(order.status)}
        ${cell(money(order.price))}${cell(money(order.expected))}${cell(order.bar)}
        ${cell(order.order_id)}${cell(order.client_order_id)}</tr>`
    ) || empty("no order has been submitted yet"));
  }

  function renderTrades() {
    const trades = (state.trades && state.trades.trades) || [];
    setIfChanged($("lg-trades"), table(
      ["closed", "direction", "entry", "exit", "return", "weight", "bars", "reason"],
      trades,
      (trade) => {
        const cls = Number(trade.ret) < 0 ? "bad" : "good";
        return `<tr>${cell(stamp(trade.at))}${cell(trade.direction)}
          ${cell(money(trade.entry_price))}${cell(money(trade.exit_price))}
          ${cell(percent(trade.ret), cls)}${cell(trade.weight)}${cell(trade.bars)}
          ${cell(trade.reason)}</tr>`;
      }
    ) || empty("no round trip has been closed yet"));
  }

  async function loadLog(day) {
    const query = day ? `?day=${encodeURIComponent(day)}` : "";
    try {
      state.log = await api(`/api/v1/log${query}`);
    } catch (err) {
      fail(`could not read the log: ${err.message}`);
      state.log = null;
    }
    renderDays();
    renderTicks();
    renderOrders();
    // Choosing a day is what decides whether this page polls at all, so the decision is
    // remade here rather than only on load.
    schedulePoll();
  }

  async function loadAll(day) {
    try {
      state.loop = await api("/api/v1/loop");
    } catch (err) {
      fail(`could not read the loop's state: ${err.message}`);
      state.loop = null;
    }
    renderHeader();
    await loadLog(day || params.get("day"));
    // The broker half last, and independently: its failure must not blank the local log,
    // which is the half that still works when the account is unreachable.
    try {
      const [positions, accounts, orders, trades] = await Promise.all([
        api("/api/v1/positions"), api("/api/v1/accounts"), api("/api/v1/orders"),
        api("/api/v1/trades"),
      ]);
      state.positions = positions;
      state.accounts = accounts;
      state.orders = orders;
      state.trades = trades;
    } catch (err) {
      fail(`could not read the account: ${err.message}`);
      state.positions = { positions: [] };
      state.accounts = { ok: false, message: err.message, accounts: [] };
      state.orders = { ok: false, message: err.message, open: [], resting: [], closed: [] };
      state.trades = { trades: [] };
    }
    renderAccount();
    renderTrades();
    schedulePoll();
  }

  window.loadLog = loadLog;
  window.loadAll = loadAll;
  // The ↻ button means "re-read what I am looking at". Going through ``loadAll()`` with no
  // day would fall back to the server's newest, so a click from a past day would silently
  // jump the page forward — the one thing a refresh must not do.
  window.refreshLog = function () {
    return loadAll(state.log && state.log.day);
  };

  // The page re-reads itself only while TODAY is showing. A past day cannot gain rows, and
  // polling one would be broker traffic for a page nobody is watching change. Today is the
  // exchange's today, decided by the server, so a reader in another timezone does not drag
  // the page into refreshing a day that has already closed.
  const LOG_POLL_MS = 20000;
  let pollTimer = null;

  function isToday() {
    return !!(state.log && state.log.day && state.log.day === state.log.today);
  }

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  // Self-scheduling, so a slow day read delays the next poll instead of stacking one on top.
  function schedulePoll(delay) {
    stopPoll();
    if (document.hidden || !isToday()) return;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (document.hidden || !isToday()) return;
      await loadAll();
      schedulePoll(LOG_POLL_MS);
    }, delay === undefined ? LOG_POLL_MS : delay);
  }

  // A hidden tab is nobody watching: stop, and catch up in one go on the way back.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPoll();
    else loadAll();
  });
  window.addEventListener("pagehide", stopPoll);

  // Read once, on load, and then keep today's view current. A day button switches to that
  // day and does not poll; the ↻ button re-reads whatever is on screen.
  loadAll();
})();
