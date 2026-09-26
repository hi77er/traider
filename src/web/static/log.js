/* TRAIDER — the Session monitor.
 *
 * What the loop DID, and the account it did it for. Account state first, local context second.
 * That order is not decoration: the broker is the truth about what is held and what is working,
 * and everything the loop wrote down is the EXPLANATION of it. A page that led with its own
 * records would let a stale or deleted log pass for the state of the account.
 *
 * Nothing here places, changes or cancels an order: the broker is only ever READ. What this page
 * writes is the master switch and the mode, and it does both through ``trading_switch.js`` —
 * starting, stopping and re-pointing the bot are three actions with one wording each, not one per
 * page. This is also the page that carries the trading controls at all: the Strategy lab builds a
 * strategy and never arms one.
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
    // What the dataset says the bars ARE: symbol, bar size, and the zone they are stamped in.
    // Read with the chart, and used by the ticks table too — a `bar` cell names a time, and a
    // time without its zone is a number that means something different in every reader's head.
    dataset: null,
    equity: null,
    // The two screener lists under the day menu, keyed by panel: what came back, and WHEN this
    // page read it — the label beside each ↻ is that read time, and it is the only thing that
    // separates a list screened this morning from one screened last week.
    screens: {},
  };

  // The countdown is the page's one local clock: what it counts to comes from the loop, and it is
  // the only thing here that redraws without a request. Stopped the moment nobody is looking.
  const COUNTDOWN_MS = 1000;
  let countdownTimer = null;
  let countdownUntil = null; // epoch ms of the next wake, or null when nothing is scheduled
  // How long after an arm to look again for the boundary the first tick commits to.
  const FIRST_TICK_MS = 5000;

  /* How much of the loop's history is drawn at once, and which slice of it is on screen.
   *
   * A full session of five-minute bars is 78 ticks and a day file only grows, so the table is
   * paged rather than scrolled: fifteen rows sit under the gates without the reader losing the
   * strip at the top of the panel, which is the thing the table is read against.
   *
   * The page number is remembered while the DAY is the same — a poll that adds a tick must not
   * throw the reader back to the top — and reset when the day changes, because "page 3 of
   * today" is not a place in yesterday. */
  const TICKS_PER_PAGE = 15;
  let tickPage = 1;
  let tickPageDay = null;

  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replace(/[&<>"']/g, (char) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
    ));

  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      // Same reason as the Strategy lab's wrapper: a process running older code answers 404 for
      // an endpoint this page was built against, and "404 Not Found" reads like a typo.
      if (res.status === 404) {
        throw new Error(`${path} is missing (404) — the server is running older code than `
          + "this page, so restart it");
      }
      throw new Error(`${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  /* Transient message, bottom-centre (the Strategy lab's #toast styling, same contract). */
  function flashToast(text, kind) {
    const el = $("toast");
    if (!el) return;
    el.textContent = text;
    el.className = kind || "";
    el.hidden = false;
    clearTimeout(flashToast._timer);
    flashToast._timer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  /* Promise-based confirmation dialog (same contract as the Strategy lab's). The master switch
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

  /* Which prose cells are OPEN, by the ROW KEY they carry rather than by node. The tables are
   * rebuilt whenever a poll brings a new row, and an expansion that lived only in the DOM would
   * snap shut under the reader's eyes — the one moment it is being used for something. Same reason
   * the tick pager remembers its page. */
  const openProse = new Set();

  /* A cell that holds a SENTENCE rather than a value — the broker's refusal, a tick's reason, the
   * loop's notes. It shows its FIRST LINE (the ellipsis the browser draws at the end is the rest of
   * it) and opens on a click: three paragraphs of refusal under every row is a table nobody can
   * read across, and the question the cell answers is only asked sometimes. The truncation itself
   * is CSS (``.lg-table td.prose``), so a cell whose text fits is not a control at all.
   *
   * ``key`` names the ROW: whatever is unique and stable for it — a timestamp, an order id. */
  function proseCell(value, key, cls) {
    const open = Boolean(key) && openProse.has(key);
    return `<td class="prose${cls ? ` ${cls}` : ""}${open ? " open" : ""}"`
      + `${key ? ` data-prose="${esc(key)}"` : ""}>`
      + `<span class="prose-text" tabindex="0">${esc(value)}</span></td>`;
  }

  function toggleProse(target) {
    const cell = target && target.closest ? target.closest("td.prose") : null;
    if (!cell || !cell.dataset.prose) return false;
    const open = cell.classList.toggle("open");
    if (open) openProse.add(cell.dataset.prose);
    else openProse.delete(cell.dataset.prose);
    return true;
  }

  // The ⚠ a tick carries when its bar was odd but usable (`src.data.quality`). An em dash when
  // there is nothing to say, so an empty cell never reads as a value that failed to load —
  // and the text is shown rather than hidden behind a tooltip, because "which bar was strange,
  // and how" is exactly what someone reading this table came to find out.
  function notesCell(notes, key) {
    const list = (notes || []).filter(Boolean);
    if (!list.length) return "<td>—</td>";
    return proseCell(`⚠ ${list.join("; ")}`, key ? `tick:${key}:notes` : null, "warn");
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

  /* Which zone the bars are stamped in, as the dataset reports it. Defaults to UTC rather than
   * to the reader's own zone: the loop writes bar stamps in UTC, so an un-read dataset means
   * "not known yet", and the browser's zone is not a better guess than the one the stamp is
   * actually written in. */
  function marketZone() {
    return (state.dataset && state.dataset.market_timezone) || "UTC";
  }

  /* A BAR cell: "12:25 EDT".
   *
   * The loop records the bar as a UTC stamp with the seconds and the offset on it, and both are
   * facts about how the record is STORED rather than about the bar — every bar stamp is on the
   * minute, and "+00:00" is not a timezone anyone trades in. What the reader wants is the time
   * the market was at and the zone it was in, so the cell is built rather than printed
   * (``ChartTime.stampCell``). Falls back to the raw stamp when the module is not loaded, which
   * is the state a harness that lifts this function out of the file runs in. */
  function barLabel(value) {
    if (typeof ChartTime === "undefined") return value || "—";
    return ChartTime.stampCell(value, marketZone());
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
   * stopped and why, and it does not describe the gates it sailed through. Written as a sentence
   * — the label reads as prose, so it starts like prose. */
  const GATES = [
    { id: "switch", asks: "Is trading on" },
    { id: "armed", asks: "Is this the strategy the switch was armed for" },
    { id: "instrument", asks: "Should the instrument be a different one" },
    { id: "execution", asks: "Could an order be placed at all" },
    { id: "clock", asks: "Is the exchange open" },
    { id: "sync", asks: "Is the dataset synced to now" },
    { id: "window", asks: "Is the trailing window readable, and not behind" },
    { id: "quality", asks: "Is the bar about to be decided on a bar at all" },
    { id: "decide", asks: "Did the strategy decide, and act" },
  ];

  /* Records written before the loop stamped a stage, and hand-built ones: the verdict still says
   * most of it, and a gate derived from the verdict beats a pipeline with no current step. */
  const STAGE_BY_ACTION = {
    off: "switch", closed: "clock", noop: "decide", decided: "decide", refused: "decide",
    switched: "instrument",
  };

  function renderNextTick() {
    const loop = state.loop || {};
    const running = loop.state === "running";
    const stalled = loop.state === "stalled";
    // Only a LIVE loop has a boundary worth counting against. An ``overdue`` claim names one too
    // — the boundary a process committed to before it died — and counting down to that would
    // promise a tick that nothing is going to make. A ``stalled`` loop counts UP from its
    // boundary instead: the same moment read the other way round is how late it is.
    syncCountdown(running || stalled ? loop.next_wake : null, loop.state);
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

  /* Lateness in words, because the line under the clock reads as a sentence: "+00:38:12" is a
   * number, "38 min" is a delay an operator can act on. */
  function lateText(ms) {
    const seconds = Math.max(0, Math.round(ms / 1000));
    if (seconds < 60) return `${seconds} sec`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  // The countdown is the page's only clock of its own, and it is LOCAL: no request, no poll, just
  // arithmetic on the boundary the loop already committed to. It is restarted from each status
  // read, so a loop that moves its own boundary cannot leave this showing an old one.
  // Whether the switch is on. Read through the payload rather than cached: the box that changes it
  // is on this page, and one more read is cheaper than one more piece of state to keep in step.
  // Defined with the countdown because that is where it is first needed — the line under the clock
  // is the one that has to say "armed, and nothing is ticking".
  function tradingArmed() {
    return !!(((state.trading || {}).trading) || {}).on;
  }

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
      // The two ways this page can say "nothing will tick", and which one it is depends on the
      // switch: an armed account waiting for a process that is not there is the failure an
      // operator can do something about, and the loop that DIED is the same news with its cause.
      setIfChanged(when, tradingArmed()
        ? "Trading is armed, but no loop is running — nothing will tick."
        : (loopState === "overdue"
          ? "The loop that claimed it is gone — nothing will tick."
          : "Nothing scheduled — no loop is running."));
      return;
    }
    const left = countdownUntil - Date.now();
    if (loopState === "stalled") {
      // Counting UP, because that number is the whole symptom. A loop that woke for its bar and
      // never finished the tick is not about to tick, and standing at 00:00:00 said "any moment
      // now" — which is exactly the lie that let a wedged loop go unnoticed for a session. The
      // seconds keep moving so it is visibly NOT getting shorter.
      setIfChanged(clock, `+${clockText(-left)}`);
      setIfChanged(when, `It is ${lateText(-left)} past the `
        + `${whenText(new Date(countdownUntil).toISOString())} bar it woke for and has not ticked `
        + "since — the process is stuck inside a tick, and restarting it is the only way it "
        + "ticks again.");
      return;
    }
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

  /* One STEP of the roadmap: the gate's own name inside the chevron, and what it asks plus the
   * tick's answer to it as a label that appears under the pointer.
   *
   * The SHAPE is an inner span and the label is its SIBLING, which is not decoration: the chevron
   * is drawn with ``clip-path``, and a clip-path clips everything INSIDE the element that carries
   * it — a label nested in the shape is a label nobody can see, whatever its computed style says.
   * That was this panel's first version, and the reason hovering showed nothing.
   *
   * The label is a real element rather than a `title` attribute, for the reason this project has
   * already been bitten by: the embedded browser these pages are read in renders no native
   * tooltip, so a title-only explanation is one nobody ever sees. It is focusable too, so the
   * keyboard reaches the same text.
   */
  /* The label reads as prose, so it starts like prose. The reason after the dash is the loop's own
   * wording and is left exactly as it was written — it is quoted, not rewritten. */
  const sentence = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

  function gateStep(gate, state_, tick, verdict) {
    // A switch is a tick that DID something, like a decision: not a failure because it did not
    // reach the decision itself.
    const acted = tick.action === "decided" || tick.action === "switched";
    const mark = state_ === "passed" ? "\u2713"
      : state_ === "stop" ? (acted ? "\u2713" : "\u2717")
        : "\u00b7";
    const answer = state_ === "passed" ? "Passed"
      : state_ === "stop" ? `${sentence(tick.action)}${tick.reason ? ` \u2014 ${tick.reason}` : ""}`
        : state_ === "unreached" ? "Not reached"
          : "\u2014";
    const cls = state_ === "stop" ? verdict : state_;
    return `<div class="lg-step ${cls}" tabindex="0">
      <span class="lg-step-body">
        <span class="lg-step-mark">${mark}</span>
        <span class="lg-step-name">${esc(gate.id)}</span>
      </span>
      <span class="lg-step-help" role="tooltip">
        <span class="lg-step-asks">${esc(gate.asks)}</span>
        <span class="lg-step-answer">${esc(answer)}</span>
      </span>
    </div>`;
  }

  /* The pipeline, as the last tick walked it: the gates it passed, the one that ended it with the
   * loop's own reason, and the ones it never reached.
   *
   * Drawn as a ROADMAP rather than a table: the gates are an ORDER — a tick stops at the first
   * that says no, so everything after it never ran — and eight rows of three columns never said
   * that. A row of chevrons does, and what each gate ASKS moves into the step's hover label,
   * because a question is what you want when you are looking at one step, not eight lines you
   * read past to reach the one that stopped. */
  function renderGates(tick) {
    const host = $("lg-gates");
    if (!host) return;
    if (!tick) {
      // A tick FROM ANOTHER STRATEGY is not a missing tick: the page is scoped to one strategy,
      // and saying "nothing has ticked yet" for a strategy whose log is simply elsewhere is a
      // sentence about the wrong thing. The name is in the line because that is the answer to
      // "whose log is this" — the panel it sits over belongs to the strategy above it.
      const name = (state.loop && state.loop.strategy) || "";
      setIfChanged(host, empty(name ? `${name} has not ticked yet` : "nothing has ticked yet"));
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
    const verdict = (tick.action === "decided" || tick.action === "switched")
      ? "good"
      : (tick.action === "noop" ? "muted" : "bad");
    const steps = GATES.map((gate, i) => {
      // No stage on the record and no verdict to derive one from: say so, rather than pretend the
      // tick walked a pipeline we cannot place it in.
      if (stoppedAt < 0) return gateStep(gate, "unknown", tick, verdict);
      if (i < stoppedAt) return gateStep(gate, "passed", tick, verdict);
      if (i === stoppedAt) return gateStep(gate, "stop", tick, verdict);
      return gateStep(gate, "unreached", tick, verdict);
    }).join("");
    // The one thing that does not wait for a hover: the step that STOPPED the tick, in the loop's
    // own words. Its verdict is the news on this panel — everything else is a gate it sailed
    // through — so it is printed under the strip rather than hidden in a label. A tick that
    // decided sits with no caption: the whole strip is green, which is the answer.
    const why = stoppedAt >= 0 && tick.action !== "decided"
      ? `<p class="lg-roadmap-why ${verdict}">${esc(GATES[stoppedAt].id)} · `
        + `${esc(tick.action)}${tick.reason ? ` — ${esc(tick.reason)}` : ""}</p>`
      : "";
    setIfChanged(host, `<div class="lg-roadmap" role="list">${steps}</div>${why}`);
  }

  /* The loop's own state: the countdown in the Loop panel below, and the page's title. The chip and
   * the master switch that used to be rendered here are gone — the switch is the Trading box in the
   * Account card, and "is a process running" is what the countdown says a few lines further down. */
  function renderLoopState() {
    renderNextTick();
    document.title = `TRAIDER — Session monitor · ${(state.loop || {}).strategy || ""}`.trim();
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
    // The head's OWN title names the panel, falling back to the card's: the Top-10 is a section of
    // the day-menu card, and reading the card's first ``<h2>`` there would call it "days".
    const named = head.querySelector("h2") || card.querySelector("h2");
    if (btn) {
      btn.textContent = body.hidden ? "+" : "−";
      btn.title = `${body.hidden ? "Expand" : "Collapse"} `
        + (named ? named.textContent.trim().toLowerCase() : "this panel");
    }
  }

  /* Start or stop trading, then re-read the state that changed: arming spawns the loop
   * (``src.web.services.loop_control``), so the read after it is the new state of both. The box,
   * the dialog, the endpoints and the acknowledgement come from the shared module, because they
   * are not two decisions. */
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
      // What is open, from the SAME read the Open box is drawn from: stopping asks about it, and a
      // dialog quoting a different number than the box beside it would be worse than no dialog.
      open: state.trading || {},
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
   * the lab's Mode box used to offer (``flipEnv`` is the shared decision; only ``reload`` is this
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
    const name = (state.log && state.log.strategy) || (state.loop || {}).strategy || "";
    if (!days.length) {
      $("lg-days").innerHTML = empty(name
        ? `no day has been recorded for ${name} yet`
        : "no day has been recorded yet");
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
    // position the bot is not pointed at still refuses an arming, and the lab's Open box counts
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
    // reads like a warning. An unreadable other account does not count either: the Strategy lab's
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

    // EVERY working order in the account, not just this instrument's: an order resting for a symbol
    // this run does not trade is still working in the account, and hiding it is how a panel called
    // "what the broker holds" comes to be wrong about what the broker holds. The symbol is a column
    // for that reason. The PROTECTION verdict is a different question and stays this instrument's
    // (the route filters the legs it hands it).
    const working = [...(orders.open || []), ...(orders.resting || [])];
    setIfChanged($("lg-working"), orders.ok === false
      ? empty("the broker could not be read, so nothing is known about working orders")
      : (table(
        ["symbol", "id", "client id", "side", "type", "qty", "filled", "avg price", "stop",
          "limit", "submitted", "status"],
        working,
        (order) => `<tr>${cell(order.symbol)}${cell(order.id)}${cell(order.client_order_id)}
          ${cell(order.side)}${cell(order.type)}${cell(order.qty)}${cell(order.filled_qty)}
          ${cell(money(order.filled_avg_price))}${cell(money(order.stop_price))}
          ${cell(money(order.limit_price))}${cell(stamp(order.submitted_at))}
          ${cell(order.status)}</tr>`
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
  // lab counts positions in both; this page shows one.
  //
  // The three TRADING boxes stand above the figures, in a row of their own: they are about the
  // run rather than about the account's balance, and they are built by the shared module — same
  // builders, same hints, same click handlers' shape as the boxes the lab draws for signals and
  // risk — because the mode and the switch are one decision with one wording.
  function renderBoxes() {
    const payload = state.accounts || { accounts: [] };
    const trading = state.trading || {};
    const inPlay = inPlayEnv();
    const rows = (payload.accounts || []).filter(mine);

    const tradingRow = '<div class="lg-metrics">'
      // Locked while trading is ON, exactly as on the lab: the server refuses the write, and
      // a strategy running on one account must not be pointed at the other in flight.
      + TraiderSwitch.envTile(inPlay, !!trading.locked, "flipMode()")
      + TraiderSwitch.tradeTile(state.trading, "toggleTrading()")
      + TraiderSwitch.openTile(trading, inPlay)
      + "</div>";

    const figures = rows.map((row) => (row.known ? accountFigures(row) : accountTrouble(row)))
      .join("");

    setIfChanged($("lg-accounts"), tradingRow + (figures || empty(payload.ok === false
      ? `the accounts could not be read (${payload.message || "no reason given"})`
      : `no figures were returned for the ${inPlay || "active"} account`)));
  }

  /* An account the broker answered for: its figures, and nothing else. */
  function accountFigures(row) {
    const change = Number(row.day_pl);
    const day = row.day_pl === null || row.day_pl === undefined ? "" : signedMoney(row.day_pl);
    const pct = percentText(row.day_pl_pct);
    const cls = change < 0 ? "neg" : (change > 0 ? "pos" : "");
    const status = [row.status, row.blocked ? "BLOCKED" : ""].filter(Boolean).join(" ");
    return '<div class="lg-metrics">'
      + TraiderSwitch.tile("Equity", esc(money(row.equity)))
      + TraiderSwitch.tile("Day", esc([day, pct === "—" ? "" : `(${pct})`].filter(Boolean).join(" ")), cls)
      + TraiderSwitch.tile("Cash", esc(money(row.cash)))
      + TraiderSwitch.tile("Buying power", esc(money(row.buying_power)))
      + TraiderSwitch.tile("Status", esc(status), row.blocked ? "neg" : "")
      + "</div>";
  }

  /* An account nothing could be read from, and what the reader is supposed to DO about it.
   *
   * Not a footnote, and never a row of dashes: an unreadable account is not a balance of zero,
   * and it is the reason there are no figures here at all. Amber, like every other "this is what
   * stands in the way" line on the page.
   *
   * But the two ways this happens are NOT the same news, and one sentence for both is a lie half
   * the time. The server says which it was (``fix``): a key that is missing or was refused is the
   * reader's to fix, and they are told where; a broker that timed out or answered 5xx is not —
   * sending an operator to re-enter the keys they already entered, over Alpaca's gateway hiccup,
   * is a wild goose chase with a real cost, because the panel is where they came to see whether
   * anything is trading. */
  function accountTrouble(row) {
    const why = row.reason || "this account could not be read";
    const env = String(row.env || "").toUpperCase();
    if (row.fix === "credentials") {
      return `<p class="warn">${esc(why)} — add the ${esc(env)} key pair in`
        + " <b>Account Settings</b> on the Strategy lab and validate it; until then nothing can"
        + " be traded here.</p>";
    }
    return `<p class="warn">${esc(why)} — the ${esc(env)} key pair is configured and the broker`
      + " did not answer, so there is nothing here for you to fix: the panel asks again every 30"
      + " seconds and the figures appear as soon as Alpaca does.</p>";
  }

  /* The pager under a long table: which slice of the day is on screen, and the two ways off it.
   *
   * The COUNT is the useful half — "16–30 of 43" says where the reader is and how much there is
   * — and the two buttons are disabled at the ends rather than hidden, so the control does not
   * change shape under the pointer as they walk the day. Rows are newest first, so the buttons
   * are named for the direction of the TICKS and not for the page numbers.
   *
   * Nothing at all is drawn for a day that fits on one page: pagination with one page is a
   * label with two dead buttons under it. */
  function pager(total, pages, from, count) {
    if (pages <= 1) return "";
    return `<div class="lg-pager">
      <button class="ghost small" onclick="goTickPage(-1)"${tickPage <= 1 ? " disabled" : ""}
        title="The ticks after these">‹ Newer</button>
      <button class="ghost small" onclick="goTickPage(1)"${tickPage >= pages ? " disabled" : ""}
        title="The ticks before these">Older ›</button>
      <span class="lg-pager-count">${from + 1}–${from + count} of ${total}</span>
      <span class="lg-pager-page">page ${tickPage} / ${pages}</span>
    </div>`;
  }

  function goTickPage(step) {
    tickPage += step;
    renderTicks();  // which clamps the number to the pages this day actually has
  }

  function renderTicks() {
    const all = (state.log && state.log.ticks) || [];
    const ticks = all.filter(mine);
    $("lg-day").textContent = state.log ? state.log.day : "";
    // A page number belongs to the day it was chosen on. Kept while the day is the same, so a
    // poll that appends a tick leaves the reader where they were reading.
    const day = (state.log && state.log.day) || "";
    if (day !== tickPageDay) {
      tickPageDay = day;
      tickPage = 1;
    }
    const pages = Math.max(1, Math.ceil(ticks.length / TICKS_PER_PAGE));
    // Clamped rather than trusted: a shorter day (or a smaller filter) can leave the remembered
    // page past the end, and an empty table with a live pager under it reads as a load failure.
    tickPage = Math.min(Math.max(1, tickPage), pages);
    const from = (tickPage - 1) * TICKS_PER_PAGE;
    const shown = ticks.slice(from, from + TICKS_PER_PAGE);
    // The account each tick ran for is a column of its own, and the rows are filtered to the one
    // in play (see ``mine``): a day's file holds both accounts' ticks, and ``reason`` reads the
    // same either way — "the exchange is closed" is true of paper and live alike.
    //
    // The day is not filtered, only the rows: a day when the OTHER account traded is still a day
    // in the menu, and what it shows here is the truth about this account — nothing. The empty
    // line says that, with the count, so it cannot be mistaken for a day the loop never ran.
    const body = table(
      ["when", "account", "action", "bar", "signal", "reason", "orders", "notes"],
      shown,
      (tick) => {
        const ids = (tick.order_ids || []).length;
        const cls = tick.action === "refused" ? "bad" : "";
        return `<tr>${cell(stamp(tick.at))}${accountCell(tick.env)}${cell(tick.action, cls)}
          ${cell(barLabel(tick.bar))}${cell(tick.signal)}${proseCell(tick.reason, `tick:${tick.at}:reason`)}
          <td>${ids ? `${ids} — ${esc((tick.order_ids || []).join(", "))}` : "—"}</td>
          ${notesCell(tick.notes, tick.at)}</tr>`;
      }
    );
    setIfChanged($("lg-ticks"), body
      ? body + pager(ticks.length, pages, from, shown.length)
      : empty(all.length - ticks.length
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
        ${cell(money(order.price))}${cell(money(order.expected))}${cell(barLabel(order.bar))}
        ${cell(order.order_id)}${cell(order.client_order_id)}${whyCell(order)}</tr>`
    ) || empty(all.length - orders.length
      ? `no order for the ${inPlayEnv()} account — ${counted(all.length - orders.length, "order")}`
        + ` from the ${otherEnv()} account`
      : "no order has been submitted yet"));
  }

  // The broker's own words when an order was refused, shown rather than hidden in a tooltip
  // for the same reason the ticks table shows a bar's notes: "why did this not fill" is what
  // someone opens this table to find out. Collapsed to a line like every other sentence here —
  // this one runs to three — and opened with a click.
  function whyCell(order) {
    const detail = String(order.detail || "").trim();
    if (!detail) return "<td>—</td>";
    const key = order.client_order_id || order.order_id || order.at;
    return proseCell(detail, `order:${key}`, order.status === "rejected" ? "bad" : "muted");
  }

  function renderTrades() {
    const all = (state.trades && state.trades.trades) || [];
    const trades = all.filter(mine);
    // The account, and the rows filtered to it: a round trip closed on paper is not one closed
    // with real money, and entry, exit and return look exactly the same.
    setIfChanged($("lg-trades"), table(
      ["closed", "account", "direction", "entry", "exit", "return", "P/L", "weight", "bars", "reason"],
      trades,
      (trade) => {
        const cls = Number(trade.ret) < 0 ? "bad" : "good";
        // Money, where the record knows the size the broker filled; a dash where it does not —
        // every row written before the size was recorded has no P/L, and a guess would be worse
        // than a dash. The colours follow the money, not the return: they disagree whenever the
        // size is not what the weight implied.
        const pl = trade.pnl;
        const plCls = pl === null || pl === undefined || pl === ""
          ? "muted" : (Number(pl) < 0 ? "bad" : "good");
        return `<tr>${cell(stamp(trade.at))}${accountCell(trade.env)}${cell(trade.direction)}
          ${cell(money(trade.entry_price))}${cell(money(trade.exit_price))}
          ${cell(percent(trade.ret), cls)}${cell(signedMoney(pl), plCls)}
          ${cell(trade.weight)}${cell(trade.bars)}
          ${proseCell(trade.reason, `trade:${trade.at}:reason`)}</tr>`;
      }
    ) || empty(all.length - trades.length
      ? `no round trip has been closed for the ${inPlayEnv()} account — `
        + `${counted(all.length - trades.length, "trade")} from the ${otherEnv()} account`
      : "no round trip has been closed yet"));

    // The equity pane answers to THIS list, and this is the first moment the answer is known: the
    // session was drawn before the trades were read. If the pane has just appeared, its curve is
    // fetched now rather than left blank until the next poll.
    if (showEquityPane()) loadEquity();
  }

  /* ---------- the session chart: the bars, and the account's equity under them ----------
   *
   * Two panes over one time axis. The price pane is the instrument the loop was deciding about —
   * bars with their volume, so a tick's bar can be found and read against what it did. The pane
   * under it is the ACCOUNT: what it was worth through the session, as the BROKER recorded it
   * (``/api/v1/accounts/history``, Alpaca's portfolio history). Nothing here is reconstructed
   * from this page's own readings, for the same reason the account panel reads the broker rather
   * than the loop's files: a page that samples a balance only knows the moments it was open.
   *
   * Both panes are drawn for the day the page is showing. Zoom, and the other follows — by TIME,
   * not by index: the two series do not start at the same bar (the equity series marks the whole
   * session including the hours before the first stored bar), so an index link would slide them
   * apart the further you zoomed.
   */
  const CHART_HEIGHT = { price: 300, equity: 150, osc: 100 };

  const charts = {
    price: null, equity: null, candles: null, volume: null, line: null,
    // The signals' own series: the invisible row at the top of the pane the arrows are attached to,
    // its current value, and the frame in which it is placed a second time (the scale settles on
    // the library's own animation frame).
    signals: null, signalRowValue: null, signalFrame: null,
    day: null, bars: 0, points: 0,
    // The indicator series drawn ON the price pane, so a toggle can take them off it again,
    // the bars currently drawn, by the library's own time value (what a marker has to match), and
    // those bars IN ORDER — the row wants one point on each of them, and a Set has no order.
    overlays: [], barTimes: new Set(), barOrder: [],
  };

  // The indicator panes under the price chart: ``[{key, chart, points}]``. Rebuilt with the day,
  // with the toggle, and never built at all when their host has no width (the library measures its
  // container as the chart is created — see ``buildEquityPane``).
  const oscPanes = [];

  // Whether the equity pane was on screen at the last look. What matters is the CHANGE: the
  // pane appears with the first closed round trip, and the curve is fetched on that turn.
  let equityShown = false;
  // The reader's own answer about the account's pane (see ``equityWanted``): null until they touch
  // the chip, so the page's rule about when a curve is worth showing is the default.
  let equityChoice = null;
  // Panes with a range subscription on them, so ``linkPanes`` can be called again when the
  // account's pane is built later without double-subscribing the price one.
  const linkedPanes = new WeakSet();
  // The account's pane is built before its curve is read, and a day change rebuilds the bars under
  // a pane that is already there: either way it has to be put on the bars' stretch ONCE, and not
  // again on every poll (that would undo the window the reader set on it).
  let equityNeedsAlign = false;

  function chartOptions(height, withTimeAxis) {
    const interval = (state.dataset && state.dataset.interval) || "";
    return {
      height,
      layout: { background: { color: "transparent" }, textColor: "#8a93a6" },
      grid: { vertLines: { color: "#22262f" }, horzLines: { color: "#22262f" } },
      // The top margin is what the signal row hangs in, so it is WRITTEN DOWN rather than
      // inherited: these are the library's own defaults, and a pane that quietly lost its top
      // strip would put the arrows through the candles. The shape of the band does not matter —
      // the row is placed in pixels (see ``SIGNAL_ROW_Y``) — only that there is one, at every
      // zoom and every window size.
      rightPriceScale: {
        borderColor: "#333a46",
        scaleMargins: { top: 0.2, bottom: 0.1 },
      },
      timeScale: Object.assign(
        { borderColor: "#333a46", minBarSpacing: ChartZoom.MIN_BAR_SPACING, visible: withTimeAxis },
        typeof ChartTime === "undefined" ? {} : ChartTime.timeScaleOptions(interval, marketZone())
      ),
      localization: typeof ChartTime === "undefined" ? {} : ChartTime.localizationOptions(marketZone()),
      // The Strategy lab's wheel policy, from the same module: a plain wheel pans, only a pinch
      // zooms, and the zoom stops at this chart's own data. The library's own pinch is OFF with
      // it — see chart_zoom.js: two zoomers on one ctrl+wheel is what made a flick collapse the
      // view, and only this one is bounded and clamped to the data.
      handleScroll: { mouseWheel: true },
      handleScale: { mouseWheel: false, pinch: false, axisPressedMouseMove: { time: false, price: true } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    };
  }

  /* Rebuild the price pane. Called when the DAY changes and on the first draw: a chart is cheap
   * to create and the alternative — keeping one alive across days — would have to reason about
   * which series a redraw belongs to. The account's pane is built separately, and last. */
  function buildCharts() {
    const priceHost = $("lg-chart");
    const eqHost = $("lg-equity");
    if (!priceHost || !eqHost) return;
    if (typeof LightweightCharts === "undefined") {
      setNote("lg-chart-note", "the chart library did not load — check the network, then reload");
      return;
    }
    if (charts.price) charts.price.remove();
    if (charts.equity) charts.equity.remove();
    // The price chart is gone, so the indicator series on it are too — the read-outs are drawn
    // again onto the new one by the draw that follows this. The signal row goes with them: it is a
    // series like any other, and its place on the new pane is not the old one's.
    charts.overlays = [];
    charts.signals = null;
    charts.signalRowValue = null;
    charts.equity = null;
    charts.line = null;
    priceHost.innerHTML = "";
    eqHost.innerHTML = "";

    // The upper pane owns the time axis; the lower one shares its scale but not its labels, so
    // the two do not print the same row of times twice.
    charts.price = LightweightCharts.createChart(priceHost, chartOptions(CHART_HEIGHT.price, true));

    charts.candles = charts.price.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
      wickUpColor: "#26a69a", wickDownColor: "#ef5350", priceLineVisible: false,
    });
    // Volume lives in the bottom fifth of the price pane, on its own scale: it is a different
    // quantity from price, and one scale for both would draw every bar to the same height.
    charts.volume = charts.price.addHistogramSeries({
      priceScaleId: "volume", priceLineVisible: false, lastValueVisible: false,
      priceFormat: { type: "volume" },
    });
    charts.price.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    // The volume read-out follows the crosshair: the amount of the bar under the pointer, over
    // that bar's column (see chart_volume.js, shared with the Strategy lab's main chart).
    if (typeof ChartVolume !== "undefined") {
      ChartVolume.attach({
        chart: charts.price, series: charts.volume,
        host: priceHost, label: $("lg-vol-label"),
      });
    }

    ChartZoom.bind(priceHost, () => ({ chart: charts.price, barCount: charts.bars }));
    // The signal row is a PRICE and the pane's prices move under it: panning, pinching, or the
    // first fit of the day's bars all put the top of the pane somewhere else, so the row is placed
    // again on every change of the visible range rather than only when the read-outs are redrawn.
    charts.price.timeScale().subscribeVisibleTimeRangeChange(() => {
      if (signalsWanted()) drawSignalMarkers();
    });
    linkPanes();
    // A rebuild happens on a day change, and the pane may well be on screen already.
    buildEquityPane();
  }

  /* Build the account's pane — the one thing here that cannot be done while the panel is HIDDEN.
   *
   * The library measures its container as the chart is created, so a chart created inside a
   * hidden panel comes up with a canvas 0px wide, and the pinned build has no way back: neither
   * ``resize(width, height)`` nor ``applyOptions({ width })`` brings it back, both measured in
   * the browser rather than assumed (4.1.3, and it is also why the pane appeared as an empty
   * box the first time it was revealed). So it is built when it can be seen, which — since the
   * pane only exists once a round trip has closed — is the turn it appears.
   */
  function buildEquityPane() {
    const host = $("lg-equity");
    const panel = $("lg-equity-panel");
    if (!host || charts.equity) return false;
    if (panel && panel.hidden) return false;
    if (!host.clientWidth || typeof LightweightCharts === "undefined") return false;

    charts.equity = LightweightCharts.createChart(host, chartOptions(CHART_HEIGHT.equity, false));
    charts.line = charts.equity.addLineSeries({
      color: "#4c8dff", lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
    });
    ChartZoom.bind(host, () => ({ chart: charts.equity, barCount: charts.points }));
    linkPanes();
    // Built before its curve is read, so the stretch it should be showing is settled by
    // ``drawEquity`` — the one moment the pane has points to be put on a window.
    equityNeedsAlign = true;
    return true;
  }

  /* Keep the panes that EXIST on the same stretch of time.
   *
   * Two guards, and both are load-bearing rather than defensive:
   *
   * * a pane with NO DATA reports a null range, and pushing that onto the other pane throws
   *   inside the library ("Value is null"). The equity pane is empty until the broker answers,
   *   which is the state this panel starts in — the price chart came up blank because of it;
   * * our own push comes back as a change event, so echoing it would hand the range back and
   *   forth on every mouse move.
   *
   * Subscribed once per pane, and callable whenever another one is built: they are not all built at
   * the same moment any more (see ``buildEquityPane``), and the indicator panes join later still. */
  const pushedRanges = new WeakMap();

  /* Which pane the READER is working in. A pane's range also changes when we push one onto it,
   * and when the layout catches up around it; only a gesture in that pane may move the others. */
  const paneTouchedAt = new WeakMap();
  const PANE_TOUCH_WINDOW_MS = 400;

  function markPaneInteraction(pane, el) {
    if (!el) return;
    const mark = () => paneTouchedAt.set(pane, Date.now());
    ["wheel", "pointerdown", "pointermove", "touchstart", "touchmove"].forEach((type) => {
      el.addEventListener(type, mark, { passive: true });
    });
  }

  function userTouchedPane(pane) {
    return Date.now() - (paneTouchedAt.get(pane) || 0) <= PANE_TOUCH_WINDOW_MS;
  }

  /* A window of TIME as a pane's own logical indices.
   *
   * A time range cannot express the empty space beyond a pane's last point, and the library CLAMPS
   * one that reaches past its data — which is why handing a pane a window as a time range changed
   * that pane's SCALE: dragging an indicator pane took the price chart from 171 bars to 161 and the
   * account's pane from 132 to 65, and dragging the account's pane took the price chart from 129 to
   * 96 (measured in the browser). Its own indices can go past the ends, so a pane given a window in
   * its own units keeps its scale and shows the same stretch of the day — the empty space included.
   * Between two of a pane's own points the index is interpolated; outside them it is extrapolated
   * on that end's own spacing, which is what makes the whitespace come out the same shape. */
  function ownIndex(times, time) {
    const n = times.length;
    if (time <= times[0]) {
      return (time - times[0]) / ((times[1] - times[0]) || 1);
    }
    if (time >= times[n - 1]) {
      return (n - 1) + (time - times[n - 1]) / ((times[n - 1] - times[n - 2]) || 1);
    }
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (times[mid] <= time) lo = mid; else hi = mid;
    }
    return lo + (time - times[lo]) / ((times[hi] - times[lo]) || 1);
  }

  function ownRange(times, from, to) {
    if (!times || times.length < 2 || !(from < to)) return null;
    const a = ownIndex(times, from);
    const b = ownIndex(times, to);
    // Times the library reports as dates (a calendar bar size) cannot be interpolated, and a range
    // the library cannot use would throw inside the gesture that caused it.
    if (!isFinite(a) || !isFinite(b) || !(b > a)) return null;
    return { from: a, to: b };
  }

  /* ...and the other way round: the TIME a pane's own index sits at, extrapolated past either end
   * by that end's own spacing. A window panned into the empty space beside the data has to be read
   * this way — the library's own time range STOPS at the last point, so taking the window from that
   * would hand on a shorter one and every pane that took it would shrink rather than move. */
  function ownTime(times, index) {
    if (!times || times.length < 2 || !isFinite(index)) return NaN;
    const n = times.length;
    if (index <= 0) return times[0] + index * ((times[1] - times[0]) || 1);
    if (index >= n - 1) return times[n - 1] + (index - (n - 1)) * ((times[n - 1] - times[n - 2]) || 1);
    const lo = Math.floor(index);
    return times[lo] + (index - lo) * ((times[lo + 1] - times[lo]) || 1);
  }

  /* Every chart this page draws, with a LIVE count of the points on it. The list is built afresh
   * on each call: the indicator panes come and go with the day and with the toggle, and a pane
   * built while the price chart had no bars yet must not be written off for the session. */
  function paneList() {
    return [
      { chart: charts.price, el: $("lg-chart"), count: () => charts.bars,
        times: () => charts.barOrder },
      ...oscPanes.map((pane) => ({ chart: pane.chart, el: pane.canvas, count: () => pane.points,
        times: () => pane.times })),
      { chart: charts.equity, el: $("lg-equity"), count: () => charts.points,
        times: () => charts.pointTimes },
    ].filter((entry) => entry.chart);
  }

  /* Put ONE pane on the stretch of the day the bars are showing, in that pane's own indices.
   *
   * A pane that has just been built has missed all the moving and zooming the others have been
   * through, and the account's pane in particular comes up on its OWN window — the curve covers
   * more of the day than the session's bars do, so left alone it draws a different stretch under
   * the same time axis, at a different scale, and the first pan of any other pane would then
   * re-scale it to match. Said as a push of ours, so the pane's own answer is not echoed onwards. */
  function showWhatTheBarsShow(pane) {
    const entry = paneList().filter((candidate) => candidate.chart === pane)[0];
    const shown = charts.price && charts.price.timeScale().getVisibleLogicalRange();
    if (!entry || !shown) return false;
    const from = ownTime(charts.barOrder, shown.from);
    const to = ownTime(charts.barOrder, shown.to);
    const own = ownRange(entry.times && entry.times(), from, to);
    if (!own) return false;
    pushedRanges.set(pane, { from: from, to: to });
    pane.timeScale().setVisibleLogicalRange(own);
    return true;
  }

  function linkPanes() {
    paneList().forEach((entry) => {
      const pane = entry.chart;
      if (linkedPanes.has(pane)) return;
      linkedPanes.add(pane);
      markPaneInteraction(pane, entry.el);
      pane.timeScale().subscribeVisibleTimeRangeChange((range) => {
        if (!range || range.from === null || range.from === undefined
          || range.to === null || range.to === undefined) return;
        if (!entry.count()) return;  // nothing drawn: it has no range worth sharing
        // ANY change that follows our own push IS that push coming back — including the case where
        // the pane hands back something OTHER than the range it was asked for. A pane holding less
        // data than the one that asked always does: a TIME range cannot even express the empty
        // space a drag past the last bar is looking at, so the account's pane, whose 134 points
        // cover a shorter stretch, answers the price pane's window with a narrower one. Passing
        // that on as though the reader had asked for it is what zoomed the chart the moment a drag
        // reached the end of the data (168 bars became 88 mid-drag, "to" pinned at the last bar).
        if (pushedRanges.has(pane)) {
          pushedRanges.delete(pane);
          if (!userTouchedPane(pane)) return;
        }
        // The price pane leads; a pane below it leads only while the reader is working in that
        // one. A pane whose range moved for any other reason is not the reader moving anything.
        if (pane !== charts.price && !userTouchedPane(pane)) return;
        // The window as TIMES, empty space included: read from this pane's own indices rather than
        // from the range the library reported, which stops at its last point.
        const sourceTimes = entry.times && entry.times();
        const shown = pane.timeScale().getVisibleLogicalRange();
        const from = shown ? ownTime(sourceTimes, shown.from) : NaN;
        const to = shown ? ownTime(sourceTimes, shown.to) : NaN;
        if (!(to > from)) return;
        paneList().forEach((other) => {
          if (other.chart === pane) return;
          if (!other.count()) return;  // and a pane with no data cannot take one
          pushedRanges.set(other.chart, { from: from, to: to });
          const scale = other.chart.timeScale();
          const own = ownRange(other.times && other.times(), from, to);
          // A pane whose times cannot be interpolated (a calendar bar size reports dates) keeps
          // the old time range: it is clamped, but it is still the same stretch of the day.
          if (own) scale.setVisibleLogicalRange(own);
          else scale.setVisibleRange({ from: range.from, to: range.to });
        });
      });
    });
  }

  /* ---------- the chart's read-outs: the strategy's indicators, and its signals ----------
   *
   * CHIPS above the chart, one per indicator the strategy's RULES test — not one per feature it
   * happens to have configured. A chart is read against the rules that are trading, and an
   * indicator nothing tests is a picture without a question. The server says which those are
   * (``used`` on each overlay in the bundle: the same feature columns the rules name), and every
   * chip starts ON. Each draws its indicator where that indicator belongs: the price-scaled ones
   * as series on the candles, the rest in a pane of their own below.
   *
   * The bundle is the lab's endpoint, read once per page — the same configuration, the same bars,
   * so the two charts cannot disagree about what the strategy sees. The absolute-volume indicator
   * is left out of the panes on purpose: this chart's own volume pane IS that series.
   *
   * The SIGNALS checkbox marks the signals the LOOP generated that were not a hold. They are read
   * from the day's own tick records rather than recomputed here — the loop's account of what it
   * decided is the thing worth drawing — and the tick also says whether anything came of it: an
   * order that was submitted (or a round trip closed) is a FILLED signal and gets the trade's
   * colour, while one that never became an order is grey. That is the distinction a chart of a live
   * session is looked at for: not "what did it say", but "what did it say that anything happened
   * on". */
  let indicators = null;          // the strategy's indicator bundle, or null until read
  let indicatorOff = {};          // chip key -> the reader switched it OFF (absent means ON)

  const MARKER_COLOURS = {
    buyFilled: "#26a69a", sellFilled: "#ef5350",
    buyOpen: "#8a93a6", sellOpen: "#8a93a6",
  };

  function signalsWanted() {
    const box = $("lg-signals-toggle");
    return !!(box && box.checked);
  }

  /* The SIGNAL ROW: one line of arrows along the TOP of the price pane.
   *
   * A signal is an event on a bar, not a price — and drawn on the candle it belongs to, as it was,
   * the arrows sat in the middle of the chart: over the very bars they are about, at a height that
   * moved with the day's range. So they are lifted onto a row of their own at the top edge, every
   * one of them pointing DOWN at the bar it belongs to. The session then reads as one timeline of
   * decisions with the candles left alone underneath, and the colour still carries the two things
   * that are the point of the read-out: which side, and whether anything came of it.
   *
   * The row is PIXELS from the top, not a price. The band it sits in is the empty strip the pane
   * already keeps above the highest candle — the price scale's own top margin, which is a share of
   * the pane and so holds at every zoom, on every day, and after any resize. A margin in PRICE
   * would be worse than this one in every way: it would have to be re-chosen per instrument, and
   * the series carrying it would be measured against the autoscale it was meant to stay outside
   * of. So the row's VALUE is whatever price falls at ``SIGNAL_ROW_Y`` — ``coordinateToPrice``,
   * the library's own conversion, which is what keeps the arrows in step with the scale in front
   * of them. */
  const SIGNAL_ROW_Y = 14;   // pixels from the top of the pane: where the arrows are pinned

  /* The series the arrows hang on: a flat line that draws nothing, with one point per drawn bar.
   *
   * Invisible for the obvious reason — there is no line here, only a row — and deliberately OUT of
   * the autoscale: a series that took part would extend the pane to hold the row, and the row would
   * then be measured against a pane stretched to reach it. */
  function signalRow() {
    if (!charts.price) return null;
    if (!charts.signals) {
      charts.signals = charts.price.addLineSeries({
        lineVisible: false, lastValueVisible: false, priceLineVisible: false,
        crosshairMarkerVisible: false, autoscaleInfoProvider: () => null,
      });
    }
    return charts.signals;
  }

  /* Put the row where the top of the pane is NOW, and give it a point on every drawn bar — the
   * arrows are placed against those points. The pane's prices move under the row whenever the chart
   * is panned, pinched, fitted or gains an overlay, which is why this runs again on every change of
   * the visible range as well as on every draw.
   *
   * It cannot re-enter itself: the row's bars are a SUBSET of the candles', so writing to it
   * cannot widen the time scale and set off the range change that called it. */
  function placeSignalRow(series) {
    if (!series || !charts.candles) return;
    const value = charts.candles.coordinateToPrice(SIGNAL_ROW_Y);
    if (value === null || value === undefined || !Number.isFinite(value)) return;
    charts.signalRowValue = value;
    series.setData((charts.barOrder || []).map((time) => ({ time, value })));
  }

  /* The indicators this chart has a control for: the ones the rules test, minus the volume — this
   * chart's own volume pane IS that series, so a chip for it would be a switch with nothing behind
   * it. */
  function chippedIndicators() {
    if (!indicators) return [];
    return indicators.overlays.filter(
      (overlay) => overlay.used && overlay.key !== "volume_abs");
  }

  function indicatorWanted(overlay) {
    return !indicatorOff[overlay.key];
  }

  /* The bundle, read once: it is memoized on the server and identical to the lab's, so a second
   * read per poll would be a request for a picture that has not changed. */
  async function loadIndicators() {
    if (indicators) return indicators;
    try {
      indicators = await api("/api/v1/chart/indicators");
    } catch (err) {
      setNote("lg-chart-note", `the indicators could not be read — ${err.message}`);
      indicators = null;
    }
    return indicators;
  }

  /* One chip per rule-tested indicator, in the shape the lab's chart uses for the same control:
   * the overlay's own colour as a dot, its label, and a click that takes the series — or the
   * whole pane — off and back on. */
  function renderIndicatorChips() {
    const host = $("lg-indicator-chips");
    if (!host) return;
    if (!indicators) {
      setIfChanged(host, "");
      return;
    }
    const wanted = chippedIndicators();
    if (!wanted.length) {
      setIfChanged(host, `<span class="muted">No indicator is used by this strategy's rules.</span>`);
      return;
    }
    setIfChanged(host, wanted.map((overlay) => {
      const on = indicatorWanted(overlay) ? " active" : "";
      return `<button type="button" class="chip-ind${on}" data-key="${esc(overlay.key)}"`
        + ` title="${esc(overlay.label)}">`
        + `<span class="dot" style="background:${esc(overlay.color || "#4c8dff")}"></span>`
        + `${esc(overlay.label)}</button>`;
    }).join(""));
    for (const chip of host.querySelectorAll("[data-key]")) {
      chip.onclick = () => {
        const key = chip.dataset.key;
        indicatorOff[key] = !indicatorOff[key];
        chip.classList.toggle("active", !indicatorOff[key]);
        drawOverlays();
        buildOscPanes();
      };
    }
  }

  /* Is this a bar the chart is showing?
   *
   * The indicator bundle is the LAB's: it arrives for the whole dataset, because the lab charts
   * sixty days. This chart is OF one session, and its bars are the day's — so a series drawn
   * unclipped would stretch the price pane's own fit to two months and squeeze the day being
   * examined into a sliver at the right-hand edge. The drawn bars are the window, exactly. */
  function onScreen(time) {
    if (!charts.barTimes || !charts.barTimes.size) return false;
    return charts.barTimes.has(time);
  }

  function dayPoints(line) {
    return (line.data || []).filter((point) => onScreen(point.time));
  }

  /* The price-scaled overlays the reader has left on, as series on the candles' own scale. */
  function drawOverlays() {
    (charts.overlays || []).forEach((series) => {
      try { charts.price.removeSeries(series); } catch (_) { /* the chart is gone */ }
    });
    charts.overlays = [];
    if (!charts.price || !indicators) return;

    chippedIndicators()
      .filter((overlay) => overlay.scale === "price" && indicatorWanted(overlay))
      .forEach((overlay) => {
        (overlay.lines || []).forEach((line) => {
          // Bands are one overlay with three lines, and the two edges read as a channel only if
          // they are the colours a trader expects: red top, green bottom, the middle in the
          // overlay's own colour.
          const color = overlay.kind === "bands"
            ? (line.name === "upper" ? "#ef5350" : (line.name === "lower" ? "#26a69a" : overlay.color))
            : (line.color || overlay.color);
          const series = charts.price.addLineSeries({
            color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          });
          series.setData(dayPoints(line));
          charts.overlays.push(series);
        });
      });
  }

  /* The panes: one small chart per oscillator, under the price pane and above the account's.
   *
   * Rebuilt only when the SET of them changes — a day change, the bundle arriving, a toggle —
   * because the session is redrawn on every poll and taking two charts down every twenty seconds
   * would take their zoom and their crosshair with them.
   *
   * ...and built only when the host has a width: the library measures its container as the chart
   * is created, so a pane built while the panel is folded is a pane that comes back blank (see
   * ``buildEquityPane``, where this was learned). */
  let oscSignature = null;
  let oscDay = null;

  function buildOscPanes() {
    const host = $("lg-osc-panes");
    const wanted = (indicators ? chippedIndicators() : [])
      .filter((overlay) => overlay.scale === "osc" && overlay.key !== "volume_abs"
        && indicatorWanted(overlay));
    const signature = wanted.map((overlay) => overlay.key).join(",");

    if (host && signature === oscSignature && charts.day === oscDay) return;
    oscSignature = signature;
    oscDay = charts.day;

    oscPanes.forEach((pane) => {
      try { pane.chart.remove(); } catch (_) { /* already gone */ }
    });
    oscPanes.length = 0;
    if (!host) return;
    host.innerHTML = "";
    if (typeof LightweightCharts === "undefined") return;
    if (!charts.price || !wanted.length || !host.clientWidth) return;

    wanted.forEach((overlay) => {
      const box = document.createElement("div");
      box.className = "osc-pane";
      const title = document.createElement("div");
      title.className = "osc-title";
      title.textContent = overlay.label;
      const canvas = document.createElement("div");
      canvas.className = "osc-canvas";
      box.appendChild(title);
      box.appendChild(canvas);
      host.appendChild(box);

      const chart = LightweightCharts.createChart(canvas, chartOptions(CHART_HEIGHT.osc, false));
      let points = 0;
      // The pane's own times, longest line first: a window from another pane is handed over in
      // THESE units (see ``ownRange``).
      let times = [];
      (overlay.lines || []).forEach((line) => {
        const data = dayPoints(line);
        if (data.length > times.length) times = data.map((point) => point.time);
        points = Math.max(points, data.length);
        const series = line.kind === "histogram"
          ? chart.addHistogramSeries({
            priceFormat: line.priceFormat, priceLineVisible: false, lastValueVisible: false,
          })
          : chart.addLineSeries({
            color: line.color || overlay.color, lineWidth: 1,
            priceLineVisible: false, lastValueVisible: false,
          });
        series.setData(data);
      });
      ChartZoom.bind(canvas, () => ({ chart, barCount: points }));
      oscPanes.push({ key: overlay.key, chart, points, canvas, times });
    });
    linkPanes();
    // A pane built now has missed all the moving and zooming the others have been through, so it
    // is put on the stretch of time the reader is actually looking at. Only one with data can
    // take a range: the library throws on a pane with nothing on it.
    oscPanes.filter((pane) => pane.points > 0).forEach((pane) => {
      showWhatTheBarsShow(pane.chart);
    });
  }

  /* A tick's bar stamp, as the library's ``time`` value for this dataset — the same conversion the
   * server does for the bars (``src.data.dataset.chart_time``): unix seconds for intraday bars,
   * the date for calendar ones. */
  function markerTime(stamp) {
    if (!stamp) return null;
    const interval = (state.dataset && state.dataset.interval) || "";
    const intraday = typeof ChartTime === "undefined" ? true : ChartTime.isIntraday(interval);
    if (!intraday) return String(stamp).slice(0, 10);
    const ms = Date.parse(stamp);
    return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
  }

  /* The signals that were not a hold, on the bars they were decided on — as one row of arrows at
   * the top of the pane, all of them pointing down at their bar (see ``SIGNAL_ROW_Y``). */
  function drawSignalMarkers() {
    if (!signalsWanted()) {
      // Off: no row is built for a chart nobody asked to annotate, and one left over from a
      // previous look has its arrows taken off it.
      if (charts.signals) charts.signals.setMarkers([]);
      return;
    }
    const row = signalRow();
    if (!row) return;
    const day = charts.barTimes || new Set();
    const markers = [];
    ((state.log && state.log.ticks) || []).filter(mine).forEach((tick) => {
      const signal = String(tick.signal || "").toUpperCase();
      if (!signal || signal === "HOLD") return;
      const time = markerTime(tick.bar);
      if (time === null || !day.has(time)) return;
      // Filled: the tick's own record of what it sent. An order id means something reached the
      // broker; a closed round trip means something came of it. A signal with neither is a
      // decision nothing happened on — drawn, because "it said BUY and nothing was filled" is
      // exactly what this read-out is for, and grey, so it reads as the non-event it was.
      const filled = (tick.order_ids || []).length > 0
        || (tick.trades || []).length > 0
        || (tick.intents || []).some((intent) => intent && intent.order_id);
      const buy = signal === "BUY";
      markers.push({
        time,
        // The SAME shape and the same place for both sides: the row is the session's timeline of
        // decisions and every arrow points down at its own bar. Which side it was is the colour's
        // job — the trade's own green and red, grey for a signal nothing came of — and the height
        // is the row's, so nothing here depends on where the price went.
        position: "aboveBar",
        shape: "arrowDown",
        color: filled
          ? (buy ? MARKER_COLOURS.buyFilled : MARKER_COLOURS.sellFilled)
          : (buy ? MARKER_COLOURS.buyOpen : MARKER_COLOURS.sellOpen),
      });
    });
    markers.sort((a, b) => (a.time > b.time ? 1 : (a.time < b.time ? -1 : 0)));

    // Placed twice, on purpose. The price scale settles on the library's own animation frame, so
    // the call below measures the pane as it was a moment ago — before an overlay was added, or
    // before this day's bars were fitted — and the one after it the pane the arrows are drawn on.
    const place = () => {
      if (!signalsWanted()) return;
      placeSignalRow(row);
      row.setMarkers(markers);
    };
    place();
    charts.signalFrame = requestAnimationFrame(() => {
      charts.signalFrame = null;
      // Only on the chart this row belongs to: a day change removes the series, and a stale frame
      // writing to it would be writing to a chart that is gone.
      if (charts.signals === row) place();
    });
  }

  /* Draw both read-outs as they now stand. Called from every draw of the session, and from the
   * checkboxes themselves — a toggle that waited for the next poll would be a control that does
   * nothing for twenty seconds. */
  async function drawReadouts() {
    // Decoration on a chart that must draw without it: a series the library refuses, or an
    // endpoint answering with something unexpected, leaves the bars alone.
    try {
      await loadIndicators();
      renderIndicatorChips();
      drawOverlays();
      buildOscPanes();
      drawSignalMarkers();
    } catch (err) {
      setNote("lg-chart-note", `the read-outs could not be drawn — ${err.message}`);
    }
  }

  async function onChartToggle() {
    await drawReadouts();
  }

  function number(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function drawBars(rows) {
    const bars = [];
    const volume = [];
    for (const row of rows || []) {
      const time = row.time;
      const open = number(row.open), high = number(row.high);
      const low = number(row.low), close = number(row.close);
      if (time === null || time === undefined || open === null || high === null
        || low === null || close === null) continue;
      bars.push({ time, open, high, low, close });
      const size = number(row.volume);
      volume.push({
        time,
        value: size === null ? 0 : size,
        color: close >= open ? "rgba(38, 166, 154, 0.45)" : "rgba(239, 83, 80, 0.45)",
      });
    }
    charts.candles.setData(bars);
    charts.volume.setData(volume);
    charts.bars = bars.length;
    // The bars that are on screen, by the library's own time value: the signal markers are placed
    // on these and dropped when their bar is not here (the other account's tick, or another day).
    // Ordered as well as indexed, because the signal row gives one point to each of them.
    charts.barTimes = new Set(bars.map((bar) => bar.time));
    charts.barOrder = bars.map((bar) => bar.time);
    return bars.length;
  }

  function drawEquity(payload) {
    const points = ((payload && payload.points) || [])
      .filter((p) => number(p.equity) !== null && p.time !== undefined && p.time !== null)
      .map((p) => ({ time: p.time, value: Number(p.equity) }));
    charts.line.setData(points);
    charts.points = points.length;
    charts.pointTimes = points.map((point) => point.time);
    // Only when the pane is new (or the day was rebuilt around it): a poll redraws the curve and
    // must leave the reader's own window on that pane alone.
    if (equityNeedsAlign) {
      equityNeedsAlign = false;
      showWhatTheBarsShow(charts.equity);
    }
    return points.length;
  }

  function setEquityNote(text) {
    setNote("lg-equity-note", text);
  }

  /* Is the account's pane wanted at all?
   *
   * It is what the account was WORTH through the session, which is an answer to what the day's
   * decisions did — and there is nothing to answer until a round trip has been closed ON THAT
   * DAY. The rows behind this are the strategy's whole history, so the day on screen is what
   * separates them here: a curve under today's candles because a trade closed last week would be
   * a verdict on the wrong session. Filtered to the account in play as well, like every other
   * table here: the curve drawn is that account's, so it answers to that account's round trips.
   *
   * Unless the reader has said otherwise: the chip over the chart is their word, and it wins over
   * this rule in BOTH directions — showing the pane on a day whose trades say nothing, and keeping
   * it off on a day where a curve is not what they are reading. */
  function equityWanted() {
    if (equityChoice !== null) return equityChoice;
    const day = state.log && state.log.day;
    return ((state.trades && state.trades.trades) || [])
      .filter(mine)
      // A row with no ``day`` at all — hand-written, or written before days were recorded — cannot
      // be shown to be another session's, so it counts: the same rule ``mine`` applies to accounts.
      .some((trade) => !day || !trade.day || String(trade.day) === day);
  }

  /* Put the pane on screen or take it off, and build nothing.
   *
   * The two are apart because visibility is what decides whether the chart can be BUILT at all
   * (see ``buildEquityPane``), so a rebuild has to settle this before it draws anything — and a
   * show that built the chart here would build it a second time when the rebuild drew it. */
  function syncEquityPane() {
    const panel = $("lg-equity-panel");
    const wanted = equityWanted();
    if (panel) panel.hidden = !wanted;
    // The chip REPORTS the pane rather than deciding it: the pane also appears on its own when a
    // round trip closes, and a box still showing "off" over a visible chart would be lying.
    const box = $("lg-equity-toggle");
    if (box) box.checked = wanted;
    const appeared = wanted && !equityShown;
    equityShown = wanted;
    return appeared;
  }

  /* The equity chip: the reader's own word on whether the account's pane is on screen.
   *
   * Turning it ON builds the chart if this is the first time the pane has been visible — a chart
   * created inside a hidden panel comes up with a canvas 0px wide and never recovers (see
   * ``buildEquityPane``) — and fetches the curve, since nothing has read it. Turning it OFF only
   * hides the pane: the chart and its zoom survive being switched back on. */
  async function onEquityToggle() {
    const box = $("lg-equity-toggle");
    equityChoice = !!(box && box.checked);
    const appeared = showEquityPane();
    if (!equityShown) return;
    if (appeared || !charts.points) await loadEquity();
  }

  /* The same, and the chart with it. Returns whether the pane just APPEARED — the one moment its
   * curve is worth fetching without waiting for the next poll. The trades are read after the
   * session (the broker half comes last in ``loadAll``), so on the first load the pane is decided
   * twice: once by ``renderSession``, which does not know yet, and once by ``renderTrades``. */
  function showEquityPane() {
    const appeared = syncEquityPane();
    if (equityShown) buildEquityPane();
    return appeared;
  }

  /* The account's own series, from the broker. Kept apart from the bars so the two halves can be
   * fetched and fail independently — and so the pane can ask for it LATE (see above). */
  async function loadEquity() {
    if (!charts.line) return;
    try {
      const env = inPlayEnv() || (state.accounts && state.accounts.env) || "";
      const payload = await api(`/api/v1/accounts/history?env=${encodeURIComponent(env)}`);
      drawEquity(payload);
      setEquityNote(payload.ok ? "" : `the account's equity could not be read — ${payload.reason}`);
    } catch (err) {
      // A 404 here means the server predates this endpoint, and the wrapper's thrown message says
      // so in words built for a console, not for a sentence under a chart. Said in the page's own
      // words instead, with the fix in them.
      charts.line.setData([]);
      charts.points = 0;
      charts.pointTimes = [];
      setEquityNote(/\(404\)/.test(err.message)
        ? "the account's equity history is not served by this server yet — "
          + "restart the server to draw the curve"
        : `the account's equity could not be read — ${err.message}`);
    }
  }

  /* One of the two lines under the panes. They are SIBLINGS of the chart hosts, never the hosts
   * themselves: the library draws its canvas INTO a host, so a message written there takes the
   * chart with it — which is exactly how the price pane came up blank the first time. */
  function setNote(id, text) {
    const note = $(id);
    if (!note) return;
    note.textContent = text || "";
    note.hidden = !text;
  }

  function renderChartMeta() {
    const meta = $("lg-chart-meta");
    if (!meta) return;
    const d = state.dataset || {};
    const day = (state.log && state.log.day) || "";
    // The instrument and the bar size come from the dataset the bars were read from, not from the
    // strategy this page is scoped to: if the two ever disagree, the chart says which one it is
    // showing rather than letting the reader assume.
    const bits = [d.symbol, d.interval, day, charts.bars ? `${charts.bars} bars` : ""].filter(Boolean);
    meta.textContent = bits.join(" · ");
  }

  /* The dataset's own account of itself: the symbol, the bar size, and the zone the bars are
   * stamped in. One local file read, and the only thing on this page that can say which time
   * zone a bar is in — nothing in the loop's records carries it. */
  async function readDataset() {
    try {
      state.dataset = await api("/api/v1/dataset/status");
    } catch (err) {
      state.dataset = null;  // unread is not fatal: the cells fall back to the stamp's own zone
    }
    return state.dataset;
  }

  /* Draw the session. The day decides the bars; the account in play decides the equity. Both
   * halves fail on their own — a broker that cannot be read must not take the price chart with
   * it, and that is the state this page is most likely to be read in when something is wrong. */
  async function renderSession(day) {
    const host = $("lg-chart");
    if (!host) return;
    const wanted = day || (state.log && state.log.day) || "";

    // The zone and the bar size label the axis. Read if nothing has read them yet (a day button
    // goes straight to ``loadLog``, which does read them, so this is normally already in hand).
    if (!state.dataset) await readDataset();

    // Rebuilt when the DAY changes (and on the first draw); re-drawn in place otherwise, so a
    // poll does not throw away the zoom somebody just set.
    const rebuilt = !charts.price || charts.day !== wanted;
    if (rebuilt) {
      charts.day = wanted;
      charts.bars = 0;
      charts.points = 0;
      // A new day's bars are a new window, so the account's pane is put on it again when its curve
      // arrives (the pane itself survives a day change — see ``equityNeedsAlign``).
      equityNeedsAlign = true;
      // Whether the pane is on screen is settled BEFORE anything is built: it is what decides
      // whether its chart can be built at all (see ``buildEquityPane``). The state can have moved
      // since the last draw — a day change with a mode flip, say — and a pane built while hidden
      // is a pane that comes back blank.
      syncEquityPane();
      buildCharts();
    }
    if (!charts.price) return;

    try {
      // The day, exactly: the filter is on each bar's own stamp, so a bare date as the END is
      // midnight — the day's first instant — and "2026-09-21 to 2026-09-21" returns nothing at
      // all for intraday bars. Both ends are therefore spelled to the second.
      const query = wanted
        ? `?start=${encodeURIComponent(wanted + " 00:00:00")}`
          + `&end=${encodeURIComponent(wanted + " 23:59:59")}&limit=0`
        : "?limit=0";
      const data = await api(`/api/v1/dataset/data${query}`);
      const drawn = drawBars(data.rows);
      setNote("lg-chart-note", drawn ? "" : emptySessionNote(wanted));
    } catch (err) {
      setNote("lg-chart-note", `the bars could not be read — ${err.message}`);
      return;
    }

    // After the bars, and outside their error handler: the overlays go on the candles and a marker
    // can only sit on a drawn bar, but a read-out that fails is not a day whose bars failed to
    // load — the chart draws without them either way.
    await drawReadouts();

    // Refreshed on every draw the pane is on screen for — the session's curve grows — so the
    // question here is whether it is WANTED, not whether it just appeared: an account with
    // nothing closed is not asked for a curve nobody will see.
    if (equityWanted()) {
      showEquityPane();
      await loadEquity();
    }

    renderChartMeta();
    // Fit only the first draw of a day: a poll that re-fitted would zoom the reader out every
    // twenty seconds.
    if (rebuilt && charts.bars) charts.price.timeScale().fitContent();
  }

  /* Why the pane is empty, in words. A day the loop has not traded YET is not a day it failed on,
   * and an empty chart with no sentence beside it reads as a broken chart — which is exactly the
   * state this page is opened in first thing in the morning. */
  function emptySessionNote(day) {
    const today = (state.log && state.log.today) || "";
    return day && day === today
      ? "the session has not started — no bars for today yet"
      : "no bars were recorded for this day";
  }

  async function loadLog(day) {
    const query = day ? `?day=${encodeURIComponent(day)}` : "";
    // The dataset says which zone the bars are stamped in, and the ticks and orders tables name a
    // BAR in every row — so it is read before either of them renders. Read here rather than in the
    // chart because the chart is the one panel that can wait; a table drawn in UTC and re-drawn
    // in the market's zone twenty seconds later is a table that lied for twenty seconds.
    await readDataset();
    try {
      state.log = await api(`/api/v1/log${query}`);
    } catch (err) {
      fail(`could not read the log: ${err.message}`);
      state.log = null;
    }
    renderDays();
    // Keep the URL in step with the day being read, like the report page does with its run: a
    // refresh, or a link sent to someone, reopens THIS session rather than jumping to today.
    if (state.log && state.log.day) {
      const url = new URL(window.location.href);
      url.searchParams.set("day", state.log.day);
      history.replaceState(null, "", url);
    }
    // The strategy's closed round trips BEFORE the chart, because the account's pane under it
    // appears when a round trip has closed — and the pane is ONE session's, while the rows are the
    // whole history: they carry the day they closed on, so the pane picks its own out of them
    // (``equityWanted``). The table above does not change with the day.
    await loadTrades();
    renderTicks();
    renderOrders();
    // The chart is OF this day, so it is redrawn with the tables that changed under it — picking
    // a day at the left has to move the price chart too, or the pane keeps drawing yesterday's
    // session under today's heading.
    await renderSession(state.log && state.log.day);
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
    // Armed with nothing running it is the state this page exists to make visible — and the one it
    // can also FIX: the arm outlives a loop that crashed, and until now the operator had to notice
    // and re-arm by hand. The runner is put back, the state is read again, and the gates below
    // then show the tick that came out of it.
    if (await ensureLoopRunning()) {
      try {
        state.loop = await api("/api/v1/loop");
      } catch (_) { /* keep the state we read, rather than blanking the panel */ }
    }
    renderLoopState();
  }

  /* Ask the server to restore the loop when trading is ON and nothing is running it.
   *
   * Only when it is genuinely missing: ``running`` is a live holder, while ``overdue`` (a claim no
   * process honours — the crash case), ``stopped`` and ``never`` all mean an armed switch is
   * waiting for a process that is not there. The server throttles the ask, so a 20-second poll
   * cannot fork a storm, and a failure here is not reported: the state line under the clock says
   * what is wrong in the loop's own words. */
  async function ensureLoopRunning() {
    const loop = state.loop || {};
    if (!tradingArmed() || loop.state === "running") return false;
    let result = null;
    try {
      result = await api("/api/v1/loop/ensure", { method: "POST" });
    } catch (_) {
      return false;
    }
    if (result && result.started) {
      flashToast("Trading is armed and no loop was running — started one.", "warn");
      return true;
    }
    return false;
  }

  /* ── The day's tape, under the day menu: the biggest gainers, and the small caps doing the most
   * volume ──
   *
   * Both are panels of the screener the Market page runs, so the two pages cannot disagree about
   * what moved today, and each list is in the panel's own order (by day change, by volume) rather
   * than in one this page sorts. They sit under the days because the three answer one question
   * between them — what this bot has run, and what moved while it ran.
   *
   * A panel reaches the provider, so each list is read ONCE: a reload does not re-screen, and
   * neither does a pass of the poll. The ↻ is the only thing that reads one again, and the label
   * beside it is when that read happened. The numbers are the screener's, and it reports the last
   * COMPLETED regular session rather than a live tape, so the tooltip says which print this is.
   */
  const SCREEN_ROWS = 10;
  // ...and asked for a WINDOW rather than for exactly ten: the screener runs its own filters over
  // Yahoo's ranking, so a request for ten can come back with eight, and a "top 10" showing eight
  // would be the filters talking rather than the market. The first ten of the window are the ten
  // the panel's own ranking puts first.
  const SCREEN_WINDOW = 25;
  const SCREEN_LISTS = [
    { key: "top_gainers", body: "lg-gainers-body", host: "lg-gainers-list",
      at: "lg-gainers-at", button: "lg-gainers-refresh" },
    { key: "small_cap_volume", body: "lg-smallcaps-body", host: "lg-smallcaps-list",
      at: "lg-smallcaps-at", button: "lg-smallcaps-refresh" },
  ];

  // The rows the list SHOWS, which is the panel's order cut to a length a reader can take in — not
  // the whole window the panel answered with.
  function screenRows(payload) {
    return ((payload && payload.rows) || []).slice(0, SCREEN_ROWS);
  }

  /* Shares and market caps in a 240px column: the screener's numbers rounded to what a reader
   * compares — 12.3M, $1.8B — rather than to the digit. */
  function compactNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    const abs = Math.abs(number);
    if (abs >= 1e12) return `${(number / 1e12).toFixed(2)}T`;
    if (abs >= 1e9) return `${(number / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${(number / 1e3).toFixed(1)}K`;
    return String(Math.round(number));
  }

  // Named for what it is, and NOT ``clockText``: that one is the countdown's face (a duration,
  // ``00:12:34``), and a second function of the same name silently shadows it for every caller
  // above — which is how the tick timer came to print a 1970 clock reading.
  function readTime(when) {
    return new Date(when).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function readScreen(list) {
    const entry = state.screens[list.key];
    return entry && entry.payload ? entry : null;
  }

  /* The two lists ride the page's ordinary reload — the label is re-stamped by it, and a list that
   * is already on screen is NOT read again: the ↻ is what says "screen this now". */
  async function loadScreens() {
    for (const list of SCREEN_LISTS) {
      if (!$(list.body)) continue;
      if (readScreen(list)) renderScreenLabel(list);
      else await screenAgain(list);
    }
  }

  // One read of one panel. Returns the payload, or null when the screen failed — the caller says
  // so, because only it knows whether the reader asked for this read.
  async function screenAgain(list) {
    const host = $(list.host);
    if (!host) return null;
    try {
      const payload = await api(`/api/v1/market/panel/${list.key}?size=${SCREEN_WINDOW}`);
      state.screens[list.key] = { payload, at: Date.now() };
      renderScreen(list);
      return payload;
    } catch (err) {
      // The one thing a failed screen must not do is empty a table the reader is reading: an empty
      // table says "nothing matched", which is not what happened. It says so where the table would
      // go, and only when there is no table yet.
      if (!host.innerHTML) {
        setIfChanged(host, `<p class="muted note">could not read the list: `
          + `${esc(err.message)}</p>`);
      }
      return null;
    }
  }

  function renderScreenLabel(list) {
    const at = $(list.at);
    const entry = readScreen(list);
    if (!at || !entry) return;
    // A clock reading rather than an age: it is when THIS page read the list, which stays true in a
    // tab left open — an age would have to be re-rendered to stay honest about itself.
    setIfChanged(at, `read ${readTime(entry.at)}`);
    const ranked = { percentchange: "the day's change", dayvolume: "volume today" };
    at.title = [
      entry.payload.description || "",
      `ranked by ${ranked[String(entry.payload.sort_field || "")] || "the screener's own order"}`,
      `read ${stamp(entry.at)}`,
      entry.payload.criteria ? `criteria: ${entry.payload.criteria}` : "",
      entry.payload.error ? `the screener failed: ${entry.payload.error}` : "",
      "The screener reports the last COMPLETED regular session — its change, its volume and its "
        + "close — so on a day that has not closed yet this is that session's tape.",
    ].filter(Boolean).join("\n");
  }

  function renderScreen(list) {
    renderScreenLabel(list);
    const host = $(list.host);
    const entry = readScreen(list);
    if (!host || !entry) return;
    const payload = entry.payload || {};
    if (payload.error) {
      setIfChanged(host, `<p class="muted note">the screener failed: ${esc(payload.error)}</p>`);
      return;
    }
    const rows = screenRows(payload);
    if (!rows.length) {
      setIfChanged(host, `<p class="muted note">no symbol matched today.</p>`);
      return;
    }

    // The panel's columns minus the ones a 240px column cannot carry; the name, the price and the
    // exchange ride in each row's tooltip, which is where a reader checking WHICH one this is looks.
    const body = rows.map((row, index) => {
      const change = Number(row.change_percent);
      const cls = Number.isFinite(change) ? (change < 0 ? "bad" : "good") : "";
      const price = Number(row.price);
      const title = [row.name, Number.isFinite(price) ? `$${price.toFixed(2)}` : "",
        row.exchange_name || row.exchange].filter(Boolean).join(" · ");
      return `<tr${index === 0 ? ' class="leader"' : ""}>`
        + `<td>${index + 1}</td>`
        + `<td title="${esc(title)}">${esc(row.symbol)}</td>`
        + `<td class="num ${cls}">${percentText(row.change_percent)}</td>`
        + `<td class="num">${compactNumber(row.volume)}</td>`
        + `<td class="num">${row.market_cap === null || row.market_cap === undefined
          ? "—" : "$" + compactNumber(row.market_cap)}</td>`
        + "</tr>";
    }).join("");

    setIfChanged(host, `<table class="lg-table"><thead><tr><th>#</th><th>symbol</th>`
      + `<th class="num">chg %</th><th class="num">volume</th><th class="num">cap</th>`
      + `</tr></thead><tbody>${body}</tbody></table>`);
  }

  /* Screen one list again instead of waiting for the next page load. The screener reaches the
   * provider, so the label says it is working: a click that shows nothing for ten seconds reads as
   * a click that did nothing. A failed screen keeps the list on screen — an emptied table would say
   * "nothing matched", which is the one thing this must not invent. */
  async function refreshMarketList(key) {
    const list = SCREEN_LISTS.filter((item) => item.key === key)[0];
    if (!list) return;
    const button = $(list.button);
    const at = $(list.at);
    if (button) button.disabled = true;
    if (at) at.textContent = "screening…";
    const payload = await screenAgain(list);
    if (button) button.disabled = false;
    if (payload) {
      flashToast(`Screened ${screenRows(payload).length} symbols.`, "ok");
      return;
    }
    if (at) at.textContent = "screening failed";
    flashToast("could not screen the list.", "warn");
  }

  async function loadAll(day) {
    await loadStatus();
    // The local half carries the day's round trips with it (see ``loadLog``). The broker half is
    // read here and kept: positions and working orders are what the account holds NOW, and no day
    // chosen in the log changes that — which is exactly why they are worth having on the page.
    await loadLog(day || params.get("day"));
    // The broker half last, and independently: its failure must not blank the local log,
    // which is the half that still works when the account is unreachable.
    try {
      const [positions, accounts, orders] = await Promise.all([
        api("/api/v1/positions"), api("/api/v1/accounts"), api("/api/v1/orders"),
      ]);
      state.positions = positions;
      state.accounts = accounts;
      state.orders = orders;
    } catch (err) {
      fail(`could not read the account: ${err.message}`);
      state.positions = { positions: [] };
      state.accounts = { ok: false, message: err.message, accounts: [] };
      state.orders = { ok: false, message: err.message, open: [], resting: [], closed: [] };
    }
    renderAccount();
    // The two screener lists ride the ordinary reload too, but the reload only re-stamps what it
    // has: a list already on screen is not screened again (see ``loadScreens``).
    loadScreens();
    schedulePoll();
  }

  window.loadLog = loadLog;
  window.loadAll = loadAll;
  // The Mode and Trading boxes are built by the shared module and handled here, so both handlers
  // have to be reachable from the markup it renders.
  window.toggleTrading = toggleTrading;
  window.flipMode = flipMode;
  window.toggleCard = toggleCard;
  // The session chart's read-outs: the signals checkbox in its head, and the indicator chips it
  // renders itself.
  window.onChartToggle = onChartToggle;
  window.onEquityToggle = onEquityToggle;
  // The pager's buttons, reachable from the markup the table renders.
  window.goTickPage = goTickPage;
  // The ↻ button means "re-read what I am looking at". Going through ``loadAll()`` with no
  // day would fall back to the server's newest, so a click from a past day would silently
  // jump the page forward — the one thing a refresh must not do.
  window.refreshLog = function () {
    return loadAll(state.log && state.log.day);
  };

  /* The ↻ in each PANEL's head. The Account card's (above) re-reads the whole page; these re-read
   * ONE panel, now, instead of waiting out the twenty-second poll — the reader is asking "what did
   * it just do", and should not have to sit through a pass over everything, nor have the broker
   * asked about positions to find out what the last tick decided.
   *
   * Each re-renders only what it read, and a failure goes to the page's error strip with the panel
   * left as it was: a table emptied by a failed read would say "nothing happened", which is the one
   * thing a refresh must never invent. Two of them share one read — the ticks and the orders both
   * come out of ``/api/v1/log`` — so a click refreshes that payload and re-renders its own panel. */
  async function reloadDayRecords() {
    const day = state.log && state.log.day;
    const query = day ? `?day=${encodeURIComponent(day)}` : "";
    try {
      state.log = await api(`/api/v1/log${query}`);
    } catch (err) {
      fail(`could not read the log: ${err.message}`);
    }
  }

  // The Loop panel: the lease and the switch (the clock is written from them), the gates the last
  // tick walked, and the day's records under them.
  async function refreshLoop() {
    await loadStatus();
    await reloadDayRecords();
    renderTicks();
  }

  // Positions and working orders — the panel that asks the BROKER. The accounts read comes with
  // it: one reply carries what the boxes say as well, and asking twice would be two answers to
  // the same question.
  async function refreshPositions() {
    try {
      const [positions, accounts, orders] = await Promise.all([
        api("/api/v1/positions"), api("/api/v1/accounts"), api("/api/v1/orders")]);
      state.positions = positions;
      state.accounts = accounts;
      state.orders = orders;
    } catch (err) {
      fail(`could not read the account: ${err.message}`);
      return;
    }
    renderAccount();
  }

  async function refreshOrders() {
    await reloadDayRecords();
    renderOrders();
  }

  /* The strategy's closed round trips — EVERY one of them, not only the day on screen.
   *
   * The panel answers "how has this strategy done", and a strategy runs for weeks: a table that
   * emptied every midnight answered a question nobody asks. Nothing is lost by reading the log
   * whole, because every row carries the day it closed on — which is also how the account's pane
   * under the chart, which IS about one session, picks its own rows out of this list.
   *
   * ``limit`` counts from the END: the newest of the history, which is the half anyone reads. The
   * broker's own panels are deliberately NOT re-read here: positions and working orders are what
   * the account holds NOW, and walking the day menu must not touch them. */
  const TRADES_LIMIT = 200;

  async function loadTrades() {
    try {
      state.trades = await api(`/api/v1/trades?limit=${TRADES_LIMIT}`);
    } catch (err) {
      fail(`could not read the trades: ${err.message}`);
      return;
    }
    renderTrades();
  }

  async function refreshTrades() {
    await loadTrades();
  }

  /* The CHART panel's ↻: the bars and the read-outs drawn over them, re-read for the day on
   * screen. It is the one panel refresh that DOES redraw a chart, and that is what it is for — the
   * session grows while the page is open, and a reader watching a position work wants the bar that
   * just closed without waiting out the poll.
   *
   * The dataset is read with it: that is where the zone the bar labels are written in comes from,
   * and a dataset rebuilt under the page is exactly the case where re-reading is worth a click.
   * ``renderSession`` redraws IN PLACE unless the day changed, so the zoom the reader set survives
   * a refresh — and nothing here touches the tables, which have ↻s of their own. */
  async function refreshChart() {
    await readDataset();
    await renderSession(state.log && state.log.day);
  }

  window.refreshLoop = refreshLoop;
  window.refreshPositions = refreshPositions;
  window.refreshOrders = refreshOrders;
  window.refreshTrades = refreshTrades;
  // The chart panel's own ↻, which is the panel refresh that redraws a chart (its own panel's).
  window.refreshChart = refreshChart;
  // The ↻ each screener list carries: the one control on the reference lists, and the only thing
  // this page does with the market screener.
  window.refreshMarketList = refreshMarketList;

  // The page re-reads itself only while TODAY is showing. A past day cannot gain rows, and
  // polling one would be broker traffic for a page nobody is watching change. Today is the
  // exchange's today, decided by the server, so a reader in another timezone does not drag
  // the page into refreshing a day that has already closed.
  const LOG_POLL_MS = 20000;
  let pollTimer = null;

  function isToday() {
    return !!(state.log && state.log.day && state.log.day === state.log.today);
  }

  /* The lock lives in ``auth.js``, which a page may not have (a node harness, or an install with
   * no PIN set). Asking whether it exists is cheaper than a page that cannot load at all. */
  function isLockedByAuth() {
    return !!(window.Auth && window.Auth.isLocked && window.Auth.isLocked());
  }

  /* Is there anything LIVE to keep an eye on? The day's tables do not change on a past day, but
   * the status block does: a loop can die, and the switch can be armed from another tab. That
   * block is what this page now carries, so it is what decides whether the page keeps reading —
   * and with both quiet there is nothing to watch. */
  function statusIsLive() {
    const status = state.loop || {};
    if (status.state === "running" || status.state === "stalled"
        || status.state === "overdue") return true;
    return !!((state.trading || {}).trading || {}).on;
  }

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  // Self-scheduling, so a slow day read delays the next poll instead of stacking one on top.
  function schedulePoll(delay) {
    stopPoll();
    // Locked: the overlay covers the page, so reading it again is traffic nobody can see. The
    // lock's own heartbeat is what keeps the session alive, not this poll.
    if (document.hidden || isLockedByAuth() || !(isToday() || statusIsLive())) return;
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

  // Opening a prose cell, delegated to the document because the tables are REBUILT on every read:
  // a listener per cell would be one per render, and the cell is gone by the time the next click
  // arrives. Registered here rather than beside the helpers so the section the scoping harness
  // evaluates stays free of the DOM.
  document.addEventListener("click", (event) => {
    // NOT while text is selected: the mouse-up that ends a drag-select arrives as a click, and it
    // would shut the very paragraph the reader was selecting to copy.
    const selection = window.getSelection ? String(window.getSelection()) : "";
    if (selection.trim()) return;
    toggleProse(event.target);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    // A space would otherwise scroll the page out from under the cell that was just opened.
    if (toggleProse(event.target)) event.preventDefault();
  });

  // Read once, on load, and then keep today's view current. A day button switches to that
  // day and does not poll; the ↻ button re-reads whatever is on screen.
  loadAll();
})();
