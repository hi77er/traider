"""The lock screen's logic: what counts as somebody being there, and when the card comes up.

Asked for in as many words — fifteen minutes without USER activity locks the portal, and the loop
ticking, the page polling and its own clock redrawing must not count as activity. So the two things
pinned hardest here are negative: a fetch and a re-render do NOT reset the clock, and a page nobody
is touching does NOT slide the session. Those are the failures that would look like a working lock.

The component's DOM is skipped (``build`` returns nothing without a real document), which is what
lets a node harness drive the logic: the keypad still fills, the clock still runs, and whether the
card is up is ``Auth.isLocked()``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTH_JS = ROOT / "src" / "web" / "static" / "auth.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the lock screen"
)

HARNESS = r"""
// ---- a browser-shaped world, without a DOM ---------------------------------
let NOW = Date.parse("2026-09-25T18:00:00+00:00");
Date.now = () => NOW;
const advance = (minutes) => { NOW += minutes * 60 * 1000; };

const timers = [];
global.setInterval = (cb, ms) => { const timer = { cb, ms }; timers.push(timer); return timer; };
global.clearInterval = (timer) => {
  const at = timers.indexOf(timer);
  if (at >= 0) timers.splice(at, 1);
};
const listeners = {};
global.addEventListener = (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); };
function fire(name, event) { (listeners[name] || []).forEach((fn) => fn(event || {})); }
// The page's hidden/visible flag, and the timer the harness runs by hand.
global.document = { hidden: false };
const tick = () => timers.forEach((timer) => timer.cb());

global.localStorage = {
  store: {}, setItem(k, v) { this.store[k] = v; }, getItem(k) { return this.store[k]; },
};

const calls = [];
let reply = { status: 200, body: { ok: true } };
global.fetch = (path) => {
  calls.push(path);
  return Promise.resolve({
    status: reply.status, json: () => Promise.resolve(reply.body),
  });
};

const out = {};
Auth.watchFetch();
Auth.beginIdleWatch(900);
out.checkEvery = timers.length === 1 ? timers[0].ms : timers.length;

// 1. Fourteen minutes in which the page does its own work: a poll, a re-render, a tick.
calls.length = 0;
advance(14);
for (let minute = 0; minute < 14; minute += 1) {
  await global.fetch("/api/v1/log");   // the twenty-second poll
  tick();                              // the countdown redrawing
}
out.stillOpenAt14 = Auth.isLocked();
out.callsWhileIdle = calls.filter((path) => path.indexOf("heartbeat") >= 0).length;

// 2. One more silent minute, and the card is up.
advance(1);
tick();
out.lockedAt15 = Auth.isLocked();
out.reason = Auth.state.reason;

// 3. A keypress restarts the clock, so fourteen quiet minutes after one are not enough.
Auth.unlock();
fire("keydown");
advance(14);
tick();
out.openAfterAKeypress = Auth.isLocked();

// 4. A hidden tab is throttled or frozen, so coming back to it settles up immediately.
advance(20);
global.document.hidden = true;
fire("visibilitychange");
global.document.hidden = false;
fire("visibilitychange");
out.lockedOnReturn = Auth.isLocked();

// 5. The keypad builds a PIN, and backspace and clear take it back.
Auth.unlock();
["4", "8", "2", "1"].forEach((key) => Auth.press(key));
out.pin = Auth.state.pin;
Auth.press("back");
out.afterBackspace = Auth.state.pin;
Auth.press("clear");
out.afterClear = Auth.state.pin;

// 6. Only a 401 raises the card — wherever it came from.
reply = { status: 200, body: {} };
await global.fetch("/api/v1/log");
out.openAfterA200 = Auth.isLocked();
reply = { status: 401, body: {} };
await global.fetch("/api/v1/log");
out.lockedBy401 = Auth.isLocked();
out.afterResponse401 = Auth.afterResponse(401);
out.afterResponse200 = Auth.afterResponse(200);

// 7. Submitting: the server's own words keep the card up, the right PIN lifts it.
calls.length = 0;
Auth.state.pin = "0000";
reply = { status: 401, body: { ok: false, reason: "wrong PIN — 4 attempt(s) left" } };
await Auth.submit();
out.afterWrongPin = { locked: Auth.isLocked(), reason: Auth.state.reason, pin: Auth.state.pin };
Auth.state.pin = "4821";
reply = { status: 200, body: { ok: true } };
await Auth.submit();
out.afterRightPin = { locked: Auth.isLocked(), posted: calls.slice() };

// 8. A busy page DOES slide the session: that half is what keeps a working session alive.
Auth.beginIdleWatch(900);
calls.length = 0;
for (let minute = 0; minute < 10; minute += 1) {
  fire("keydown");
  advance(1);
  tick();
}
out.heartbeatsWhileActive = calls.filter((path) => path.indexOf("heartbeat") >= 0).length;

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def lock(tmp_path_factory) -> dict:
    script = tmp_path_factory.mktemp("lockscreen") / "lock.mjs"
    script.write_text(AUTH_JS.read_text(encoding="utf-8") + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "lock-screen harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_clock_is_checked_every_second(lock):
    assert lock["checkEvery"] == 1000


def test_a_poll_is_not_a_person(lock):
    """The one the request was about: the page's own traffic must not hold the lock open."""
    assert lock["stillOpenAt14"] is False
    assert lock["callsWhileIdle"] == 0, "an untouched page must not slide the session either"


def test_fifteen_quiet_minutes_put_the_card_up(lock):
    assert lock["lockedAt15"] is True
    assert "15 minutes" in lock["reason"]


def test_a_keypress_restarts_the_clock(lock):
    assert lock["openAfterAKeypress"] is False


def test_returning_to_a_hidden_tab_settles_up_at_once(lock):
    assert lock["lockedOnReturn"] is True


def test_the_keypad_builds_a_pin_and_can_take_it_back(lock):
    assert lock["pin"] == "4821"
    assert lock["afterBackspace"] == "482"
    assert lock["afterClear"] == ""


def test_only_a_401_raises_the_card(lock):
    assert lock["openAfterA200"] is False
    assert lock["lockedBy401"] is True
    assert lock["afterResponse401"] is True
    assert lock["afterResponse200"] is False


def test_a_wrong_pin_keeps_the_card_up_and_says_why(lock):
    assert lock["afterWrongPin"]["locked"] is True
    assert "4 attempt" in lock["afterWrongPin"]["reason"]
    assert lock["afterWrongPin"]["pin"] == "", "and the field is cleared"


def test_the_right_pin_lifts_it(lock):
    assert lock["afterRightPin"]["locked"] is False
    assert lock["afterRightPin"]["posted"][-1] == "/api/v1/auth/login"


def test_a_busy_page_does_slide_the_session(lock):
    """The other half: while somebody is there, the session is kept alive on purpose."""
    assert lock["heartbeatsWhileActive"] >= 2, lock["heartbeatsWhileActive"]


# ---------------------------------------------------------------------------
# the sign-out button, and where it lands
# ---------------------------------------------------------------------------
# A very small DOM: elements with children, attributes and listeners, and a querySelector that
# understands the three selectors the component uses. Enough to answer the question that matters —
# does the button end up in the header's own top-RIGHT group — including on the Session monitor,
# whose header has no group at all.
SIGN_OUT_HARNESS = r"""
function fakeEl(tag) {
  const classes = new Set();
  const node = {
    tagName: String(tag).toUpperCase(), children: [], attrs: {}, listeners: {},
    className: "", type: "", innerHTML: "", disabled: false, hidden: false,
    dataset: {}, style: {}, offsetWidth: 0,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const want = force === undefined ? !classes.has(name) : Boolean(force);
        if (want) classes.add(name);
        else classes.delete(name);
        return want;
      },
    },
    appendChild(child) { node.children.push(child); return child; },
    replaceChildren() { node.children = []; },
    setAttribute(key, value) { node.attrs[key] = value; },
    addEventListener(name, fn) { (node.listeners[name] = node.listeners[name] || []).push(fn); },
    querySelector(selector) { return find(node, selector); },
    querySelectorAll() { return []; },
    focus() { node.focused = true; },
    click() { (node.listeners.click || []).forEach((fn) => fn()); },
  };
  return node;
}

function matches(node, selector) {
  if (selector === "header") return node.tagName === "HEADER";
  if (selector === ".header-actions") return node.className === "header-actions";
  if (selector === "[data-sign-out]") return Boolean(node.attrs["data-sign-out"]);
  return false;
}

function find(node, selector) {
  if (matches(node, selector)) return node;
  for (const child of node.children || []) {
    const found = find(child, selector);
    if (found) return found;
  }
  return null;
}

function world(withActions) {
  const header = fakeEl("header");
  header.appendChild(fakeEl("div")).className = "header-left";
  if (withActions) header.appendChild(fakeEl("div")).className = "header-actions";
  const body = fakeEl("body");
  body.appendChild(header);
  global.document = {
    body,
    hidden: false,
    createElement: fakeEl,
    querySelector: (selector) => find(body, selector),
    addEventListener: () => {},
  };
  return { header, body };
}

const calls = [];
let status = { enabled: true, signed_in: true, idle_seconds: 900 };
let logoutFails = false;
global.fetch = (path) => {
  calls.push(path);
  if (path.indexOf("logout") >= 0 && logoutFails) {
    return Promise.reject(new Error("the portal is unreachable"));
  }
  const body = path.indexOf("status") >= 0 ? status : { ok: true };
  return Promise.resolve({ status: 200, json: () => Promise.resolve(body) });
};
global.location = { assigned: [], assign(url) { this.assigned.push(url); } };
global.setInterval = () => 1;
global.clearInterval = () => {};
global.addEventListener = () => {};
global.localStorage = { store: {}, setItem() {}, getItem() { return undefined; } };

const out = {};

// 1. A header that already has an actions group: the button joins it, last, which is the corner.
let page = world(true);
Auth.installSignOut();
let actions = find(page.body, ".header-actions");
out.withGroup = {
  groups: page.header.children.filter((child) => child.className === "header-actions").length,
  last: actions.children[actions.children.length - 1].className,
  insideActions: actions.children.filter((child) => child.attrs["data-sign-out"]).length,
};

// 2. The Session monitor's header is identity only, so the group is created for it.
page = world(false);
Auth.installSignOut();
actions = find(page.body, ".header-actions");
out.withoutGroup = {
  created: actions !== null,
  groups: page.header.children.filter((child) => child.className === "header-actions").length,
  insideActions: actions.children.filter((child) => child.attrs["data-sign-out"]).length,
};

// 3. Called twice, there is still one button.
Auth.installSignOut();
let buttons = [];
(function walk(node) {
  if (node.attrs && node.attrs["data-sign-out"]) buttons.push(node);
  (node.children || []).forEach(walk);
})(page.body);
out.installedTwice = buttons.length;

// 4. Clicking it signs out and leaves.
calls.length = 0;
global.location.assigned = [];
buttons[0].click();
await new Promise((resolve) => setImmediate(resolve));
out.click = { calls: calls.slice(), went: global.location.assigned.slice(), disabled: buttons[0].disabled };

// 5. ...and a logout that fails still leaves: the session is not worth being stuck behind.
page = world(true);
const fresh = Auth.installSignOut();
logoutFails = true;
calls.length = 0;
global.location.assigned = [];
fresh.click();
await new Promise((resolve) => setImmediate(resolve));
out.failedClick = { went: global.location.assigned.slice() };
logoutFails = false;

// 6. Only a portal WITH a PIN offers it. No lock, nothing to sign out of.
page = world(true);
status = { enabled: false, signed_in: true };
await Auth.start();
out.withoutTheLock = find(page.body, "[data-sign-out]") !== null;

status = { enabled: true, signed_in: true };
page = world(true);
await Auth.start();
out.withTheLock = find(page.body, "[data-sign-out]") !== null;

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def signout(tmp_path_factory) -> dict:
    script = tmp_path_factory.mktemp("signout") / "signout.mjs"
    script.write_text(AUTH_JS.read_text(encoding="utf-8") + SIGN_OUT_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "sign-out harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_button_joins_the_headers_own_actions_group(signout):
    """Last child of the right-hand group, which is what `space-between` puts in the corner."""
    assert signout["withGroup"]["groups"] == 1, "not a second group"
    assert signout["withGroup"]["insideActions"] == 1
    assert signout["withGroup"]["last"] == "sign-out"


def test_a_header_with_no_actions_group_gets_one(signout):
    """The Session monitor's bar is identity only — the button is still top-right."""
    assert signout["withoutGroup"]["created"] is True
    assert signout["withoutGroup"]["groups"] == 1
    assert signout["withoutGroup"]["insideActions"] == 1


def test_it_is_installed_once(signout):
    assert signout["installedTwice"] == 1


def test_clicking_it_signs_out_and_leaves(signout):
    assert signout["click"]["calls"] == ["/api/v1/auth/logout"]
    assert signout["click"]["went"] == ["/login"]
    assert signout["click"]["disabled"] is True, "a second click cannot fire a second time"


def test_a_logout_that_fails_still_leaves(signout):
    """The cookie is cleared server-side either way, and being stuck on a dead session is worse."""
    assert signout["failedClick"]["went"] == ["/login"]


# ---------------------------------------------------------------------------
# the card's three jobs: set the first PIN, enter it, change it
# ---------------------------------------------------------------------------
# A DOM with enough of an element in it to build and repaint the card: children, classes,
# attributes, listeners, a textContent, and a querySelector that understands the handful of
# selectors auth.js uses. It exists because the mode logic is only half a behaviour without the
# repaint — which button says "Set PIN" instead of "Unlock", and whether the card is on screen.
MODES_HARNESS = r"""
function fakeEl(tag) {
  const classes = new Set();
  const attrs = {};
  const node = {
    tagName: String(tag).toUpperCase(), children: [], attrs, listeners: {},
    className: "", type: "", innerHTML: "", textContent: "", disabled: false, hidden: false,
    style: {}, offsetWidth: 0,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const want = force === undefined ? !classes.has(name) : Boolean(force);
        if (want) classes.add(name);
        else classes.delete(name);
        return want;
      },
    },
    appendChild(child) { node.children.push(child); return child; },
    replaceChildren() { node.children = Array.prototype.slice.call(arguments); },
    setAttribute(key, value) { node.attrs[key] = String(value); },
    addEventListener(name, fn) { (node.listeners[name] = node.listeners[name] || []).push(fn); },
    querySelector(selector) { return find(node, selector); },
    querySelectorAll(selector) { return findAll(node, selector); },
    focus() { node.focused = true; },
    click() { (node.listeners.click || []).forEach((fn) => fn()); },
  };
  // A button's dataset IS its data- attributes in a browser, and auth.js leans on that for the
  // keypad and for the enter key, so the fake has to lean on it too.
  node.dataset = new Proxy({}, {
    set(_, name, value) { attrs["data-" + name] = String(value); return true; },
    get(_, name) { return attrs["data-" + name]; },
    has(_, name) { return Object.prototype.hasOwnProperty.call(attrs, "data-" + name); },
  });
  return node;
}

function matches(node, selector) {
  if (!node || !node.tagName) return false;
  if (selector === "button") return node.tagName === "BUTTON";
  if (selector === "header") return node.tagName === "HEADER";
  const cls = /^\.([a-z-]+)$/.exec(selector);
  if (cls) return String(node.className).split(/\s+/).indexOf(cls[1]) >= 0;
  const attr = /^\[([a-z-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(selector);
  if (attr) {
    const value = node.attrs[attr[1]];
    if (value === undefined) return false;
    return attr[2] === undefined || String(value) === attr[2];
  }
  return false;
}

function findAll(node, selector, found) {
  const out = found || [];
  (node.children || []).forEach((child) => {
    if (matches(child, selector)) out.push(child);
    findAll(child, selector, out);
  });
  return out;
}

function find(node, selector) {
  const all = findAll(node, selector);
  return all.length ? all[0] : null;
}

const body = fakeEl("body");
global.document = {
  body,
  hidden: false,
  createElement: fakeEl,
  querySelector: (selector) => find(body, selector),
  addEventListener: () => {},
};
global.setInterval = () => 1;
global.clearInterval = () => {};
global.setTimeout = () => 1;          // the toast's fade; not worth holding the loop open for
global.clearTimeout = () => {};
global.addEventListener = () => {};
global.localStorage = { store: {}, setItem(k, v) { this.store[k] = v; }, getItem(k) { return this.store[k]; } };
global.location = { assigned: [], assign(url) { this.assigned.push(url); } };

const calls = [];
let replies = {};
let status = { enabled: false, signed_in: false };
global.fetch = (path, options) => {
  const body_ = options && options.body ? JSON.parse(options.body) : null;
  calls.push({ path, body: body_ });
  if (path.indexOf("/status") >= 0) return Promise.resolve({ status: 200, json: () => Promise.resolve(status) });
  const reply = replies[path] || { status: 200, body: { ok: true } };
  return Promise.resolve({ status: reply.status, json: () => Promise.resolve(reply.body) });
};

const enterPin = async (pin) => {
  String(pin).split("").forEach((digit) => Auth.press(digit));
  await Auth.submit();
};
const enterKey = () => {
  const keys = find(body, ".lock-keys");
  return find(keys, "[data-key='enter']");
};
const card = () => find(body, ".lock-card");
const overlay = () => find(body, ".lock-overlay");
const showed = () => Boolean(overlay()) && overlay().hidden === false;
const posted = () => calls.filter((call) => call.path.indexOf("/status") < 0);
const last = () => posted()[posted().length - 1];

const out = {};

// 1. No PIN and the lock page: this is the create form, and it says who else could get there.
status = { enabled: false, signed_in: false };
let unlocked = 0;
await Auth.start({ page: true, onUnlock: () => { unlocked += 1; } });
out.createMode = {
  mode: Auth.state.mode, step: Auth.state.step, showing: showed(),
  sub: find(card(), ".lock-sub").textContent,
  hint: find(card(), ".lock-hint").textContent,
  note: find(card(), ".lock-note").textContent,
  enter: enterKey().textContent,
  links: find(card(), ".lock-links").children.map((child) => child.textContent),
};

// 2. Two entries that agree: the first PIN, hashed by the server, and the card stands down.
calls.length = 0;
replies["/api/v1/auth/setup"] = { status: 200, body: { ok: true, signed_in: true, created: true } };
await enterPin("4821");
out.createAfterFirst = {
  step: Auth.state.step, enter: enterKey().textContent,
  sub: find(card(), ".lock-sub").textContent, hint: find(card(), ".lock-hint").textContent,
};
await enterPin("4821");
out.createSent = { calls: calls.map((call) => call.path), body: last().body };
out.createDone = {
  showing: showed(), unlocked, toast: find(body, ".lock-toast") ? find(body, ".lock-toast").textContent : null,
};

// 3. Two entries that do not agree: nothing is sent, and the question is asked again.
calls.length = 0;
Auth.open("create");
await enterPin("4821");
await enterPin("7788");
out.createMismatch = {
  calls: calls.filter((call) => call.path.indexOf("/auth") >= 0).map((call) => call.path),
  step: Auth.state.step, reason: Auth.state.reason, pin: Auth.state.pin,
};

// 4. A PIN the server would refuse is refused here first, without a round trip.
calls.length = 0;
Auth.open("create");
await enterPin("1234");
out.createWeak = { calls: calls.length, step: Auth.state.step, reason: Auth.state.reason };

// 4b. A 404 from the server is NOT a refused PIN, however alike the two look, and the card has to
// say which one it is: it means the dashboard is running code older than this page.
calls.length = 0;
Auth.open("create");
replies["/api/v1/auth/setup"] = { status: 404, body: { detail: "Not Found" } };
await enterPin("482482");
await enterPin("482482");
out.createStaleDashboard = {
  step: Auth.state.step, reason: Auth.state.reason, showing: showed(),
  posted: calls.map((call) => call.path),
};
delete replies["/api/v1/auth/setup"];

// 4c. A create card left over from before a PIN existed, on a portal that has one now: the form can
// never work, so the same PIN is submitted as an unlock instead of asking for it a second time.
calls.length = 0;
Auth.open("create");
replies["/api/v1/auth/setup"] = { status: 409, body: { ok: false, reason: "a PIN is already set — use Change PIN" } };
replies["/api/v1/auth/login"] = { status: 200, body: { ok: true, signed_in: true } };
await enterPin("482482");
await enterPin("482482");
out.createOnALockedPortal = {
  posted: calls.map((call) => call.path),
  lastBody: last().body,
  mode: Auth.state.mode,
  showing: showed(),
};

// 4d. And when that PIN is not the one, the card is at least asking the right question.
calls.length = 0;
Auth.open("create");
replies["/api/v1/auth/login"] = { status: 401, body: { ok: false, reason: "wrong PIN — 4 attempt(s) left" } };
await enterPin("482482");
await enterPin("482482");
out.createOnALockedPortalWrongPin = {
  mode: Auth.state.mode,
  reason: Auth.state.reason,
  showing: showed(),
};
replies["/api/v1/auth/login"] = { status: 200, body: { ok: true, signed_in: true } };
Auth.unlock();

// 5. A PIN exists and the session is gone: the card is a keypad, and the keypad opens it.
// A page load starts from an unlocked component, so the harness stands the card down first: a
// single instance driven through every flow in turn is not the same thing as five page loads.
status = { enabled: true, signed_in: false, idle_seconds: 900 };
Auth.unlock();
await Auth.start({ page: true });
out.unlockMode = {
  mode: Auth.state.mode, enter: enterKey().textContent,
  links: find(card(), ".lock-links").children.map((c) => c.textContent),
  note: find(card(), ".lock-note").textContent,
  message: find(card(), ".lock-message").textContent,
  sub: find(card(), ".lock-sub").textContent,
};
calls.length = 0;
replies["/api/v1/auth/login"] = { status: 200, body: { ok: true, signed_in: true } };
await enterPin("4821");
out.unlocked = {
  showing: showed(), calls: posted().map((call) => call.path),
  mode: Auth.state.mode, step: Auth.state.step, locked: Auth.state.locked,
  busy: Auth.state.busy, pin: Auth.state.pin, reason: Auth.state.reason,
};

// 6. Change: the current PIN is checked WHERE IT IS TYPED, so a wrong one is answered there.
Auth.open("change");
out.changeAsksFirst = {
  mode: Auth.state.mode, step: Auth.state.step, sub: find(card(), ".lock-sub").textContent,
  note: find(card(), ".lock-note").textContent,
};
calls.length = 0;
replies["/api/v1/auth/login"] = { status: 401, body: { ok: false, reason: "wrong PIN — 4 attempt(s) left" } };
await enterPin("0000");
out.changeWrongCurrent = {
  step: Auth.state.step, reason: Auth.state.reason,
  call: last().path, body: last().body,
};

// 7. Right current PIN, then the new one twice: one request at the end, carrying both.
calls.length = 0;
replies["/api/v1/auth/login"] = { status: 200, body: { ok: true, signed_in: true } };
replies["/api/v1/auth/change"] = { status: 200, body: { ok: true, changed: true } };
await enterPin("4821");
out.changeAfterCurrent = { step: Auth.state.step, sub: find(card(), ".lock-sub").textContent };
await enterPin("7788");
out.changeAfterNew = { step: Auth.state.step, enter: enterKey().textContent };
await enterPin("7788");
out.changeSent = {
  paths: calls.map((call) => call.path),
  body: last().body,
  showing: showed(),
};

// 8. A FRESH page load with a live session — an empty body, nothing built yet — which is the case
// that was broken: the overlay was built visible, so every page came up wearing a lock screen.
// Then the Security card: what is set, and the two things you can do about it.
body.children = [];
let security = fakeEl("div");
security.setAttribute("data-security", "1");
security.hidden = true;
security.appendChild((() => { const n = fakeEl("span"); n.setAttribute("data-security-state", "1"); return n; })());
security.appendChild((() => { const n = fakeEl("p"); n.setAttribute("data-security-note", "1"); return n; })());
security.appendChild((() => { const n = fakeEl("div"); n.setAttribute("data-security-actions", "1"); return n; })());
body.appendChild(security);

status = { enabled: true, signed_in: true, idle_seconds: 900, updated_at: "2026-09-25T14:02:11Z", user: "owner" };
Auth.state.overlay = null;
Auth.state.locked = false;
await Auth.start();
const enabledCard = find(body, "[data-security]");
out.securityOn = {
  hidden: enabledCard.hidden,
  state: find(enabledCard, "[data-security-state]").textContent,
  note: find(enabledCard, "[data-security-note]").textContent,
  actions: find(enabledCard, "[data-security-actions]").children.map((c) => c.textContent),
  cardHidden: overlay().hidden,
  cardShown: showed(),
};
calls.length = 0;
replies["/api/v1/auth/change"] = { status: 200, body: { ok: true, changed: true } };
find(enabledCard, "[data-change-pin]").click();
out.securityChangeOpens = {
  mode: Auth.state.mode, step: Auth.state.step, showing: showed(),
  links: find(card(), ".lock-links").children.map((c) => c.textContent),
};
find(card(), ".lock-links").children[0].click();
out.securityChangeCancels = { showing: showed(), mode: Auth.state.mode };
Auth.unlock();
calls.length = 0;
replies["/api/v1/auth/sign-out-everywhere"] = { status: 200, body: { ok: true, signed_out_others: true } };
find(enabledCard, "[data-sign-out-everywhere]").click();
await new Promise((resolve) => setImmediate(resolve));
out.securitySignOutEverywhere = { calls: calls.map((call) => call.path) };

// 9. And with NO PIN, the card is the warning — plus the way out of it.
status = { enabled: false, signed_in: true };
await Auth.start();
const openCard = find(body, "[data-security]");
out.securityOff = {
  state: find(openCard, "[data-security-state]").textContent,
  note: find(openCard, "[data-security-note]").textContent,
  actions: find(openCard, "[data-security-actions]").children.map((c) => c.textContent),
};
find(openCard, "[data-set-pin]").click();
out.securitySetPinOpens = {
  mode: Auth.state.mode, step: Auth.state.step, showing: showed(),
  links: find(card(), ".lock-links").children.map((c) => c.textContent),
};

// 10. The automatic boot steps aside for a page that starts the card ITSELF. `/login` does, because
// it decides which mode applies — and starting it again with no options stands the card back down,
// which is a blank lock page. This is the one the live check caught.
const statusCalls = () => calls.filter((call) => call.path.indexOf("/status") >= 0).length;
status = { enabled: false, signed_in: false };
Auth.state.started = false;
await Auth.start({ page: true });
const before = statusCalls();
Auth.boot();
await new Promise((resolve) => setImmediate(resolve));
out.bootAfterThePageStarted = {
  askedAgain: statusCalls() - before, mode: Auth.state.mode, page: Auth.state.page,
  showing: showed(),
};

// 11. A page that did not start it is booted by it, exactly as before.
Auth.state.started = false;
Auth.unlock();
const beforeOwn = statusCalls();
Auth.boot();
await new Promise((resolve) => setImmediate(resolve));
out.bootOnItsOwn = { asked: statusCalls() - beforeOwn, mode: Auth.state.mode, page: Auth.state.page };

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def modes(tmp_path_factory) -> dict:
    script = tmp_path_factory.mktemp("modes") / "modes.mjs"
    script.write_text(AUTH_JS.read_text(encoding="utf-8") + MODES_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "lock-screen mode harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_lock_page_creates_the_first_pin_rather_than_bouncing_you_away(modes):
    """No PIN and you typed the address on purpose: that is a job, not an absence of a lock."""
    assert modes["createMode"]["mode"] == "create"
    assert modes["createMode"]["showing"] is True
    assert "choose one" in modes["createMode"]["sub"]
    assert "whoever reaches this page" in modes["createMode"]["hint"].lower()
    assert modes["createMode"]["enter"] == "Continue"


def test_the_first_pin_is_confirmed_before_it_is_sent(modes):
    assert modes["createAfterFirst"]["step"] == 1
    assert modes["createAfterFirst"]["enter"] == "Set PIN"
    assert "again" in modes["createAfterFirst"]["sub"] + modes["createAfterFirst"]["hint"]
    assert modes["createSent"]["calls"] == ["/api/v1/auth/setup"]
    assert modes["createSent"]["body"] == {"pin": "4821"}, "the confirmed one, not the first"


def test_setting_the_pin_signs_you_in_and_takes_the_card_away(modes):
    assert modes["createDone"]["showing"] is False
    assert modes["createDone"]["unlocked"] == 1
    assert "PIN is set" in modes["createDone"]["toast"]


def test_the_create_card_says_what_a_pin_may_be(modes):
    """Asked for in as many words: somebody choosing a PIN should not have to guess its length."""
    note = modes["createMode"]["note"]
    assert "4-12 digits" in note
    assert "longer is better" in note
    assert "one digit repeated" in note and "1234" in note
    assert "never shown again" in note, "and what will happen to it"


def test_the_change_card_says_what_the_change_costs(modes):
    note = modes["changeAsksFirst"]["note"]
    assert "4-12 digits" in note
    assert "every OTHER session" in note, "which is the consequence worth knowing before you do it"


def test_the_unlock_card_says_where_a_forgotten_pin_is_reset(modes):
    """Text, not a button: running it needs the machine, so naming it helps only the operator."""
    note = modes["unlockMode"]["note"]
    assert "auth reset" in note
    assert "data/" in note, "and where that command has to be run from"
    assert modes["unlockMode"]["links"] == ["Change PIN"], "no reset button, then"


def test_a_confirmation_that_does_not_match_sends_nothing(modes):
    assert modes["createMismatch"]["calls"] == []
    assert modes["createMismatch"]["step"] == 0, "and it asks for the new PIN again"
    assert "did not match" in modes["createMismatch"]["reason"]
    assert modes["createMismatch"]["pin"] == ""


def test_a_pin_the_server_would_refuse_is_answered_without_a_round_trip(modes):
    assert modes["createWeak"]["calls"] == 0
    assert modes["createWeak"]["step"] == 0
    assert "first PIN anyone tries" in modes["createWeak"]["reason"]


def test_a_stale_dashboard_is_named_as_such_and_not_as_a_refused_pin(modes):
    """The trap this was found by: a 404 and a rejected PIN produced the same words."""
    assert modes["createStaleDashboard"]["posted"] == ["/api/v1/auth/setup"], "it did ask"
    assert "Restart the dashboard" in modes["createStaleDashboard"]["reason"]
    assert "refused" not in modes["createStaleDashboard"]["reason"]
    assert modes["createStaleDashboard"]["showing"] is True, "and the PIN is still there to retry"


def test_unlock_still_asks_for_the_pin_and_offers_the_change(modes):
    assert modes["unlockMode"]["mode"] == "unlock"
    assert modes["unlockMode"]["enter"] == "Unlock"
    assert "Change PIN" in modes["unlockMode"]["links"]
    assert modes["unlocked"]["showing"] is False
    assert modes["unlocked"]["calls"] == ["/api/v1/auth/login"]


def test_the_unlock_card_asks_once_and_does_not_repeat_itself(modes):
    """Reported as "it asks me to enter it a second time": that was the CREATE card, which asks
    twice on purpose. The unlock card asks once — and says so once, not in two paragraphs."""
    assert modes["unlockMode"]["enter"] == "Unlock"
    assert modes["unlockMode"]["message"] == "", "the sentence above is the instruction"
    assert "Enter your PIN" in modes["unlockMode"]["sub"]
    assert modes["unlocked"]["calls"] == ["/api/v1/auth/login"], "one entry, one request"


def test_a_create_card_on_a_portal_that_already_has_a_pin_unlocks_with_it(modes):
    """The leftover form cannot work, so the PIN just typed is tried as the real thing rather than
    the operator being sent round the create flow — twice — and then refused."""
    assert modes["createOnALockedPortal"]["posted"] == [
        "/api/v1/auth/setup", "/api/v1/auth/login",
    ]
    assert modes["createOnALockedPortal"]["lastBody"] == {"pin": "482482"}
    assert modes["createOnALockedPortal"]["mode"] == "unlock"
    assert modes["createOnALockedPortal"]["showing"] is False, "and it worked: they are in"


def test_and_when_that_pin_is_wrong_the_card_asks_the_right_question(modes):
    assert modes["createOnALockedPortalWrongPin"]["mode"] == "unlock", "not the create form"
    assert "4 attempt" in modes["createOnALockedPortalWrongPin"]["reason"]
    assert modes["createOnALockedPortalWrongPin"]["showing"] is True


def test_changing_the_pin_asks_for_the_current_one_first(modes):
    assert modes["changeAsksFirst"]["mode"] == "change"
    assert modes["changeAsksFirst"]["step"] == 0
    assert "current PIN" in modes["changeAsksFirst"]["sub"]


def test_a_wrong_current_pin_is_answered_where_it_was_typed(modes):
    """Not at the end, after the new PIN has been typed twice."""
    assert modes["changeWrongCurrent"]["step"] == 0, "still on the current PIN"
    assert "4 attempt" in modes["changeWrongCurrent"]["reason"]
    assert modes["changeWrongCurrent"]["call"] == "/api/v1/auth/login"
    assert modes["changeWrongCurrent"]["body"] == {"pin": "0000"}


def test_the_change_sends_the_current_and_the_new_pin_together(modes):
    assert modes["changeAfterCurrent"]["step"] == 1
    assert "new PIN" in modes["changeAfterCurrent"]["sub"]
    assert modes["changeAfterNew"]["step"] == 2
    assert modes["changeAfterNew"]["enter"] == "Save PIN"
    assert modes["changeSent"]["paths"] == ["/api/v1/auth/login", "/api/v1/auth/change"]
    assert modes["changeSent"]["body"] == {"current": "4821", "pin": "7788"}
    assert modes["changeSent"]["showing"] is False


def test_the_monitor_says_what_the_pin_is_and_offers_the_two_actions(modes):
    assert modes["securityOn"]["hidden"] is False
    assert modes["securityOn"]["state"] == "PIN set · changed 2026-09-25 14:02 UTC", (
        "the minute is enough; microseconds are noise"
    )
    assert "15 minutes" in modes["securityOn"]["note"]
    assert "the bot" in modes["securityOn"]["note"], "and it says the loop is unaffected"
    assert modes["securityOn"]["actions"] == ["Change PIN", "Sign out everywhere"]


def test_a_signed_in_page_never_shows_the_lock_card(modes):
    """The card is built on every page so a 401 has somewhere to go. If it is built VISIBLE, a
    working page comes up with a lock screen over it, which is indistinguishable from being signed
    out — and that is what navigating between pages did once a PIN existed."""
    assert modes["securityOn"]["cardShown"] is False
    assert modes["securityOn"]["cardHidden"] is True


def test_the_cards_buttons_open_the_flows(modes):
    assert modes["securityChangeOpens"]["mode"] == "change"
    assert modes["securityChangeOpens"]["step"] == 0
    assert modes["securityChangeOpens"]["showing"] is True
    assert modes["securitySignOutEverywhere"]["calls"] == ["/api/v1/auth/sign-out-everywhere"]


def test_a_card_the_page_raised_can_be_dismissed(modes):
    """Nothing is locked when the Security card opens one, so the overlay must not be a trap."""
    assert modes["securityChangeOpens"]["links"] == ["Cancel"]
    assert modes["securitySetPinOpens"]["links"] == ["Cancel"]
    assert modes["securityChangeCancels"]["showing"] is False


def test_with_no_pin_the_card_says_so_and_offers_the_way_out(modes):
    assert modes["securityOff"]["state"] == "No PIN — the portal is open"
    assert "anyone who can reach this address" in modes["securityOff"]["note"].lower()
    assert modes["securityOff"]["actions"] == ["Set a PIN"]
    assert modes["securitySetPinOpens"]["mode"] == "create"
    assert modes["securitySetPinOpens"]["showing"] is True


def test_the_automatic_boot_stands_aside_for_a_page_that_started_the_card_itself(modes):
    """`/login` calls ``start({page: true})`` from its own inline script, and the automatic boot on
    DOMContentLoaded would otherwise call ``start()`` again with no options — which is a page flag of
    false, an unlock card, and nothing on screen at all."""
    assert modes["bootAfterThePageStarted"]["askedAgain"] == 0, "no second status call"
    assert modes["bootAfterThePageStarted"]["mode"] == "create"
    assert modes["bootAfterThePageStarted"]["page"] is True
    assert modes["bootAfterThePageStarted"]["showing"] is True, "the card it put up is still up"


def test_a_page_that_does_not_start_itself_is_still_booted(modes):
    assert modes["bootOnItsOwn"]["asked"] == 1
    assert modes["bootOnItsOwn"]["page"] is False, "no page flag, so the overlay is an overlay"


def test_it_is_offered_only_when_there_is_a_lock(signout):
    assert signout["withoutTheLock"] is False, "no PIN, no sign-out, no dead button"
    assert signout["withTheLock"] is True
