"""Behavioural tests for the master switch's confirmation.

Turning trading ON always asks first — on the paper account as well as the live
one. Two things matter and neither is visible in a string assertion:

* it asks in BOTH environments (the paper prompt used to be skipped entirely), and
  the wording differs, because "this spends real money" and "this bot starts acting
  on the next signal" are different decisions;
* declining really writes nothing, and turning trading OFF never asks at all —
  stopping must always take one click.

The decision itself lives in ``trading_switch.js``, because the trading log page carries
the same control and one confirmation is enough for both. So the real module is lifted
into the same script as the page's handler, and the assertions are about the calls that
were made rather than about either file's text.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
APP_JS = STATIC / "app.js"
LOG_JS = STATIC / "log.js"
SWITCH_JS = STATIC / "trading_switch.js"

START_MARKER = "async function toggleTrading() {"
END_MARKER = "\nfunction switchTip("

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the master switch"
)

PAPER = {
    "env": "paper", "live": False, "ok": True, "broker": "alpaca",
    "base_url": "https://paper-api.alpaca.markets",
}
LIVE = {
    "env": "live", "live": True, "ok": True, "broker": "alpaca",
    "base_url": "https://api.alpaca.markets",
}


def extract_toggle_block() -> str:
    """The real toggle handler, lifted verbatim out of `app.js`."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    return src[start:src.index(END_MARKER, start)]


def switch_module() -> str:
    """The shared switch, verbatim: the module both pages run."""
    return SWITCH_JS.read_text(encoding="utf-8") + "\n"


HARNESS = r"""
// ---- fakes: record every effect the handler can have ----------------------
const log = { api: [], toasts: [], dialogs: [], loads: 0 };
let confirmAnswer = true;

function flashToast(text) { log.toasts.push(text); }
function escapeHtml(s) { return String(s == null ? "" : s); }
function renderStrategyBar() {}
function loadTrading() { log.loads += 1; return Promise.resolve(); }
function api(path, opts) {
  log.api.push({ path: path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return Promise.resolve({ ok: true, message: "done" });
}
function confirmDialog(opts) {
  log.dialogs.push({ title: opts.title, confirmText: opts.confirmText, messageHtml: opts.messageHtml });
  return Promise.resolve(confirmAnswer);
}

const state = { tradingState: {}, executionStatus: PAPER, rulesPayload: null };
function reset() { log.api = []; log.dialogs = []; log.loads = 0; }

(async function () {
  const out = {};

  // 1. PAPER, turning ON: must ask, and the wording must not claim real money.
  state.tradingState = { on: false };
  state.executionStatus = PAPER;
  reset();
  confirmAnswer = true;
  await toggleTrading();
  out.paperOn = { dialogs: log.dialogs, api: log.api, loads: log.loads };

  // 2. Declining the paper prompt writes nothing.
  reset();
  confirmAnswer = false;
  await toggleTrading();
  out.paperOnDeclined = { dialogs: log.dialogs.length, api: log.api.length, loads: log.loads };

  // 3. LIVE, turning ON: asks too, and says what is at stake.
  state.executionStatus = LIVE;
  reset();
  confirmAnswer = true;
  await toggleTrading();
  out.liveOn = { dialogs: log.dialogs, api: log.api, loads: log.loads };

  // 4. Turning OFF never asks — stopping is always one click.
  state.tradingState = { on: true };
  reset();
  await toggleTrading();
  out.turningOff = { dialogs: log.dialogs.length, api: log.api, loads: log.loads };

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def toggle_results(tmp_path_factory) -> dict:
    """Run the real toggle handler under node and return what it did."""
    script = tmp_path_factory.mktemp("toggle") / "toggle.js"
    script.write_text(
        "const PAPER = " + json.dumps(PAPER) + ";\nconst LIVE = " + json.dumps(LIVE) + ";\n"
        + switch_module() + extract_toggle_block() + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "master switch harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_starting_on_the_paper_account_asks_too(toggle_results):
    """Regression: the prompt only appeared for the live account."""
    got = toggle_results["paperOn"]
    assert len(got["dialogs"]) == 1
    dialog = got["dialogs"][0]
    assert dialog["title"] == "Start trading on the paper account?"
    assert "simulated" in dialog["messageHtml"]
    assert "REAL money" not in dialog["messageHtml"], "paper must not be dressed up as live"
    # ...and only after the confirmation does it actually start.
    assert got["api"] == [{"path": "/api/v1/trading/on", "body": {"confirm_live": False}}]
    assert got["loads"] == 1, "the UI must re-read the state it just changed"


def test_declining_the_paper_prompt_writes_nothing(toggle_results):
    got = toggle_results["paperOnDeclined"]
    assert got["dialogs"] == 1
    assert got["api"] == 0 and got["loads"] == 0


def test_starting_on_the_live_account_says_what_is_at_stake(toggle_results):
    got = toggle_results["liveOn"]
    assert len(got["dialogs"]) == 1
    dialog = got["dialogs"][0]
    assert dialog["title"] == "Start trading with REAL money?"
    assert dialog["confirmText"] == "Start live trading"
    # The live flag is what makes the SERVER require the acknowledgement too.
    assert got["api"] == [{"path": "/api/v1/trading/on", "body": {"confirm_live": True}}]


def test_stopping_never_asks(toggle_results):
    got = toggle_results["turningOff"]
    assert got["dialogs"] == 0
    # No acknowledgement either: stopping is not something to consent to.
    assert got["api"] == [{"path": "/api/v1/trading/off", "body": {}}]


# ---------------------------------------------------------------------------
# what the switch says BEFORE the click
# ---------------------------------------------------------------------------
TOOLTIP_HARNESS = r"""
function $(id) { return null; }   // switchTip is a pure string: it touches no element

const failed = {
  env: "paper", ok: true, message: "ok", broker: "alpaca",
};
const codes = {};

// A check that FAILED must be quoted: "not verified yet" would send the operator
// to press Validate when the fix is to replace the keys.
codes.failed = switchTip({
  execution: failed, trading: { on: false }, strategy: "s1",
  verification: { has_verdict: true, verified: false, message: "Alpaca rejected these credentials (401)" },
});

// Never checked: no verdict, no reason to quote — say what to do.
codes.unchecked = switchTip({
  execution: failed, trading: { on: false }, strategy: "s1",
  verification: { has_verdict: false, verified: false, message: "" },
});

// Verified: an invitation, not a warning.
codes.verified = switchTip({
  execution: failed, trading: { on: false }, strategy: "s1",
  verification: { has_verdict: true, verified: true, message: "Credentials accepted" },
});

// A broken execution target outranks everything: the keys are not even in play.
codes.no_target = switchTip({
  execution: { env: "live", ok: false, message: "LIVE API key/secret are missing" },
  trading: { on: false }, strategy: "s1",
  verification: { has_verdict: false, verified: false, message: "" },
});

// Armed: the hint names the action that is left, which is stopping.
codes.armed = switchTip({
  execution: { env: "paper", ok: true, broker: "alpaca" },
  trading: { on: true, env: "paper", since: "2026-09-15T09:12:31+00:00" }, strategy: "s1",
  verification: { has_verdict: true, verified: true, message: "Credentials accepted" },
});

process.stdout.write(JSON.stringify(codes));
"""


@pytest.fixture(scope="module")
def tooltips(tmp_path_factory) -> dict:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function switchTip(")
    block = src[start:src.index("\nfunction renderTradingPanel(", start)]
    script = tmp_path_factory.mktemp("tooltip") / "tooltip.js"
    script.write_text(block + TOOLTIP_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "tooltip harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_a_failed_check_is_quoted_rather_than_called_unverified(tooltips):
    assert "401" in tooltips["failed"]
    assert "not been verified yet" not in tooltips["failed"]


def test_an_unchecked_pair_is_checked_by_the_switch_itself(tooltips):
    """There is nothing to press first: validating is no longer a prerequisite, the
    switch does it. So the hint says when, not where."""
    assert "checked when you switch it on" in tooltips["unchecked"]
    assert "cannot start" not in tooltips["unchecked"], "nothing has failed yet"


def test_a_verified_pair_is_only_an_invitation(tooltips):
    assert "cannot start" not in tooltips["verified"]


def test_a_broken_target_outranks_the_credential_verdict(tooltips):
    assert "LIVE API key/secret are missing" in tooltips["no_target"]


def test_the_hint_names_stopping_once_it_is_armed(tooltips):
    """The box reads "on"; the hint has to say what pressing it does — and it must not still be
    inviting an arming that already happened."""
    assert "click to stop" in tooltips["armed"]
    assert "Start sending orders" not in tooltips["armed"]


# ---------------------------------------------------------------------------
# the second page: the trading log's half of the same switch
# ---------------------------------------------------------------------------
LOG_START_MARKER = "  async function toggleTrading() {"
LOG_END_MARKER = "\n  }\n"


def extract_log_toggle() -> str:
    """The log page's handler, lifted verbatim out of `log.js`."""
    src = LOG_JS.read_text(encoding="utf-8")
    start = src.index(LOG_START_MARKER)
    return src[start:src.index(LOG_END_MARKER, start) + len(LOG_END_MARKER)]


# The module is stubbed HERE on purpose, and the real one is not: what this harness is about is
# the PAGE's half — that it hands its own plumbing to the shared decision, and re-reads the state
# the click changed. The module itself is exercised for real in the fixture above.
LOG_HARNESS = r"""
const log = { calls: [], status: 0, toasts: [], wrote: true };

function flashToast(text, kind) { log.toasts.push({ text: text, kind: kind }); }
function api() {}
function confirmDialog() {}
function loadStatus() { log.status += 1; return Promise.resolve(); }

// After a write the page reads the status once more a few seconds later, to pick up the boundary
// the first tick commits to. Captured, not fired: this harness is about what the CLICK does.
const FIRST_TICK_MS = 5000;
function setTimeout() { return 0; }

const TraiderSwitch = {
  flip(deps) {
    log.calls.push({
      trading: deps.trading,
      execution: deps.execution,
      sameApi: deps.api === api,
      sameDialog: deps.confirmDialog === confirmDialog,
      sameToast: deps.flashToast === flashToast,
    });
    return Promise.resolve({ wrote: log.wrote });
  },
};

const state = { trading: null };

(async function () {
  const out = {};

  // 1. A click with a state that WAS read.
  state.trading = { trading: { on: false, env: "paper" }, execution: { env: "paper", live: false } };
  log.wrote = true;
  await toggleTrading();
  out.clicked = { calls: log.calls, status: log.status };

  // 2. Declining the dialog writes nothing, so there is nothing to re-read.
  log.calls = []; log.status = 0; log.wrote = false;
  await toggleTrading();
  out.declined = { calls: log.calls.length, status: log.status };

  // 3. Nothing was read at all: the switch must not be used on a guess.
  log.calls = []; log.status = 0; log.toasts = []; log.wrote = true;
  state.trading = null;
  await toggleTrading();
  out.unreadable = { calls: log.calls.length, status: log.status, toasts: log.toasts };

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def log_toggle(tmp_path_factory) -> dict:
    """Run the log page's handler under node and return what it did."""
    script = tmp_path_factory.mktemp("logswitch") / "logswitch.js"
    script.write_text(extract_log_toggle() + LOG_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "log switch harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_log_page_hands_its_own_plumbing_to_the_shared_switch(log_toggle):
    """The page re-decides nothing: it passes the dialog and the toast it has, and the state it
    last read, to the same module the dashboard uses."""
    assert log_toggle["clicked"]["calls"] == [{
        "trading": {"on": False, "env": "paper"},
        "execution": {"env": "paper", "live": False},
        "sameApi": True,
        "sameDialog": True,
        "sameToast": True,
    }]


def test_the_log_page_re_reads_the_status_the_click_changed(log_toggle):
    """Arming starts the loop, so the chip has to be re-read as well as the switch."""
    assert log_toggle["clicked"]["status"] == 1
    assert log_toggle["declined"]["status"] == 0, "nothing was written, so nothing changed"


def test_a_state_that_could_not_be_read_cannot_be_switched(log_toggle):
    """The switch's position would be a guess, and this one starts real trading."""
    got = log_toggle["unreadable"]
    assert got["calls"] == 0
    assert got["status"] == 0
    assert got["toasts"] and "could not be read" in got["toasts"][0]["text"]


def test_the_log_page_exposes_the_handler_its_button_calls():
    """The button is in the markup, so the handler has to be reachable from there."""
    assert "window.toggleTrading = toggleTrading;" in LOG_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# arming starts a process, and a process takes a moment
# ---------------------------------------------------------------------------
SETTLE_HARNESS = r"""
// Timers fire at once: this is about how many reads were made, not how long it took.
global.setTimeout = function (cb) { Promise.resolve().then(cb); return 0; };

const log = { loopReads: 0, posts: 0 };
let states = ["running"];
let startReply = { running: true };

function flashToast() {}
function confirmDialog() { return Promise.resolve(true); }

function api(path) {
  if (path === "/api/v1/loop") {
    log.loopReads += 1;
    return Promise.resolve({ state: states.length > 1 ? states.shift() : states[0] });
  }
  log.posts += 1;
  return Promise.resolve({ ok: true, message: "done", loop: startReply });
}

function flip(trading) {
  return TraiderSwitch.flip({
    api: api, confirmDialog: confirmDialog, flashToast: flashToast,
    trading: trading, execution: PAPER,
  });
}

(async function () {
  const out = {};

  // 1. The child takes two reads to claim the lease — arming must not alarm in the meantime.
  states = ["stopped", "stopped", "running"];
  log.loopReads = 0;
  const started = await flip({ on: false });
  out.starting = { loopReads: log.loopReads, wrote: started.wrote, ok: started.ok };

  // 2. A loop that never comes up: bounded, and the caller is still told the write landed.
  states = ["stopped"];
  log.loopReads = 0;
  const never = await flip({ on: false });
  out.neverUp = { loopReads: log.loopReads, wrote: never.wrote };

  // 3. A start that FAILED is reported as failed, so there is nothing to wait for.
  states = ["stopped"];
  startReply = { running: false };
  log.loopReads = 0;
  await flip({ on: false });
  out.failedStart = { loopReads: log.loopReads };

  // 4. Stopping reads no loop at all.
  states = ["running"];
  startReply = { running: true };
  log.loopReads = 0;
  await flip({ on: true });
  out.stopping = { loopReads: log.loopReads };

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def settling(tmp_path_factory) -> dict:
    """Drive the real shared switch with a loop that takes a moment to appear."""
    script = tmp_path_factory.mktemp("settle") / "settle.js"
    script.write_text(
        "const PAPER = " + json.dumps(PAPER) + ";\n" + switch_module() + SETTLE_HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "settle harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_arming_waits_for_the_loop_it_started(settling):
    """Arming starts a PROCESS, and the POST answers as soon as the child exists — not when it
    holds the lease. Taking it costs a second of interpreter start-up, and a loop read taken in
    that window says "stopped", which is how arming told the operator that no loop was running
    about a loop it had just started."""
    got = settling["starting"]
    assert got["loopReads"] == 3, "read until it stops saying nothing is running"
    assert got["wrote"] is True and got["ok"] is True


def test_a_loop_that_never_comes_up_is_waited_out_then_reported(settling):
    """Bounded: after the attempts run out "not running" is the truth, and the warning that
    follows is then about something real."""
    assert settling["neverUp"]["loopReads"] == 12
    assert settling["neverUp"]["wrote"] is True


def test_a_start_that_failed_is_not_waited_for(settling):
    """The server already said the process could not be started: there is nothing to wait for,
    and the caller has to see it at once."""
    assert settling["failedStart"]["loopReads"] == 0


def test_stopping_reads_no_loop_at_all(settling):
    assert settling["stopping"]["loopReads"] == 0
