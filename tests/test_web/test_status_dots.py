"""Behavioural tests for the header pills' status dots.

Each pill ends with a dot that repeats what the words say, in colour:

* blue = the calm state (paper account, trading off)
* red  = the state that spends money or is live (live account, trading on), blinking

The dot is part of the TEXT rather than a styled element, and that is not a
shortcut: the account control is a native `<select>`, whose options can only hold
text, and a select always sizes itself to its WIDEST option — measured, `width:
min-content/fit-content/max-content` make no difference — so a positioned element
could never land at the end of the selected label. A glyph behaves identically in
both pills, which is what keeps them one style.

The real block is lifted out of `app.js` and run under node, with the timer driven
by hand, so the assertions are about glyphs and timer lifetime rather than timing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "const DOT_CALM"
END_MARKER = "\nfunction renderTradingControls("

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the status dots"
)

CALM, ALERT, ALERT_OFF = "\U0001f535", "\U0001f534", "\u26ab"

PAPER_KEYS_SET = {
    "env": "paper", "live": False, "ok": True, "broker": "alpaca",
    "base_url": "https://paper-api.alpaca.markets",
}
LIVE_KEYS_SET = {
    "env": "live", "live": True, "ok": True, "broker": "alpaca",
    "base_url": "https://api.alpaca.markets",
}
OPTIONS = [
    {"value": "paper", "label": "Paper — simulated, no real money"},
    {"value": "live", "label": "LIVE — REAL ORDERS"},
]


def extract_dot_block() -> str:
    """The real status-dot block, lifted verbatim out of `app.js`."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    return src[start:src.index(END_MARKER, start)]


HARNESS = r"""
// ---- fakes: the two pills, the clock, and the motion preference -------------
function makeEl(id) {
  return { id: id, textContent: "", className: "", title: "", disabled: false,
           options: [], parentElement: null };
}
const btn = makeEl("trading-toggle");
const sel = makeEl("exec-env");
function $(id) { return id === "exec-env" ? sel : id === "trading-toggle" ? btn : null; }

function makeOption(value) {
  return { value: value, textContent: "" };
}
function syncOptions(d) {
  const wanted = (d.env_options || []).map((o) => o.value);
  const have = sel.options.map((o) => o.value);
  if (wanted.join() !== have.join()) {
    sel.options = wanted.map(makeOption);
    sel.options.length = wanted.length;
    sel.options.forEach(function (o, i) { o.value = wanted[i]; });
  }
}
function optionText(value) {
  const o = sel.options.find(function (x) { return x.value === value; });
  return o ? o.textContent : null;
}

// The timer is captured, never waited on: the test fires it by hand. Only ONE may
// be active at a time, and clearing it must really release it — otherwise
// "the clock stops when nothing blinks" cannot be asserted.
const timers = [];
let activeTimer = null;
let cleared = 0;
global.setInterval = function (cb, ms) { activeTimer = { cb: cb, ms: ms }; timers.push(activeTimer); return activeTimer; };
global.clearInterval = function (t) { if (activeTimer === t) activeTimer = null; cleared += 1; };
let reducedMotion = false;
global.window = global;
global.window.matchMedia = function (q) { return { matches: reducedMotion && q.indexOf("reduce") > -1 }; };

const state = { tradingPayload: null };
function setPayload(env, live, ok, on) {
  const p = {
    env_options: OPTIONS,
    execution: env === "live" ? LIVE_KEYS_SET : PAPER_KEYS_SET,
    trading: { on: on, since: on ? "2026-09-15T09:12:31+00:00" : null, env: env },
  };
  p.execution = { env: env, live: live, ok: ok, broker: "alpaca", base_url: "x", message: "keys missing" };
  state.tradingPayload = p;
  return p;
}

function render(p) { syncOptions(p); renderStatusDots(p); }
function tick() { timers[timers.length - 1].cb(); }

const out = {};
function snap(name) {
  out[name] = {
    button: btn.textContent,
    paper: optionText("paper"),
    live: optionText("live"),
    timerActive: !!activeTimer,
    timerMs: activeTimer ? activeTimer.ms : null,
  };
}

// 1. paper + trading off: blue on both.
render(setPayload("paper", false, true, false));
snap("paperOff");

// 2. live + trading off: the account dot blinks, the switch stays blue.
render(setPayload("live", true, true, false));
snap("liveOffBefore");
tick();
snap("liveOffAfterTick");

// 3. live + trading on: both dots blink, in step.
render(setPayload("live", true, true, true));
snap("liveOnBefore");
tick();
snap("liveOnAfterTick");

// 4. back to paper + off: calm again, and the timer is released.
render(setPayload("paper", false, true, false));
snap("backToCalm");
out.clearedWhenCalm = cleared;
out.timerStopped = !activeTimer;

// 5. reduced motion: the alert colour still shows, but nothing flashes.
reducedMotion = true;
render(setPayload("live", true, true, true));
snap("reducedMotionLiveOn");
// Even a stale tick (the phase flipping) must not darken it.
tick();
snap("reducedMotionAfterTick");
reducedMotion = false;

// 6. an account with no keys says so in words, next to its dot.
render(setPayload("paper", false, false, false));
snap("blockedPaper");

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def dot_results(tmp_path_factory) -> dict:
    """Run the real status-dot block under node and return what it rendered."""
    script = tmp_path_factory.mktemp("dots") / "dots.js"
    script.write_text(
        f"const OPTIONS = {json.dumps(OPTIONS)};\n"
        f"const PAPER_KEYS_SET = {json.dumps(PAPER_KEYS_SET)};\n"
        f"const LIVE_KEYS_SET = {json.dumps(LIVE_KEYS_SET)};\n"
        + extract_dot_block()
        + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "status dot harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_a_paper_account_and_idle_trading_are_both_blue(dot_results):
    got = dot_results["paperOff"]
    assert got["button"] == f"▶ Turn trading on {CALM}"
    assert got["paper"].endswith(CALM)
    # The dot is at the END of the label, after the words.
    assert got["paper"].startswith("Paper — simulated, no real money")
    # Nothing to flash, so no clock is left running.
    assert got["timerActive"] is False


def test_a_live_account_blinks_even_with_trading_off(dot_results):
    """Being pointed at the real account is itself the thing to notice."""
    before = dot_results["liveOffBefore"]
    assert before["live"] == f"LIVE — REAL ORDERS {ALERT}"
    assert before["button"].endswith(CALM), "the switch is idle: blue, not red"
    assert dot_results["liveOffAfterTick"]["live"].endswith(ALERT_OFF), "must blink"
    assert dot_results["liveOffAfterTick"]["button"].endswith(CALM), "and only the account"


def test_armed_and_live_blinks_both_dots_in_step(dot_results):
    before = dot_results["liveOnBefore"]
    # A state change starts LIT: inheriting a mid-blink phase would read as
    # "nothing happened" for up to a period.
    assert before["button"] == f"⏹ Turn trading off {ALERT}"
    assert before["live"].endswith(ALERT)
    after = dot_results["liveOnAfterTick"]
    assert after["button"].endswith(ALERT_OFF)
    assert after["live"].endswith(ALERT_OFF)
    # One timer drives both, so they can never drift out of phase.
    assert after["timerActive"] is True
    assert after["timerMs"] == 700


def test_returning_to_calm_stops_the_clock(dot_results):
    got = dot_results["backToCalm"]
    assert got["button"].endswith(CALM) and got["paper"].endswith(CALM)
    assert dot_results["clearedWhenCalm"] >= 1, "the timer must be released"
    assert dot_results["timerStopped"]


def test_reduced_motion_keeps_the_colour_but_drops_the_flashing(dot_results):
    got = dot_results["reducedMotionLiveOn"]
    assert got["button"].endswith(ALERT) and got["live"].endswith(ALERT)
    assert got["timerActive"] is False, "no clock when motion is reduced"
    # A stale tick must not be able to darken it either.
    late = dot_results["reducedMotionAfterTick"]
    assert late["button"].endswith(ALERT)
    assert late["live"].endswith(ALERT)


def test_an_account_that_cannot_trade_says_so_beside_its_dot(dot_results):
    got = dot_results["blockedPaper"]
    assert got["paper"] == f"Paper — simulated, no real money — ⚠ no keys {CALM}"
    # The other option is untouched: only the SELECTED account's problem is ours.
    assert got["live"] == f"LIVE — REAL ORDERS {ALERT}"
