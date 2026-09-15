"""Behavioural tests for the master switch's confirmation.

Turning trading ON always asks first — on the paper account as well as the live
one. Two things matter and neither is visible in a string assertion:

* it asks in BOTH environments (the paper prompt used to be skipped entirely), and
  the wording differs, because "this spends real money" and "this bot starts acting
  on the next signal" are different decisions;
* declining really writes nothing, and turning trading OFF never asks at all —
  stopping must always take one click.

The real `toggleTrading` is lifted out of `app.js` and run under node with fakes
that record every effect, so the assertions are about the calls that were made.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "async function toggleTrading() {"
END_MARKER = "\nfunction renderTradingControls("

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
        + extract_toggle_block() + HARNESS,
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
