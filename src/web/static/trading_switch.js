/* TRAIDER — the master switch, in ONE place.
 *
 * The Session monitor carries it, and this module is what does the carrying: the confirmation is
 * the only thing standing between a click and real orders on a LIVE account, so the wording, the
 * endpoint and the acknowledgement are not written a second time anywhere. The Strategy lab does
 * not trade at all — it reads ONE fact from here (``tile``, for its boxes) and nothing else.
 *
 * The page hands over the plumbing it already has (its own `api`, dialog and toast) and this
 * file owns the DECISION: ask or not, which endpoint, and what the acknowledgement means.
 *
 * Turning ON always asks, in both environments — the wording differs, the asking does not,
 * because a confirmation that only appears sometimes is one nobody reads. Turning OFF never
 * asks and never needs an acknowledgement: stopping has to stay one click.
 */
const TraiderSwitch = (function () {
  "use strict";

  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replace(/[&<>"']/g, (char) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
    ));

  /* Read the loop state until it stops saying "nothing is running".
   *
   * ARMING SPAWNS THE LOOP, and the POST answers as soon as the child process EXISTS — the reply
   * carries its pid, not proof that it holds the claim. Taking the lease costs a second or two of
   * interpreter start-up, and until then no lease file exists, so a loop read taken in that window
   * says "stopped". That is how arming came to shout "no loop is running" at the operator who had
   * just watched it start. So the state that follows an arming is WAITED OUT rather than believed
   * on the first read.
   *
   * Bounded on purpose, and short: after about six seconds the answer really is "not running", and
   * the warning is then the truth rather than a race.
   */
  const LOOP_SETTLE_TRIES = 12;
  const LOOP_SETTLE_MS = 500;

  async function waitForLoop(api, tries, delayMs) {
    const attempts = tries === undefined ? LOOP_SETTLE_TRIES : tries;
    const pause = delayMs === undefined ? LOOP_SETTLE_MS : delayMs;
    let last = null;
    for (let i = 0; i < attempts; i += 1) {
      try {
        last = await api("/api/v1/loop");
      } catch (_) {
        return null; // unreadable: the page's own read says that better than a guess here
      }
      if (last && last.state !== "stopped" && last.state !== "never") return last;
      await new Promise((resolve) => setTimeout(resolve, pause));
    }
    return last;
  }

  /* Start or stop trading. The page supplies `api`, `confirmDialog` and `flashToast`, plus what
   * it last read (`trading`, `execution`), and gets back whether a write actually happened —
   * which is what tells it to re-read the state it just changed.
   *
   * A refusal by the SERVER is a write that happened: the switch was reached and answered, and
   * the page still has to re-read. Only a declined confirmation or an unreachable server leaves
   * nothing to re-read.
   */
  async function flip(deps) {
    const api = deps.api;
    const confirmDialog = deps.confirmDialog;
    const flashToast = deps.flashToast;
    const tr = deps.trading || {};
    const exec = deps.execution || {};
    const stopping = !!tr.on;

    if (!stopping) {
      const target = esc(exec.base_url || "");
      const ok = await confirmDialog(exec.live
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
          });
      if (!ok) return { wrote: false };
    }

    let r = null;
    try {
      r = await api(stopping ? "/api/v1/trading/off" : "/api/v1/trading/on", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The acknowledgement only means anything when STARTING on a live account; sending one
        // alongside a stop would read as if stopping needed consent.
        body: JSON.stringify(stopping ? {} : { confirm_live: !!exec.live }),
      });
    } catch (err) {
      flashToast(`Trading switch failed: ${err.message}`, "warn");
      return { wrote: false };
    }
    if (r && r.ok === false) flashToast(r.message || "Trading could not be turned on", "warn");
    else flashToast(r && r.message ? r.message : "", "ok");
    // The server said a loop is (now) running, so give it the moment it needs to claim the lease
    // before the caller reads the loop's state and decides whether to alarm about it. `running`
    // is false only when the start genuinely failed, and then there is nothing to wait for.
    if (!stopping && r && r.ok !== false && (r.loop || {}).running) {
      await waitForLoop(api);
    }
    return { wrote: true, ok: !(r && r.ok === false), payload: r };
  }

/* ---------- the state boxes ----------
 *
 * The Session monitor's Account card shows three: the mode, the switch, and what is open. They are
 * built here for the reason the switch itself lives here — two copies of "which account is this",
 * or of a confirmation standing between a click and real money, is two chances to get it wrong.
 * The Strategy lab uses `tile` alone, for the signal counts and the risk boxes, so a box on either
 * page is the same box.
 *
 * `tile` is the box itself — the same `.bt-stat` the backtest KPIs and the report page use. `click`
 * makes it a real `<button>`, and `disabled` renders it unpressable with no handler left on it, so
 * a write the server would refuse cannot look or behave like a live one.
 */
function tile(label, value, cls, tip, boxCls, click, disabled) {
  const tipAttr = tip ? ` data-tip="${esc(tip)}"` : "";
  const inner = `<span class="label">${esc(label)}</span>`
    + `<span class="value${cls ? ` ${cls}` : ""}">${value}</span>`;
  return click
    ? `<button type="button" class="bt-stat${boxCls ? ` ${boxCls}` : ""}"${tipAttr}`
      + (disabled ? " disabled" : ` onclick="${esc(click)}"`) + `>${inner}</button>`
    : `<div class="bt-stat${boxCls ? ` ${boxCls}` : ""}"${tipAttr}>${inner}</div>`;
}

/* The dot that ends the mode and switch boxes: blue is the calm setting, red is the one that
 * spends money and it BLINKS, and the calm end is marked too — an unmarked box beside a marked one
 * reads as "unknown" rather than "fine".
 *
 * A styled circle rather than an emoji glyph: this is a real element, so the blink is a CSS
 * animation instead of a per-tick in-place rewrite. That matters twice over here — a rewrite would
 * re-render the boxes, replacing the two BUTTONS under the cursor every 700ms, which restarts
 * their pulse and can swallow a click mid-press. */
function dot(alert) {
  return `<span class="dot ${alert ? "alert" : "calm"}"></span>`;
}

/* The box that says which account orders would go to, and is the only way to change it.
 *
 * `locked` is the configuration lock (trading ON): the server refuses the write, and a strategy
 * running on one account must not be pointed at the other in flight — sending LIVE orders against
 * positions the loop opened on paper is the worst case in this app. The hint says why instead of
 * inviting a click that would be refused. */
function envTile(env, locked, click) {
  const live = env === "live";
  return tile("Mode",
    (env ? esc(env) : "—") + (env ? dot(live) : ""),
    live ? "neg" : "",
    (live
      ? "orders go to the LIVE Alpaca account — real money"
      : (env ? "orders go to the PAPER Alpaca account — no real money"
              : "the account this run is pointed at has not been read yet"))
      + (locked
          ? " · trading is ON — turn it off to switch accounts"
          : ` · click to switch to the ${live ? "paper" : "live"} account`),
    // A blue edge for paper, a pulsing red one for live: the two settings are read at a glance, and
    // the safe one is marked too.
    live ? "flash-red" : (env === "paper" ? "tint-blue" : ""),
    click, locked);
}

/* The box that IS the master switch, so its hint is where the two facts a click depends on are
 * quoted: a FAILED credential check, and a process running older gate code than the files on disk.
 * Nothing read yet means nothing may be claimed or toggled either — the box says "—" and is not
 * pressable, rather than repeating a last-known state this one would act on. */
function tradeTile(payload, click) {
  const tr = (payload || {}).trading || null;
  const armed = !!(tr && tr.on);
  return tile("Trading",
    (tr ? (armed ? "on" : "off") : "—") + (tr ? dot(armed) : ""),
    armed ? "neg" : "",
    switchTip(payload || {}),
    tr ? (armed ? "flash-red" : "tint-blue") : "",
    click, !payload);
}

/* How much is OPEN. Counted per account from the switch's own payload: what is held in the OTHER
 * account is real whatever mode this run is in, and it is what refuses an arming, so the tip names
 * it rather than hiding it. An account that could not be READ is not a count of zero, so the box
 * says "?" — a number is what this box is for, and a floor must not be printed as one. */
function openTile(payload, env) {
  const accounts = (payload && payload.positions) || [];
  const active = env || ((payload && payload.execution && payload.execution.env) || "paper");
  const count = (value) => accounts.filter((a) => String(a.env) === value)
    .reduce((total, a) => total + Number(a.count || 0), 0);
  const mine = count(active);
  const others = ["live", "paper"]
    .filter((value) => value !== active && count(value) > 0)
    .map((value) => `${count(value)} in ${value}`);
  const blind = accounts.filter((a) => a.known === false).map((a) => a.env);
  const unknown = Number((payload && payload.unknown_count) || 0);
  const unreadable = blind.indexOf(active) > -1;

  let tip;
  if (unreadable) {
    tip = `the ${active} account could not be read — what it holds is unknown`;
  } else {
    tip = mine
      ? `${mine} position(s) in the ${active} account` + (others.length ? ` — and ${others.join(", ")}` : "")
      : "nothing is held in the account being traded";
    const floors = [];
    if (blind.length) floors.push(`the ${blind.join(" and ")} account(s) could not be read`);
    if (unknown) floors.push(`${unknown} could not be attributed to an account`);
    if (floors.length) tip += ` — ${floors.join(", ")}, so this count is a floor rather than the truth`;
  }
  return tile("Open", esc(unreadable ? "?" : String(mine)), "", tip);
}

/* What the master-switch box will do, or why it cannot — its hover hint, and the same words on both
 * pages. It is the one place that quotes a FAILED credential check and the freshness warning, and
 * both of those decide whether arming is possible at all. Returns a plain string, so the wording can
 * be exercised without a DOM. */
function switchTip(d) {
  const exec = d.execution || {};
  const tr = d.trading || {};
  const ver = d.verification || {};
  const fresh = d.freshness || {};
  const env = String(exec.env || "").toUpperCase();
  let tip = tr.on
    ? `Trading is ON (${env}) for ${d.strategy || "this strategy"} — click to stop`
    : !exec.ok
      ? `Trading cannot start — ${exec.message}`
      : ver.verified
        // Verified is not a promise: the check is repeated on every attempt, so a key revoked an
        // hour ago cannot be armed from a green light that is stale.
        ? `Start sending orders for ${d.strategy || "the active strategy"} — the ${env} credentials are re-checked first`
        : ver.has_verdict && ver.message
          // A check that FAILED is quoted: there is nothing to press first, so pointing at the
          // Validate button would send the operator in a circle.
          ? `Trading cannot start unless the ${env} credentials work — ${ver.message} They are re-checked when you switch it on.`
          : `Start sending orders for ${d.strategy || "the active strategy"} — the ${env} credentials are checked when you switch it on`;
  // A process running older gate code than the files on disk will answer the switch with last
  // week's rules. Nothing on screen would show it, so the tooltip says it.
  if (fresh.stale && fresh.message) tip += ` ⚠ ${fresh.message}`;
  return tip;
}

/* Move the orders to `deps.env`, and answer whether the mode actually changed.
 *
 * ONE path, because the confirmation standing between a click and real money must not exist twice:
 * the dashboard's Mode box and the log page's are two screens onto the same decision. `from` is what
 * the caller last read — what it puts its own control back to when this answers false — and
 * `reload` is the caller's own re-read, because the two pages watch different things. */
async function flipEnv(deps) {
  const api = deps.api;
  const confirmDialog = deps.confirmDialog;
  const flashToast = deps.flashToast;
  const env = deps.env;
  const from = deps.from;
  if (!env || env === from) return false;
  let r = null;
  try {
    if (env === "live") {
      const ok = await confirmDialog({
        title: "Route orders to the LIVE account?",
        messageHtml:
          "Every order for this strategy will go to your <b>real</b> Alpaca account. " +
          "Nothing is sent until you turn trading on.",
        confirmText: "Use the live account",
      });
      if (!ok) return false;
    }
    r = await api("/api/v1/execution/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ env: env }),
    });
  } catch (err) {
    // The write never landed, so neither page may start reporting the new mode.
    flashToast(`Could not change the environment: ${err.message}`, "warn");
    return false;
  }
  if (r && r.ok === false) {
    flashToast((r.errors || []).join("; ") || r.message || "Could not change the environment", "warn");
    return false;
  }
  flashToast(r && r.message ? r.message : "Environment updated", "ok");
  if (deps.reload) await deps.reload();
  return true;
}

  return {
    flip,
    waitForLoop,
    tile,
    dot,
    envTile,
    tradeTile,
    openTile,
    switchTip,
    flipEnv,
  };
})();
