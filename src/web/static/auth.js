/* The lock screen, and the two things it decides: is a session alive, and is anybody there?
 *
 * It exists as a component rather than a page because there are two ways in and they must behave
 * identically: `/login` for a cold start, and an overlay a page raises over itself when its session
 * ends. The overlay is opaque — whatever it covers must not be readable past it.
 *
 * It is also the ONLY thing allowed to decide that a human is present. The page polls every twenty
 * seconds and the clock redraws every second; neither is a person, and if either could move the
 * session's idle clock the door would stay open for a screen nobody is sitting at.
 *
 * No DOM work happens at load: ``Auth.start()`` is what mounts it, and the node harness in
 * ``tests/test_web/test_lock_screen.py`` drives the component without ever calling it.
 */
(function (root) {
  "use strict";

  const KEYPAD = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "clear", "0", "back"];

  // How long the pages wait between activity checks. A comparison of timestamps rather than a
  // countdown of ticks, because a background tab's timers are throttled and may stop entirely.
  const CHECK_MS = 1000;
  // How often a session's idle clock is slid while somebody is actually there.
  const HEARTBEAT_MS = 240000;
  // A tab that has been hidden this long is checked on the way back rather than trusted.
  const STORAGE_KEY = "traider.lock";
  const ACTIVITY_EVENTS = ["keydown", "pointerdown", "mousedown", "wheel", "touchstart"];

  const state = {
    mounted: false,
    overlay: null,
    locked: false,
    reason: "",
    pin: "",
    busy: false,
    idleSeconds: 900,
    lastActivityAt: 0,
    lastHeartbeatAt: 0,
    timer: null,
    onUnlock: null,
    listeners: [],
  };

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const lockIcon = () => {
    const wrap = el("div", "lock-badge");
    wrap.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"'
      + ' stroke-linecap="round" aria-hidden="true">'
      + '<rect x="4" y="10.5" width="16" height="10" rx="2.5"></rect>'
      + '<path d="M8 10.5V7.6a4 4 0 1 1 8 0v2.9"></path></svg>';
    return wrap;
  };

  /* ---------- the card ---------- */

  function buildCard() {
    const card = el("div", "lock-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-label", "Enter your PIN");

    const wordmark = el("div", "lock-wordmark", "Traider");
    const sub = el("p", "lock-sub", "Enter your PIN to unlock the portal.");
    const dots = el("div", "lock-dots empty");
    const keys = el("div", "lock-keys");
    const message = el("p", "lock-message");

    KEYPAD.forEach((key) => {
      const button = el("button", "lock-key");
      button.type = "button";
      button.dataset.key = key;
      if (key === "clear") {
        button.className = "lock-key faint";
        button.textContent = "C";
        button.setAttribute("aria-label", "Clear");
      } else if (key === "back") {
        button.className = "lock-key faint";
        button.textContent = "⌫";
        button.setAttribute("aria-label", "Delete");
      } else {
        button.textContent = key;
        button.setAttribute("aria-label", key);
      }
      button.addEventListener("click", () => press(key));
      keys.appendChild(button);
    });

    const enter = el("button", "lock-key go");
    enter.type = "button";
    enter.dataset.key = "enter";
    enter.textContent = "Unlock";
    enter.style.gridColumn = "1 / -1";
    enter.addEventListener("click", () => submit());
    keys.appendChild(enter);

    card.appendChild(lockIcon());
    card.appendChild(wordmark);
    card.appendChild(sub);
    card.appendChild(dots);
    card.appendChild(keys);
    card.appendChild(message);
    card.appendChild(el("div", "lock-foot", "Local portal · the bot keeps running while locked"));

    card._parts = { sub, dots, message, keys, card };
    return card;
  }

  function paint() {
    if (!state.overlay) return;
    const { dots, message, keys, card } = state.overlay._parts;
    dots.className = "lock-dots" + (state.pin ? "" : " empty");
    dots.replaceChildren(...[...state.pin].map(() => el("span", "lock-dot")));
    keys.querySelectorAll("button").forEach((button) => {
      button.disabled = state.busy;
    });
    if (state.reason) {
      message.textContent = state.reason;
      message.className = "lock-message" + (/lock|wrong|refused/i.test(state.reason) ? " bad" : "");
    }
    card.classList.toggle("shake", false);
  }

  function shake() {
    if (!state.overlay) return;
    const card = state.overlay._parts.card;
    card.classList.remove("shake");
    void card.offsetWidth; // restart the animation instead of ignoring the second failure
    card.classList.add("shake");
  }

  function press(key) {
    if (state.busy) return;
    if (key === "clear") state.pin = "";
    else if (key === "back") state.pin = state.pin.slice(0, -1);
    else if (key === "enter") return submit();
    else if (state.pin.length < 12) state.pin += key;
    if (state.reason) state.reason = "";
    paint();
  }

  /* ---------- talking to the server ---------- */

  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* a body-less answer is fine */ }
    return { status: response.status, payload };
  }

  async function submit() {
    if (state.busy || !state.pin) return;
    state.busy = true;
    state.reason = "";
    paint();
    let answer;
    try {
      answer = await post("/api/v1/auth/login", { pin: state.pin });
    } catch (err) {
      state.busy = false;
      state.pin = "";
      state.reason = `could not reach the portal (${err.message})`;
      paint();
      return;
    }
    state.busy = false;
    if (answer.payload && answer.payload.ok) {
      state.pin = "";
      unlock();
      return;
    }
    state.pin = "";
    state.reason = (answer.payload && answer.payload.reason) || "that PIN was refused";
    paint();
    shake();
  }

  /* ---------- locking and unlocking ---------- */

  function build() {
    // No DOM (a node harness): the card is skipped and the LOGIC still runs, which is what the
    // tests are about — whether a poll counts as a person, and when the lock comes up.
    if (!root.document || typeof root.document.createElement !== "function") return null;
    const overlay = el("div", "lock-overlay");
    overlay.appendChild(buildCard());
    state.overlay = overlay;
    if (root.document.body) root.document.body.appendChild(overlay);
    return overlay;
  }

  function lock(reason, options) {
    if (state.locked) return;
    state.locked = true;
    state.reason = reason || "Your session ended. Enter your PIN to continue.";
    state.pin = "";
    if (!state.overlay) build();
    if (state.overlay) state.overlay.hidden = false;
    paint();
    remember(true);
    if (options && options.focus !== false && state.overlay) {
      const card = state.overlay._parts.card;
      if (card.focus) card.focus();
    }
  }

  function unlock() {
    state.locked = false;
    state.reason = "";
    state.pin = "";
    if (state.overlay) state.overlay.hidden = true;
    remember(false);
    state.lastActivityAt = Date.now();
    state.lastHeartbeatAt = Date.now();
    if (typeof state.onUnlock === "function") state.onUnlock();
  }

  function isLocked() {
    return state.locked;
  }

  /* ---------- the other tabs ---------- */

  function remember(locked) {
    try {
      if (root.localStorage) root.localStorage.setItem(STORAGE_KEY, locked ? "1" : "0");
    } catch (_) { /* a browser with storage switched off: this tab still locks on its own */ }
  }

  function watchStorage() {
    if (!root.addEventListener) return;
    const handler = (event) => {
      if (!event || event.key !== STORAGE_KEY) return;
      if (event.newValue === "1" && !state.locked) lock("The portal was locked in another tab.");
      if (event.newValue === "0" && state.locked) unlock();
    };
    root.addEventListener("storage", handler);
    state.listeners.push(["storage", handler]);
  }

  /* ---------- is anybody there? ---------- */

  function noteActivity() {
    state.lastActivityAt = Date.now();
  }

  function beginIdleWatch(seconds) {
    state.idleSeconds = Number(seconds) || state.idleSeconds;
    noteActivity();
    ACTIVITY_EVENTS.forEach((name) => {
      const handler = noteActivity;
      root.addEventListener(name, handler, { capture: true, passive: true });
      state.listeners.push([name, handler]);
    });
    // Coming back to the tab is something a person did — but it is not an excuse for the time that
    // passed while the tab was frozen, so the elapsed time is settled FIRST. Without that order, a
    // laptop reopened after lunch would show live positions for a moment before deciding.
    const visible = () => {
      if (root.document && root.document.hidden) return;
      const wasIdle = expired();
      noteActivity();
      if (wasIdle) {
        lock("Locked after " + Math.round(state.idleSeconds / 60) + " minutes idle.");
      }
    };
    root.addEventListener("visibilitychange", visible);
    state.listeners.push(["visibilitychange", visible]);

    if (state.timer) root.clearInterval(state.timer);
    state.timer = root.setInterval(tick, CHECK_MS);
    return true;
  }

  function expired(now) {
    const moment = now || Date.now();
    return (moment - state.lastActivityAt) / 1000 >= state.idleSeconds;
  }

  function tick() {
    if (state.locked) return;
    if (expired()) {
      lock("Locked after " + Math.round(state.idleSeconds / 60) + " minutes idle.");
      return;
    }
    maybeHeartbeat();
  }

  /* Only a page that has just seen a human tells the server so. Nothing else slides the session.
   *
   * The activity gate is the whole point: a heartbeat on a timer would keep the server's idle
   * window from ever lapsing on a page nobody is touching, and then the browser's lock would be the
   * only lock — which is exactly the half that can be walked away from. */
  function maybeHeartbeat() {
    const now = Date.now();
    if (now - state.lastHeartbeatAt < HEARTBEAT_MS) return;
    if (now - state.lastActivityAt > HEARTBEAT_MS) return;
    state.lastHeartbeatAt = now;
    post("/api/v1/auth/heartbeat").catch(() => { /* the next request will settle it */ });
  }

  /* ---------- what the pages call ---------- */

  /* ---------- the way out ----------
   *
   * Built here rather than written into five templates so the pages cannot drift, and placed in the
   * header's own actions group — creating one when the page has none (the Session monitor's header
   * is identity only). It appears ONLY when the lock is on: with no PIN set there is nothing to sign
   * out of, and a button that did nothing would be worse than no button.
   */
  function installSignOut() {
    if (!root.document || typeof root.document.querySelector !== "function") return null;
    if (typeof root.document.createElement !== "function") return null;
    if (root.document.querySelector("[data-sign-out]")) return null;
    const header = root.document.querySelector("header");
    if (!header) return null;

    let actions = header.querySelector(".header-actions");
    if (!actions) {
      actions = el("div", "header-actions");
      header.appendChild(actions);
    }

    const button = el("button", "sign-out");
    button.type = "button";
    button.setAttribute("data-sign-out", "1");
    button.setAttribute("title", "Sign out of this portal");
    button.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M15 4h3a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-3"></path>'
      + '<path d="M10 8l-4 4 4 4"></path><path d="M6 12h9"></path></svg>'
      + "<span>Sign out</span>";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await post("/api/v1/auth/logout");
      } catch (_) { /* the cookie is cleared server-side either way; leaving is safe */ }
      if (root.location && root.location.assign) root.location.assign("/login");
      else unlock();
    });
    actions.appendChild(button);
    return button;
  }

  async function start(options) {
    const settings = options || {};
    state.onUnlock = settings.onUnlock || null;
    if (!state.overlay) build();
    let status = null;
    try {
      status = await (await fetch("/api/v1/auth/status")).json();
    } catch (err) {
      return { enabled: false, signed_in: true, error: err.message };
    }
    if (!status || !status.enabled) {
      // No lock on this install: take the overlay away and stop watching the clock.
      unlock();
      return status || {};
    }
    installSignOut();
    beginIdleWatch(status.idle_seconds);
    watchStorage();
    if (!status.signed_in || status.locked_until) {
      lock(status.locked_until
        ? "Too many wrong PINs. Try again in a few minutes."
        : "Enter your PIN to unlock the portal.");
    }
    return status;
  }

  /* A 401 from anywhere means the session is gone, whatever this tab thought. */
  function afterResponse(status) {
    if (status === 401) {
      lock("Your session ended. Enter your PIN to continue.");
      return true;
    }
    return false;
  }

  /* Every 401 raises the lock, wherever it came from.
   *
   * Wrapping fetch here rather than in each page's own ``api()`` is deliberate: there are five
   * pages, a sixth must not be able to forget, and a session that ends mid-read has to lock the
   * screen the reader is actually looking at.
   */
  function watchFetch() {
    if (typeof root.fetch !== "function" || root.fetch.__traiderWrapped) return false;
    const original = root.fetch;
    const wrapped = function () {
      return original.apply(this, arguments).then((response) => {
        if (response && response.status === 401) {
          lock("Your session ended. Enter your PIN to continue.");
        }
        return response;
      });
    };
    wrapped.__traiderWrapped = true;
    root.fetch = wrapped;
    return true;
  }

  root.Auth = {
    afterResponse,
    beginIdleWatch,
    installSignOut,
    isLocked,
    lock,
    mount: build,
    press,
    start,
    state,
    submit,
    unlock,
    watchFetch,
  };

  // A real page boots itself; the node harness (a fake document, with no ``readyState``) does not,
  // so it can drive the component step by step instead.
  if (typeof document !== "undefined" && typeof document.readyState === "string") {
    const boot = () => { watchFetch(); start(); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})(typeof window !== "undefined" ? window : globalThis);
