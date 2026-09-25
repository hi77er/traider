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

  // The card is one thing with three jobs, because they are three entries into the same secret:
  // unlock with it, create the only one there is, or replace it. Each mode is a list of entries —
  // what the dots are collecting at this step — and the step machine below walks it.
  const FLOWS = {
    unlock: ["current"],
    create: ["new", "confirm"],
    change: ["current", "new", "confirm"],
  };

  // The server's own three rules, mirrored so a mistake is answered without a round trip. The
  // server's copy is the one that decides; this one only has to agree with it.
  const WEAK_PINS = ["1234", "123456", "12345678", "87654321"];

  const state = {
    mounted: false,
    overlay: null,
    locked: false,
    reason: "",
    pin: "",
    busy: false,
    mode: "unlock",
    step: 0,
    currentPin: "",
    newPin: "",
    hasPin: true,
    page: false,
    started: false,
    dismissable: false,
    status: null,
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
    const sub = el("p", "lock-sub", "");
    const hint = el("p", "lock-hint");
    const dots = el("div", "lock-dots empty");
    const keys = el("div", "lock-keys");
    const message = el("p", "lock-message");
    const note = el("p", "lock-note");
    const links = el("div", "lock-links");

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
    card.appendChild(hint);
    card.appendChild(dots);
    card.appendChild(keys);
    card.appendChild(message);
    card.appendChild(note);
    card.appendChild(links);
    card.appendChild(el("div", "lock-foot", "Local portal · the bot keeps running while locked"));

    card._parts = { sub, hint, dots, message, note, keys, links, card };
    return card;
  }

  /* ---------- what the card is asking for ---------- */

  function enterLabel() {
    if (state.mode === "unlock") return "Unlock";
    if (state.mode === "create") return state.step === 0 ? "Continue" : "Set PIN";
    return state.step === 2 ? "Save PIN" : "Continue";
  }

  function subText() {
    if (state.mode === "unlock") return "Enter your PIN to unlock the portal.";
    if (state.mode === "create") {
      return state.step === 0
        ? "No PIN is set on this portal yet — choose one."
        : "Enter the same PIN again to confirm it.";
    }
    if (state.step === 0) return "Enter your current PIN.";
    if (state.step === 1) return "Choose the new PIN.";
    return "Enter the new PIN again to confirm it.";
  }

  /* The line above the dots: the warning that belongs to a create form, or where we are in a
   * multi-step flow. Both are the same slot because the card has one place to say one thing. */
  function hintText() {
    if (state.mode === "unlock") return "";
    const total = FLOWS[state.mode].length;
    const step = state.step + 1;
    if (state.mode === "create" && state.step === 0) {
      return "Whoever reaches this page before a PIN exists can set it — do it now. "
        + `Step ${step} of ${total}.`;
    }
    return `Step ${step} of ${total}.`;
  }

  /* The ways out of whatever the card is currently asking: the two things anybody wants from it
   * besides the PIN itself. */
  function linksFor() {
    const links = [];
    // A card a PAGE raised (the Security card's buttons) is not a gate — the session is alive and
    // nothing is locked — so it has to be closable, or the overlay is a trap.
    if (state.dismissable) {
      links.push(["Cancel", () => unlock()]);
      return links;
    }
    if (state.mode === "unlock") {
      if (state.hasPin) links.push(["Change PIN", () => show("change")]);
      if (state.page && state.status && state.status.signed_in) {
        links.push(["Go to the dashboard →", () => { root.location.assign("/"); }]);
      }
    } else if (state.mode === "change") {
      links.push(["← Back", () => show(state.hasPin ? "unlock" : "create")]);
    }
    return links;
  }

  /* The two things somebody looking at this card would otherwise have to guess: what a PIN may be,
   * and what to do when it has been forgotten. The second one is TEXT and not a button on purpose —
   * a "reset it" button on a public page is the bypass the CLI exists to avoid, while a line naming
   * the command is no help at all to anybody who cannot already run it. */
  function noteText() {
    if (state.mode === "unlock") {
      return "Forgotten it? On the machine that runs the bot — the one with data/ beside it — run "
        + ".venv/bin/python -m src.web.auth reset";
    }
    if (state.mode === "create") {
      return "4-12 digits, and longer is better. Not one digit repeated, and not 1234 or 123456. "
        + "Stored salted and hashed, and never shown again.";
    }
    return "4-12 digits, and longer is better. Not one digit repeated, and not 1234 or 123456. "
      + "Changing it signs out every OTHER session, so this one stays signed in.";
  }

  function paint() {
    if (!state.overlay) return;
    const { sub, hint, dots, message, note, keys, links, card } = state.overlay._parts;
    sub.textContent = subText();
    const hint_ = hintText();
    hint.textContent = hint_;
    hint.className = "lock-hint" + (hint_ ? "" : " empty");
    note.textContent = noteText();
    dots.className = "lock-dots" + (state.pin ? "" : " empty");
    dots.replaceChildren(...[...state.pin].map(() => el("span", "lock-dot")));
    keys.querySelectorAll("button").forEach((button) => {
      button.disabled = state.busy;
    });
    const enter = keys.querySelector("[data-key='enter']");
    if (enter) enter.textContent = enterLabel();
    links.replaceChildren();
    linksFor().forEach(([label, handler]) => {
      const link = el("button", "lock-link", label);
      link.type = "button";
      link.addEventListener("click", handler);
      links.appendChild(link);
    });
    message.textContent = state.reason;
    message.className = "lock-message" + (/lock|wrong|refused/i.test(state.reason) ? " bad" : "");
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

  /* The server's three rules, in the browser's words. Instant feedback only — the answer that
   * matters is the one the server gives, and it is asked anyway. */
  function pinProblem(pin) {
    const text = String(pin || "").trim();
    if (!/^[0-9]+$/.test(text)) return "a PIN is digits only";
    if (text.length < 4 || text.length > 12) return "a PIN is 4-12 digits";
    if (/^([0-9])\1+$/.test(text)) return "that is one digit repeated — pick something else";
    if (WEAK_PINS.indexOf(text) >= 0) return text + " is the first PIN anyone tries";
    return null;
  }

  /* Something worth saying once the card is GONE — a PIN that was set, a PIN that changed. Its own
   * element rather than the pages' toasts: this is the auth UI's message, it has to work on all five
   * pages, and a page that forgot to expose a toaster must not swallow it. */
  function say(text) {
    const doc = root.document;
    if (!text || !doc || typeof doc.createElement !== "function" || !doc.body) return;
    let toast = typeof doc.querySelector === "function" ? doc.querySelector("[data-lock-toast]") : null;
    if (!toast) {
      toast = el("div", "lock-toast");
      toast.setAttribute("data-lock-toast", "1");
      doc.body.appendChild(toast);
    }
    toast.textContent = text;
    toast.className = "lock-toast up";
    if (typeof root.setTimeout === "function") {
      root.setTimeout(() => { toast.className = "lock-toast"; }, 5000);
    }
  }

  function refuse(text) {
    state.busy = false;
    state.pin = "";
    state.reason = text;
    paint();
    shake();
  }

  /* Why a request failed, in words that name the cause. A 404 is worth calling out by name: it
   * means the running dashboard is OLDER CODE than this page — no endpoint for the request — and
   * without this it arrives as "that was refused", which reads exactly like a rejected PIN and
   * sends the operator hunting for a problem with what they typed. */
  function refusalText(answer, fallback) {
    const payload = answer && answer.payload;
    if (payload && payload.reason) return payload.reason;
    const status = answer && answer.status;
    if (status === 404 || status === 405) {
      return "the dashboard has no such request — it is running older code than this page. "
        + "Restart the dashboard and try again.";
    }
    return status ? fallback + " (the portal answered " + status + ")" : fallback;
  }

  /* Move to the next entry of the flow: same card, different question. */
  function stepTo(role) {
    state.step = FLOWS[state.mode].indexOf(role);
    state.pin = "";
    state.reason = "";
    paint();
    const parts = state.overlay && state.overlay._parts;
    if (parts && parts.card && parts.card.focus) parts.card.focus();
  }

  /* One key, five meanings, and which one depends entirely on where in the flow the card is. */
  async function advance() {
    const role = FLOWS[state.mode][state.step];
    if (state.mode === "unlock") return sendUnlock();

    if (role === "current") {
      // The current PIN is checked HERE, not at the end. A wrong one is answered where it was
      // typed, the last step cannot then fail on something already passed — and proving it opens a
      // session, which is exactly what the change endpoint is about to want.
      state.busy = true;
      state.reason = "";
      paint();
      let answer;
      try {
        answer = await post("/api/v1/auth/login", { pin: state.pin });
      } catch (err) {
        return refuse("could not reach the portal (" + err.message + ")");
      }
      if (!answer.payload || !answer.payload.ok) {
        return refuse(refusalText(answer, "that PIN was refused"));
      }
      state.currentPin = state.pin;
      state.busy = false;
      return stepTo("new");
    }

    if (role === "new") {
      const problem = pinProblem(state.pin);
      if (problem) return refuse(problem);
      state.newPin = state.pin;
      return stepTo("confirm");
    }

    if (state.pin !== state.newPin) {
      state.newPin = "";
      state.step = FLOWS[state.mode].indexOf("new");
      return refuse("the two did not match — enter the new PIN again");
    }
    return sendNew(state.pin);
  }

  async function sendUnlock() {
    state.busy = true;
    state.reason = "";
    paint();
    let answer;
    try {
      answer = await post("/api/v1/auth/login", { pin: state.pin });
    } catch (err) {
      return refuse("could not reach the portal (" + err.message + ")");
    }
    state.busy = false;
    if (answer.payload && answer.payload.ok) {
      state.pin = "";
      unlock();
      return;
    }
    refuse(refusalText(answer, "that PIN was refused"));
  }

  async function sendNew(pin) {
    const created = state.mode === "create";
    const path = created ? "/api/v1/auth/setup" : "/api/v1/auth/change";
    const body = created ? { pin } : { current: state.currentPin, pin };
    state.busy = true;
    state.reason = "";
    paint();
    let answer;
    try {
      answer = await post(path, body);
    } catch (err) {
      return refuse("could not reach the portal (" + err.message + ")");
    }
    state.busy = false;
    if (!answer.payload || !answer.payload.ok) {
      // A card drawn when there was no PIN, on a portal that has one now: the operator is holding a
      // PIN they believe in, and this form can never work. So ask the real question with the PIN
      // they just typed rather than leaving them on it — twice, as it happens, which is how this
      // was reported.
      if (created && answer.status === 409) return unlockWith(state.pin);
      return refuse(refusalText(answer, "that was refused"));
    }
    const unchanged = Boolean(answer.payload.unchanged);
    state.pin = "";
    unlock();
    if (created) say("The PIN is set — the portal will ask for it from now on.");
    else if (unchanged) say("That is already your PIN — nothing changed.");
    else say("PIN changed. Every other session is now signed out.");
  }

  /* "There is already a PIN" means the question being asked was the wrong one. Submit what was just
   * typed as an unlock instead — a real attempt, counted like any other, because it is one. */
  async function unlockWith(pin) {
    state.hasPin = true;
    show("unlock", { reason: "This portal already has a PIN — checking the one you just typed." });
    state.pin = pin;
    paint();
    return sendUnlock();
  }

  async function submit() {
    if (state.busy || !state.pin) return;
    return advance();
  }

  /* ---------- locking and unlocking ---------- */

  function build() {
    // No DOM (a node harness): the card is skipped and the LOGIC still runs, which is what the
    // tests are about — whether a poll counts as a person, and when the lock comes up.
    if (!root.document || typeof root.document.createElement !== "function") return null;
    const overlay = el("div", "lock-overlay");
    const card = buildCard();
    overlay.appendChild(card);
    // Every reader below — paint, shake, the focus on lock — reaches the parts through the OVERLAY,
    // so this is the one line that makes the card findable once it is on the page.
    overlay._parts = card._parts;
    state.overlay = overlay;
    if (root.document.body) root.document.body.appendChild(overlay);
    return overlay;
  }

  function show(mode, options) {
    const opts = options || {};
    if (!state.overlay) build();
    state.locked = true;
    state.dismissable = Boolean(opts.dismiss);
    setMode(mode);
    state.reason = opts.reason || "";
    if (state.overlay) state.overlay.hidden = false;
    paint();
    if (opts.focus !== false && state.overlay) {
      const card = state.overlay._parts.card;
      if (card.focus) card.focus();
    }
    return state.overlay;
  }

  function setMode(mode) {
    state.mode = FLOWS[mode] ? mode : "unlock";
    state.step = 0;
    state.pin = "";
    state.currentPin = "";
    state.newPin = "";
  }

  function lock(reason, options) {
    if (state.locked && state.overlay) return;
    show(state.hasPin ? "unlock" : "create", {
      reason: reason || "Your session ended. Enter your PIN to continue.",
      focus: !(options && options.focus === false),
    });
    remember(true);
  }

  function unlock() {
    state.locked = false;
    state.reason = "";
    setMode("unlock");
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

  /* The Session monitor's Security card. The template owns the box — it decides where the card
   * sits, and it sits there hidden until there is something to say — while this owns the contents,
   * because the behaviour belongs with the component that knows the state.
   *
   * With the lock OFF this is not decoration: "no PIN" is the one state in which anybody who can
   * reach the address can place an order, so the card says so and offers the way out of it. */
  function mountSecurity() {
    const doc = root.document;
    if (!doc || typeof doc.querySelector !== "function") return null;
    const card = doc.querySelector("[data-security]");
    if (!card) return null;

    const status = state.status || {};
    const enabled = Boolean(status.enabled);
    const line = card.querySelector("[data-security-state]");
    const note = card.querySelector("[data-security-note]");
    const actions = card.querySelector("[data-security-actions]");
    const changed = typeof status.updated_at === "string" && status.updated_at
      ? " · changed " + status.updated_at.replace("T", " ").replace("Z", " UTC")
      : "";

    if (line) line.textContent = enabled ? "PIN set" + changed : "No PIN — the portal is open";
    if (note) {
      note.textContent = enabled
        ? "A session ends after " + Math.round((Number(status.idle_seconds) || 900) / 60)
          + " minutes without activity. None of this touches the bot: it runs whether or not "
          + "anybody is signed in."
        : "Anyone who can reach this address can arm the bot, place an order or move the switch. "
          + "Set a PIN to close that door.";
    }

    if (actions) {
      actions.replaceChildren();
      if (enabled) {
        const change = el("button", "ghost small", "Change PIN");
        change.type = "button";
        change.setAttribute("data-change-pin", "1");
        change.addEventListener("click", () => show("change", { dismiss: true }));
        actions.appendChild(change);

        const out = el("button", "ghost small", "Sign out everywhere");
        out.type = "button";
        out.setAttribute("data-sign-out-everywhere", "1");
        out.addEventListener("click", async () => {
          out.disabled = true;
          let answer = null;
          try {
            answer = await post("/api/v1/auth/sign-out-everywhere");
          } catch (_) { /* reported below as the same failure it is */ }
          out.disabled = false;
          if (!answer || !answer.payload || !answer.payload.ok) {
            say((answer && answer.payload && answer.payload.reason)
              || "Sign out everywhere did not work.");
            return;
          }
          say("Signed out everywhere else. This browser is still signed in.");
        });
        actions.appendChild(out);
      } else {
        const set = el("button", "ghost small", "Set a PIN");
        set.type = "button";
        set.setAttribute("data-set-pin", "1");
        set.addEventListener("click", () => show("create", { dismiss: true }));
        actions.appendChild(set);
      }
    }
    card.hidden = false;
    return card;
  }

  async function start(options) {
    const settings = options || {};
    // Recorded so the automatic boot below can step aside for a page that starts the card ITSELF —
    // `/login` does, because it decides which of the three modes applies. Starting it a second time
    // with no options would take the card straight back down.
    state.started = true;
    state.onUnlock = settings.onUnlock || null;
    state.page = Boolean(settings.page);
    if (!state.overlay) build();
    let status = null;
    try {
      status = await (await fetch("/api/v1/auth/status")).json();
    } catch (err) {
      return { enabled: false, signed_in: true, error: err.message };
    }
    state.status = status || {};
    state.hasPin = Boolean(status && status.enabled);
    mountSecurity();

    if (!state.hasPin) {
      // No PIN yet. On the lock page that is a job to do, and it is the only way a PIN is ever
      // created from a browser. On every other page it is simply the absence of a lock, and the
      // page behaves exactly as it did before there was one.
      if (state.page) show("create");
      else unlock();
      return state.status;
    }

    if (!state.page) {
      installSignOut();
      beginIdleWatch(state.status.idle_seconds);
      watchStorage();
    }

    const signedIn = Boolean(status && status.signed_in);
    const lockedUntil = status && status.locked_until;
    if (!signedIn || lockedUntil) {
      // No reason line unless there is one worth giving: the card's own sentence already says to
      // enter the PIN, and printing the same words twice reads like two different instructions.
      show("unlock", {
        reason: lockedUntil ? "Too many wrong PINs. Try again in a few minutes." : "",
      });
      remember(true);
      return state.status;
    }

    if (state.page) {
      // Here on purpose and already through the door: the other thing this page does is change the
      // PIN, so that is what it offers rather than a keypad with nothing to unlock.
      show("unlock", { reason: "You are already signed in." });
    }
    return state.status;
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
    boot,
    installSignOut,
    isLocked,
    lock,
    mount: build,
    mountSecurity,
    open: show,
    press,
    setMode,
    start,
    state,
    submit,
    unlock,
    watchFetch,
  };

  // A real page boots itself; the node harness (a fake document, with no ``readyState``) does not,
  // so it can drive the component step by step instead — and drive ``boot`` by hand, which is the
  // only way to prove that it steps aside for a page that started the card itself.
  function boot() {
    watchFetch();
    if (!state.started) start();
  }

  if (typeof document !== "undefined" && typeof document.readyState === "string") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})(typeof window !== "undefined" ? window : globalThis);
