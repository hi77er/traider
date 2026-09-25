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


def test_it_is_offered_only_when_there_is_a_lock(signout):
    assert signout["withoutTheLock"] is False, "no PIN, no sign-out, no dead button"
    assert signout["withTheLock"] is True
