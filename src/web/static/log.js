/* TRAIDER — the trading log page.
 *
 * Account state first, local context second. That order is not decoration: the broker is the
 * truth about what is held and what is working, and everything the loop wrote down is the
 * EXPLANATION of it. A page that led with its own records would let a stale or deleted log
 * pass for the state of the account.
 *
 * Nothing here places, changes or cancels an order: the broker is only ever READ. What this page
 * writes is the master switch and the mode, and it does both through ``trading_switch.js``, the same
 * file the dashboard's Trading panel uses — starting, stopping and re-pointing the bot are three
 * actions with one wording each, not two pages' worth.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(window.location.search);

  const state = {
    loop: null,
    trading: null,
    log: null,
    positions: null,
    accounts: null,
    orders: null,
    trades: null,
  };

  // The countdown is the page's one local clock: what it counts to comes from the loop, and it is
  // the only thing here that redraws without a request. Stopped the moment nobody is looking.
  const COUNTDOWN_MS = 1000;
  let countdownTimer = null;
  let countdownUntil = null; // epoch ms of the next wake, or null when nothing is scheduled
  // How long after an arm to look again for the boundary the first tick commits to.
  const FIRST_TICK_MS = 5000;

  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replace(/[&<>"']/g, (char) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
    ));

  async function api(path, options) {
    const res = await fetch(path, options);
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

  /* Transient message, bottom-centre (the dashboard's #toast styling, same contract). */
  function flashToast(text, kind) {
    const el = $("toast");
    if (!el) return;
    el.textContent = text;
    el.className = kind || "";
    el.hidden = false;
    clearTimeout(flashToast._timer);
    flashToast._timer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  /* Promise-based confirmation dialog (same contract as the dashboard's). The master switch
   * cannot be taken without one, because on a LIVE account the click spends real money. */
  function confirmDialog(opts) {
    return new Promise((resolve) => {
      const backdrop = $("lg-confirm-backdrop");
      const title = $("lg-confirm-title");
      const message = $("lg-confirm-message");
      const okBtn = $("lg-confirm-ok");
      const cancelBtn = $("lg-confirm-cancel");
      title.textContent = opts.title || "Are you sure?";
      message.innerHTML = opts.messageHtml || "";
      okBtn.textContent = opts.confirmText || "Yes";
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

  /* The account this PAGE is about — the one the switch routes orders to. EVERY panel is scoped
   * to it: the figures, the positions, the day's ticks, the orders and the closed trades. Not a
   * cosmetic filter: the loop writes one set of files per strategy, so both accounts' rows sit in
   * the same files, separated only by the ``env`` field on each record. A page that shows both has
   * to be read with a mental filter, and the one time it cost something (the switch moved to live
   * with no live keys behind it) the idle paper account's numbers were read as the live one's.
   *
   * From the loop, falling back to the accounts payload. EMPTY when neither could be read — and
   * then nothing is filtered: hiding rows because the mode is unknown would be worse than showing
   * them, and this page's rule is to say nothing rather than guess. */
  function inPlayEnv() {
    const source = (state.loop && state.loop.env) || (state.accounts && state.accounts.env) || "";
    return String(source).toLowerCase();
  }

  /* Is this record the in-play account's? Every record the loop writes carries ``env``
   * (``tick_record``, ``order_record``, ``trade_record``), and a record with no ``env`` at all —
   * hand-written, or written before the field existed — counts as this account's: it cannot be
   * shown to be the other one's, and dropping it would hide a row that is probably this page's. */
  function mine(record) {
    const inPlay = inPlayEnv();
    if (!inPlay) return true;
    const env = String((record || {}).env || "").toLowerCase();
    return !env || env === inPlay;
  }

  /* The account that is NOT in play — the only other one there is. Named wherever a filter removed
   * rows, so that an empty table cannot read as "nothing happened" when something did happen, for
   * the other account. */
  function otherEnv() {
    const inPlay = inPlayEnv();
    if (inPlay === "live") return "paper";
    if (inPlay === "paper") return "live";
    return "";
  }

  /* "2 ticks" / "1 tick" — rows a filter dropped, counted in the reader's words. */
  function counted(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  /* The account a local record belongs to. Every table of the loop's own records carries this
   * column: those files hold both accounts' rows, a day's log outlives the switch that wrote it,
   * and nothing else in a row tells the two apart — same strategy, same bars, same words in
   * ``reason``. The rows are already filtered to the in-play account (``mine``); the column is what
   * keeps each row from having to be taken on trust.
   *
   * Deliberately without a "not in play" shade: nothing that is not in play reaches these tables
   * any more, so there would be nothing for it to mark. */
  function accountCell(env) {
    return cell(String(env || "").toLowerCase() || "—");
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
  /* The gates a tick walks, in the order ``src/scheduler/orchestrator.tick`` walks them, with
   * what each one ASKS. The ids are the ``stage`` the loop stamps on the record of a tick that
   * ended there, so the pipeline is rendered from the loop's own account of itself rather than
   * from anything this page infers — a test holds the two in step.
   *
   * ``asks`` is a question, not a claim about the last tick: the record says where the tick
   * stopped and why, and it does not describe the gates it sailed through. */
  const GATES = [
    { id: "switch", asks: "is trading on" },
    { id: "armed", asks: "is this the strategy the switch was armed for" },
    { id: "execution", asks: "could an order be placed at all" },
    { id: "clock", asks: "is the exchange open" },
    { id: "sync", asks: "is the dataset synced to now" },
    { id: "window", asks: "is the trailing window readable, and not behind" },
    { id: "quality", asks: "is the bar about to be decided on a bar at all" },
    { id: "decide", asks: "did the strategy decide, and act" },
  ];

  /* Records written before the loop stamped a stage, and hand-built ones: the verdict still says
   * most of it, and a gate derived from the verdict beats a pipeline with no current step. */
  const STAGE_BY_ACTION = {
    off: "switch", closed: "clock", noop: "decide", decided: "decide", refused: "decide",
  };

  function renderNextTick() {
    const loop = state.loop || {};
    const running = loop.state === "running";
    // Only a RUNNING loop has a boundary worth counting down to. An ``overdue`` claim names one
    // too — the boundary a process committed to before it died — and counting down to that would
    // promise a tick that nothing is going to make.
    syncCountdown(running ? loop.next_wake : null, loop.state);
    renderGates(loop.last_tick);
  }

  /* The clock's face: hours, minutes, seconds — always all three, so the SECONDS move whatever the
   * bar size is. They are also what shows the thing is alive, which is why the separators do not
   * blink as well: one moving part is a clock, two is a power light.
   *
   * The hours are total hours rather than a day count: `76:00:00` for a three-day wait reads as a
   * timer, while a `3d 4h` face would have to give up the seconds to stay short. */
  function clockText(ms) {
    const total = Math.max(0, Math.round(ms / 1000));
    const pad = (value) => String(value).padStart(2, "0");
    return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:`
      + `${pad(total % 60)}`;
  }

  function whenText(iso) {
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return "";
    const today = new Date();
    const sameDay = when.toDateString() === today.toDateString();
    return when.toLocaleString([], sameDay
      ? { hour: "2-digit", minute: "2-digit" }
      : { weekday: "short", hour: "2-digit", minute: "2-digit" });
  }

  // The countdown is the page's only clock of its own, and it is LOCAL: no request, no poll, just
  // arithmetic on the boundary the loop already committed to. It is restarted from each status
  // read, so a loop that moves its own boundary cannot leave this showing an old one.
  function syncCountdown(nextWake, loopState) {
    stopCountdown();
    const until = nextWake ? new Date(nextWake).getTime() : NaN;
    countdownUntil = Number.isNaN(until) ? null : until;
    writeCountdown(loopState);
    if (countdownUntil === null || document.hidden) return;
    countdownTimer = setInterval(() => writeCountdown(loopState), COUNTDOWN_MS);
  }

  function writeCountdown(loopState) {
    const clock = $("lg-countdown");
    const when = $("lg-next-when");
    if (!clock || !when) return;
    if (countdownUntil === null) {
      // A loop holds the lease BEFORE it has ticked, and the boundary is written by that first
      // tick — so "running with no next wake" is a loop that is starting, not one that is idle,
      // and calling it Nothing Scheduled would be the same lie as the warning this page carries.
      if (loopState === "running") {
        setIfChanged(clock, "starting…");
        setIfChanged(when, "Starting — it commits to a wake when the first tick finishes.");
        return;
      }
      setIfChanged(clock, "—");
      setIfChanged(when, loopState === "overdue"
        ? "The loop that claimed it is gone — nothing will tick."
        : "Nothing scheduled — no loop is running.");
      return;
    }
    const left = countdownUntil - Date.now();
    // Past the boundary and still running: the loop is inside the tick it woke up for, so the
    // clock stands at zero and the line under it says so. The next status read brings the
    // boundary it commits to next.
    setIfChanged(clock, clockText(left));
    setIfChanged(when, left <= 0
      ? "The bar closed — the loop is ticking."
      : `The bar closes ${whenText(new Date(countdownUntil).toISOString())} · `
        + "The loop wakes seconds later.");
  }

  function stopCountdown() {
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  }

  /* The pipeline, as the last tick walked it: the gates it passed, the one that ended it with the
   * loop's own reason, and the ones it never reached. */
  function renderGates(tick) {
    const host = $("lg-gates");
    if (!host) return;
    if (!tick) {
      setIfChanged(host, empty("nothing has ticked yet"));
      return;
    }
    if (!mine(tick)) {
      // ``latest.json`` is ONE file per strategy, written on every tick whatever account it ran
      // for, so the heartbeat it holds can be the other account's — and a pipeline read from a
      // paper tick under a live header is the mixing this page refuses. So its gates are not
      // drawn here. The line says which tick was there and when, because "no tick has run" and
      // "the only tick that ran was the other account's" are different states, and the second one
      // is the answer to why this panel is empty. It fills the moment the loop ticks for the
      // account in play.
      const when = new Date(tick.at || "").getTime();
      const age = Number.isNaN(when) ? "" : ` (${shortAge((Date.now() - when) / 1000)})`;
      setIfChanged(host, empty(`nothing has ticked for the ${inPlayEnv()} account yet —`
        + ` the last one ran for ${String(tick.env).toLowerCase()}${age}`));
      return;
    }
    const stoppedAt = GATES.findIndex(
      (gate) => gate.id === (tick.stage || STAGE_BY_ACTION[tick.action] || "")
    );
    const verdict = tick.action === "decided"
      ? "good"
      : (tick.action === "noop" ? "muted" : "bad");
    const rows = GATES.map((gate, i) => {
      // No stage on the record and no verdict to derive one from: say so, rather than pretend the
      // tick walked a pipeline we cannot place it in.
      if (stoppedAt < 0) {
        return `<tr><td class="muted">· ${esc(gate.id)}</td>
          <td class="muted">${esc(gate.asks)}</td><td class="muted">—</td></tr>`;
      }
      if (i < stoppedAt) {
        return `<tr><td class="good">✓ ${esc(gate.id)}</td>
          <td class="muted">${esc(gate.asks)}</td><td class="good">passed</td></tr>`;
      }
      if (i === stoppedAt) {
        return `<tr><td class="${verdict}">${tick.action === "decided" ? "✓" : "✗"} `
          + `${esc(gate.id)}</td><td class="muted">${esc(gate.asks)}</td>
          <td class="${verdict}">${esc(tick.action)}${tick.reason ? ` — ${esc(tick.reason)}` : ""}</td></tr>`;
      }
      return `<tr><td class="muted">· ${esc(gate.id)}</td>
        <td class="muted">${esc(gate.asks)}</td><td class="muted">not reached</td></tr>`;
    }).join("");
    setIfChanged(host, `<table class="lg-table"><thead><tr><th>gate</th><th>what it asks</th>
      <th>the last tick</th></tr></thead><tbody>${rows}</tbody></table>`);
  }

  /* The loop's own state: the countdown in the Loop panel below, and the page's title. The chip and
   * the master switch that used to be rendered here are gone — the switch is the Trading box in the
   * Account card, the same box the dashboard shows, and "is a process running" is what the
   * countdown says a few lines further down. */
  function renderLoopState() {
    renderNextTick();
    document.title = `TRAIDER — log · ${(state.loop || {}).strategy || ""}`.trim();
  }

  /* Fold a panel away — the gesture every collapsible card on this page shares.
   *
   * The head and its own toggle button both call this, and it stops the click from reaching the
   * head when the BUTTON made it, so one press is one toggle. Which body folds is found from the
   * head rather than passed in: a handler holding a map of ids is a list to remember to extend, and
   * a panel added without an entry would silently fold the wrong one.
   *
   * The toggle shows the direction it will go — "+" opens, "−" closes — and its tooltip says the
   * same. Not remembered across loads: no panel in this project is, and a collapse that survives a
   * reload is a page that opens looking broken.
   */
  function toggleCard(ev) {
    if (ev && ev.stopPropagation) ev.stopPropagation();
    const head = ev && ev.currentTarget && ev.currentTarget.closest
      ? ev.currentTarget.closest(".card-head")
      : null;
    const card = head && head.closest ? head.closest(".card") : null;
    const body = card ? card.querySelector(".collapse-body") : null;
    if (!body) return;
    body.hidden = !body.hidden;
    const btn = head.querySelector("button");
    const named = card.querySelector("h2");
    if (btn) {
      btn.textContent = body.hidden ? "+" : "−";
      btn.title = `${body.hidden ? "Expand" : "Collapse"} `
        + (named ? named.textContent.trim().toLowerCase() : "this panel");
    }
  }

  /* Start or stop trading, then re-read the state that changed: arming spawns the loop
   * (``src.web.services.loop_control``), so the read after it is the new state of both. The box,
   * the dialog, the endpoints and the acknowledgement are the dashboard's, because they are not
   * two decisions. */
  async function toggleTrading() {
    if (!state.trading) {
      flashToast("The trading state could not be read — reload the page", "warn");
      return;
    }
    const result = await TraiderSwitch.flip({
      api,
      confirmDialog,
      flashToast,
      trading: (state.trading || {}).trading,
      execution: (state.trading || {}).execution,
    });
    if (!result.wrote) return;
    await loadStatus();
    // The box that IS the switch is redrawn from what its own click changed, rather than left
    // saying "off" until the poll comes round.
    renderBoxes();
    // A started loop holds the lease before it has ticked, and the boundary the countdown needs
    // is written by that first tick — so one more read a moment later is the difference between
    // "starting…" for twenty seconds and a countdown. A single read, not a poll.
    setTimeout(loadStatus, FIRST_TICK_MS);
  }

  /* Move the orders to the other account — the same write, the same confirmation and the same box
   * as the dashboard's Mode box (``flipEnv`` is the shared decision; only ``reload`` is this
   * page's, because the mode scopes EVERY panel here and the box cannot be re-read on its own). */
  async function flipMode() {
    const from = inPlayEnv() || "paper";
    return TraiderSwitch.flipEnv({
      api,
      confirmDialog,
      flashToast,
      env: from === "live" ? "paper" : "live",
      from: from,
      reload: () => loadAll(state.log && state.log.day),
    });
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

  /* Everything the BROKER answers, which is two panels on this page: the accounts and their
   * P&L, and — separately — what they hold and what is working. Same read, same read of the same
   * place; two panels because "what is it worth" and "what is in it" are asked at different
   * times, and the second one is empty most of the time. */
  function renderAccount() {
    const positions = state.positions || { positions: [] };
    const orders = state.orders || { open: [], closed: [], resting: [] };

    // The header names the account the whole page is scoped to, from the same one helper every
    // panel filters with — a second source here could only disagree with them.
    $("lg-env").textContent = [inPlayEnv(), positions.instrument].filter(Boolean).join(" · ");

    renderBoxes();

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

    // Only the in-play account's holdings. ``/positions`` reads BOTH accounts on purpose (a
    // position the bot is not pointed at still refuses an arming, and the dashboard's pill counts
    // both), but a table that mixes them is a table you have to read with a mental filter — and
    // the account is not decoration here, it is the difference between a position this run owns
    // and one it does not. ``mine`` drops the other account; the empty line below says so rather
    // than letting an empty table read as "nothing is held".
    const rows = [];
    const kept = (positions.positions || []).filter(mine);
    for (const account of kept) {
      for (const held of account.positions || []) {
        rows.push({ env: account.env, ...held });
      }
      if (account.known === false) {
        rows.push({ env: account.env, symbol: "—", unknown: true });
      }
    }
    // Only an account with something IN it counts as dropped. ``/positions`` returns an entry per
    // environment whether or not it holds anything, so counting entries would print this note on
    // every single load — a sentence about a filter that removed nothing, which is noise that
    // reads like a warning. An unreadable other account does not count either: the dashboard's
    // panels already treat that as noise rather than a fault, and its verdict is not this page's.
    const elsewhere = (positions.positions || [])
      .filter((account) => !mine(account) && Number(account.count || 0) > 0).length;
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
        : (elsewhere
          ? `nothing is held in the ${inPlayEnv()} account — the ${otherEnv()} account holds `
            + "something, and this page does not show it"
          : "nothing is held")));

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

  // The account IN PLAY, and only it — the same rule as every other panel on the page, through the
  // same helper. Listing both was a mistake this page paid for: with the switch moved to live and
  // no live credentials behind it, the idle PAPER account's equity and cash were the only numbers
  // on the panel, and they read as the live account's. That is a false statement about money, made
  // by layout rather than by data, and no amount of labelling fixes it — the figures simply must
  // not be there.
  //
  // Nothing is lost. An account that cannot be read says why and names the fix, and what any
  // account HOLDS is on the next panel — the part of it that matters, and the part this one never
  // carried. Both accounts are still read from the broker (``/accounts`` returns both) because the
  // dashboard counts positions in both; this page shows one.
  //
  // The three TRADING boxes stand above that name line, in a row of their own: they are about the
  // run rather than about the account's balance, and they are the same three boxes the dashboard's
  // Trading panel shows — same builders, same hints, same click handlers' shape — because the mode
  // and the switch are one decision with one wording.
  function renderBoxes() {
    const payload = state.accounts || { accounts: [] };
    const trading = state.trading || {};
    const inPlay = inPlayEnv();
    const rows = (payload.accounts || []).filter(mine);

    const tradingRow = '<div class="lg-metrics">'
      // Locked while trading is ON, exactly as on the dashboard: the server refuses the write, and
      // a strategy running on one account must not be pointed at the other in flight.
      + TraiderSwitch.envTile(inPlay, !!trading.locked, "flipMode()")
      + TraiderSwitch.tradeTile(state.trading, "toggleTrading()")
      + TraiderSwitch.openTile(trading, inPlay)
      + "</div>";

    const figures = rows.map((row) => {
      const who = [row.env, row.account].filter(Boolean).join(" ");
      const name = `<div class="lg-acct in-play">${esc(who || row.env)}`
        + '<span class="lg-acct-tag">in play</span></div>';
      if (!row.known) {
        // Not a footnote, and never a row of dashes: an unreadable account is not a balance of
        // zero, and it is the reason there are no figures under this name at all. Amber, like
        // every other "this is what stands in the way" line on the page, and it says where the
        // fix is.
        const why = row.reason || "this account could not be read";
        return name
          + `<p class="warn">${esc(why)} — add the ${esc(String(row.env).toUpperCase())} key`
          + " pair in <b>Account Settings</b> on the dashboard and validate it; until then"
          + " nothing can be traded here.</p>";
      }
      const change = Number(row.day_pl);
      const day = row.day_pl === null || row.day_pl === undefined ? "" : signedMoney(row.day_pl);
      const pct = percentText(row.day_pl_pct);
      const cls = change < 0 ? "neg" : (change > 0 ? "pos" : "");
      const status = [row.status, row.blocked ? "BLOCKED" : ""].filter(Boolean).join(" ");
      return name + '<div class="lg-metrics">'
        + TraiderSwitch.tile("Equity", esc(money(row.equity)))
        + TraiderSwitch.tile("Day", esc([day, pct === "—" ? "" : `(${pct})`].filter(Boolean).join(" ")), cls)
        + TraiderSwitch.tile("Cash", esc(money(row.cash)))
        + TraiderSwitch.tile("Buying power", esc(money(row.buying_power)))
        + TraiderSwitch.tile("Status", esc(status), row.blocked ? "neg" : "")
        + "</div>";
    }).join("");

    setIfChanged($("lg-accounts"), tradingRow + (figures || empty(payload.ok === false
      ? `the accounts could not be read (${payload.message || "no reason given"})`
      : `no figures were returned for the ${inPlay || "active"} account`)));
  }

  function renderTicks() {
    const all = (state.log && state.log.ticks) || [];
    const ticks = all.filter(mine);
    $("lg-day").textContent = state.log ? state.log.day : "";
    // The account each tick ran for is a column of its own, and the rows are filtered to the one
    // in play (see ``mine``): a day's file holds both accounts' ticks, and ``reason`` reads the
    // same either way — "the exchange is closed" is true of paper and live alike.
    //
    // The day is not filtered, only the rows: a day when the OTHER account traded is still a day
    // in the menu, and what it shows here is the truth about this account — nothing. The empty
    // line says that, with the count, so it cannot be mistaken for a day the loop never ran.
    setIfChanged($("lg-ticks"), table(
      ["when", "account", "action", "bar", "signal", "reason", "orders", "notes"],
      ticks,
      (tick) => {
        const ids = (tick.order_ids || []).length;
        const cls = tick.action === "refused" ? "bad" : "";
        return `<tr>${cell(stamp(tick.at))}${accountCell(tick.env)}${cell(tick.action, cls)}
          ${cell(tick.bar)}${cell(tick.signal)}${cell(tick.reason)}
          <td>${ids ? `${ids} — ${esc((tick.order_ids || []).join(", "))}` : "—"}</td>
          ${notesCell(tick.notes)}</tr>`;
      }
    ) || empty(all.length - ticks.length
      ? `nothing for the ${inPlayEnv()} account on this day — `
        + `${counted(all.length - ticks.length, "tick")} from the ${otherEnv()} account`
      : "nothing was decided on this day"));
  }

  function renderOrders() {
    const all = (state.log && state.log.orders) || [];
    const orders = all.filter(mine);
    // The account, and the rows filtered to it — the same rule as the ticks above. An order the
    // bot sent to the paper account is not an order it sent for this one, and intent, status and
    // price read identically either way.
    setIfChanged($("lg-orders"), table(
      ["when", "account", "intent", "status", "price", "expected", "bar", "broker id", "client id",
        "why"],
      orders,
      (order) => `<tr>${cell(stamp(order.at))}${accountCell(order.env)}${cell(order.intent)}
        ${cell(order.status, order.status === "rejected" ? "bad" : "")}
        ${cell(money(order.price))}${cell(money(order.expected))}${cell(order.bar)}
        ${cell(order.order_id)}${cell(order.client_order_id)}${whyCell(order)}</tr>`
    ) || empty(all.length - orders.length
      ? `no order for the ${inPlayEnv()} account — ${counted(all.length - orders.length, "order")}`
        + ` from the ${otherEnv()} account`
      : "no order has been submitted yet"));
  }

  // The broker's own words when an order was refused, shown rather than hidden in a tooltip
  // for the same reason the ticks table shows a bar's notes: "why did this not fill" is what
  // someone opens this table to find out.
  function whyCell(order) {
    const detail = String(order.detail || "").trim();
    if (!detail) return "<td>—</td>";
    return `<td class="${order.status === "rejected" ? "bad" : "muted"}">${esc(detail)}</td>`;
  }

  function renderTrades() {
    const all = (state.trades && state.trades.trades) || [];
    const trades = all.filter(mine);
    // The account, and the rows filtered to it: a round trip closed on paper is not one closed
    // with real money, and entry, exit and return look exactly the same.
    setIfChanged($("lg-trades"), table(
      ["closed", "account", "direction", "entry", "exit", "return", "weight", "bars", "reason"],
      trades,
      (trade) => {
        const cls = Number(trade.ret) < 0 ? "bad" : "good";
        return `<tr>${cell(stamp(trade.at))}${accountCell(trade.env)}${cell(trade.direction)}
          ${cell(money(trade.entry_price))}${cell(money(trade.exit_price))}
          ${cell(percent(trade.ret), cls)}${cell(trade.weight)}${cell(trade.bars)}
          ${cell(trade.reason)}</tr>`;
      }
    ) || empty(all.length - trades.length
      ? `no round trip has been closed for the ${inPlayEnv()} account — `
        + `${counted(all.length - trades.length, "trade")} from the ${otherEnv()} account`
      : "no round trip has been closed yet"));
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

  /* Both halves of what the page says about the RUN, in one read: the loop's claim and the switch
   * that arms it. Kept apart from ``loadAll`` so a click on the switch can re-read exactly what it
   * changed without re-reading the day's tables as well. */
  async function loadStatus() {
    try {
      state.loop = await api("/api/v1/loop");
    } catch (err) {
      fail(`could not read the loop's state: ${err.message}`);
      state.loop = null;
    }
    try {
      state.trading = await api("/api/v1/trading");
    } catch (err) {
      // Not ``fail()``: the tables below are still worth reading when the switch cannot be, and
      // the switch says so itself rather than claiming trading is off.
      state.trading = null;
    }
    renderLoopState();
  }

  async function loadAll(day) {
    await loadStatus();
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
  // The Mode and Trading boxes are built by the shared module and handled here, so both handlers
  // have to be reachable from the markup it renders.
  window.toggleTrading = toggleTrading;
  window.flipMode = flipMode;
  window.toggleCard = toggleCard;
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

  /* Is there anything LIVE to keep an eye on? The day's tables do not change on a past day, but
   * the status block does: a loop can die, and the switch can be armed from another tab. That
   * block is what this page now carries, so it is what decides whether the page keeps reading —
   * and with both quiet there is nothing to watch. */
  function statusIsLive() {
    const status = state.loop || {};
    if (status.state === "running" || status.state === "overdue") return true;
    return !!((state.trading || {}).trading || {}).on;
  }

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  // Self-scheduling, so a slow day read delays the next poll instead of stacking one on top.
  function schedulePoll(delay) {
    stopPoll();
    if (document.hidden || !(isToday() || statusIsLive())) return;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (document.hidden) return;
      // A past day cannot gain rows, so on one only the status half is re-read: polling its
      // tables too would be broker traffic for a page nobody is watching change. Today is
      // re-read whole, because its tables do move.
      if (isToday()) await loadAll();
      else await loadStatus();
      schedulePoll(LOG_POLL_MS);
    }, delay === undefined ? LOG_POLL_MS : delay);
  }

  // A hidden tab is nobody watching: stop, and catch up in one go on the way back.
  document.addEventListener("visibilitychange", () => {
    // Both timers follow the tab: a countdown nobody can see, and a read of the loop's records
    // nobody is waiting for, are the two halves of the same waste. Coming back re-reads and
    // restarts the countdown from a fresh boundary.
    if (document.hidden) { stopPoll(); stopCountdown(); }
    else loadAll();
  });
  window.addEventListener("pagehide", () => { stopPoll(); stopCountdown(); });

  // Read once, on load, and then keep today's view current. A day button switches to that
  // day and does not poll; the ↻ button re-reads whatever is on screen.
  loadAll();
})();
