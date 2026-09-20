"""Behavioural tests for the one path that changes which account orders go to.

`EXECUTION_ENV` decides which account receives REAL orders, so the write is not a plain POST: a
switch to LIVE costs a confirmation first, and the two reads that follow it are what turn the
Trading panel onto the account it now points at instead of leaving the figures of the one it just
left on screen.

None of that is visible in a string assertion: what matters is whether `api()` was called, with
what body, and what was re-read afterwards. So these tests lift the real block out of `app.js` and
run it under node with fakes that record every effect, and assert the calls — the same technique
the crosshair tests use.

The Mode box in the Trading panel is the ONLY way in. The header's paper/live dropdown, and the
gesture guard it needed (a browser can restore a `<select>`'s value and fire `change` with no user
involved, so a restore must never be taken for a choice), went with it: a click on a button cannot
be restored by anything, which is why there is no guard left to test here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "/* The one way into the mode: a click on the Mode box."
END_MARKER = "\nasync function toggleTrading()"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the mode switch"
)


def extract_switch_block() -> str:
    """The real handler and the write it hides behind, lifted verbatim out of `app.js`."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    return src[start:src.index(END_MARKER, start)]


HARNESS = r"""
// ---- fakes: record every effect the write can have ------------------------
const log = { api: [], toasts: [], dialogs: [], loads: 0, refreshes: [] };
let confirmAnswer = true;
let apiAnswer = null;   // null -> the ordinary success answer
let apiThrows = false;

const state = { executionStatus: { env: "paper" }, tradingPayload: {} };
// The mode the box is showing, read exactly the way the panel's own tile reads it.
function inPlayEnv() { return String((state.executionStatus || {}).env || "").toLowerCase(); }

function confirmDialog() { log.dialogs.push(1); return Promise.resolve(confirmAnswer); }
function api(path, opts) {
  log.api.push({ path: path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (apiThrows) return Promise.reject(new Error("network down"));
  return Promise.resolve(apiAnswer || { ok: true, message: "Environment updated" });
}
function flashToast(text) { log.toasts.push(text); }
function loadTrading() { log.loads += 1; return Promise.resolve(); }
// The immediate broker re-read. Deliberately NOT counted as a `load`: that counter is what the
// cases below assert against, and folding two effects into it would make them indistinguishable.
function refreshLiveNow() { log.refreshes.push(1); return Promise.resolve(); }

(async function () {
  const out = {};
  function reset() {
    log.api = []; log.toasts = []; log.dialogs = []; log.loads = 0; log.refreshes = [];
  }

  // 1. Pointed at paper, a click moves orders to LIVE — after asking.
  reset();
  state.executionStatus = { env: "paper" };
  await onModeBoxClick();
  out.toLive = {
    api: log.api, dialogs: log.dialogs.length, loads: log.loads,
    refreshes: log.refreshes.length, toasts: log.toasts.slice(),
  };

  // 2. Pointed at live, a click moves them back to paper — and that asks nothing: going the safe
  //    way is not something to consent to.
  reset();
  state.executionStatus = { env: "live" };
  await onModeBoxClick();
  out.toPaper = {
    api: log.api, dialogs: log.dialogs.length, loads: log.loads,
    refreshes: log.refreshes.length,
  };

  // 3. Declining the confirmation writes nothing at all.
  reset();
  state.executionStatus = { env: "paper" };
  confirmAnswer = false;
  await onModeBoxClick();
  out.declined = { api: log.api.length, loads: log.loads, refreshes: log.refreshes.length };
  confirmAnswer = true;

  // 4. A refusal from the server is not a change: nothing is re-read, and the reason is said.
  reset();
  apiAnswer = { ok: false, message: "Trading is ON — turn it off to change the environment" };
  await onModeBoxClick();
  out.refused = {
    api: log.api.length, loads: log.loads, refreshes: log.refreshes.length,
    toasts: log.toasts.slice(),
  };
  apiAnswer = null;

  // 5. A request that never landed is the same case, and must not leave the panel claiming a mode
  //    the server does not have.
  reset();
  apiThrows = true;
  await onModeBoxClick();
  out.failed = {
    api: log.api.length, loads: log.loads, refreshes: log.refreshes.length,
    toasts: log.toasts.slice(),
  };
  apiThrows = false;

  // 6. Nothing read yet: the box shows "—", and the other account it offers is the one the header
  //    defaulted to — live, with its confirmation.
  reset();
  state.executionStatus = {};
  await onModeBoxClick();
  out.unread = { body: log.api.length ? log.api[0].body : null, dialogs: log.dialogs.length };

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def mode_results(tmp_path_factory) -> dict:
    """Run the real mode-switch block under node and return what it did."""
    script = tmp_path_factory.mktemp("modeswitch") / "switch.js"
    script.write_text(extract_switch_block() + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "mode switch harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_box_writes_the_other_account(mode_results):
    got = mode_results["toLive"]

    assert [c["path"] for c in got["api"]] == ["/api/v1/execution/env"]
    assert got["api"][0]["body"] == {"env": "live"}
    # Live still costs a confirmation...
    assert got["dialogs"] == 1
    assert got["loads"] == 1, "the switch's own state is re-read"
    assert got["refreshes"] == 1, "and the broker, so the figures follow the account"


def test_going_back_to_paper_asks_nothing(mode_results):
    got = mode_results["toPaper"]

    assert got["api"][0]["body"] == {"env": "paper"}
    assert got["dialogs"] == 0, "the safe direction is not something to consent to"
    assert got["loads"] == 1 and got["refreshes"] == 1


def test_declining_the_confirmation_writes_nothing(mode_results):
    got = mode_results["declined"]

    assert got["api"] == 0
    assert got["loads"] == 0 and got["refreshes"] == 0


def test_a_refusal_leaves_the_panel_on_the_account_it_had(mode_results):
    """A refused switch is not a completed one.

    The server refuses this while trading is ON (and while a position is open), and re-reading on
    a refusal would relabel the panel with a mode the server never took — which is the exact
    confusion the Mode box exists to prevent.
    """
    got = mode_results["refused"]

    assert got["api"] == 1, "the write was attempted"
    assert got["loads"] == 0 and got["refreshes"] == 0, "nothing to re-read: nothing changed"
    assert any("Trading is ON" in t for t in got["toasts"]), "and the reason is said out loud"


def test_a_write_that_never_landed_says_so(mode_results):
    got = mode_results["failed"]

    assert got["api"] == 1
    assert got["loads"] == 0 and got["refreshes"] == 0
    assert any("network down" in t for t in got["toasts"])


def test_an_unread_mode_still_offers_the_other_account(mode_results):
    """The box reads "—" for a mode nobody has read yet, and the header's old default was the paper
    account — so the other one is live, and it asks before it goes."""
    got = mode_results["unread"]

    assert got["body"] == {"env": "live"}
    assert got["dialogs"] == 1


def test_the_mode_box_is_the_only_way_in():
    """The header's dropdown is gone, so this is the whole surface that can change the mode.

    Asserted by counting callers rather than by reading: the definition plus exactly one call.
    A second caller would be a second route into the most dangerous write in the app, and the
    gesture guard that made the dropdown safe is gone with the dropdown.
    """
    app = APP_JS.read_text(encoding="utf-8")

    assert "onEnvChange" not in app, "the dropdown's handler is gone"
    assert "exec-env" not in app, "and so is everything that served it"
    assert app.count("writeExecutionEnv(") == 2, "one definition and one caller"
