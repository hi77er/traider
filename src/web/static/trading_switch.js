/* TRAIDER — the master switch, in ONE place.
 *
 * Two pages carry it: the dashboard's top bar and the trading log's Trading status block. That
 * is why this is not written twice — the confirmation is the only thing standing between a
 * click and real orders on a LIVE account, and a second copy of the wording is a second chance
 * to get it wrong.
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

  return { flip, waitForLoop };
})();
