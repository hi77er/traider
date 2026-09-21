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
SWITCH_JS = APP_JS.parent / "trading_switch.js"

# The write is the SHARED module's — the log page's Mode box moves the same mode — so these tests
# drive ``flipEnv`` directly. It runs the same code the dashboard's and the log page's boxes call.
START_MARKER = "async function flipEnv(deps) {"
END_MARKER = "\n  return {"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the mode switch"
)


def extract_switch_block() -> str:
    """The real write, lifted verbatim out of the shared module."""
    src = SWITCH_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    return src[start:src.index(END_MARKER, start)]


HARNESS = r"""
// ---- fakes: record every effect the write can have ------------------------
const log = { api: [], toasts: [], dialogs: [], reloads: 0 };
let confirmAnswer = true;
let apiAnswer = null;   // null -> the ordinary success answer
let apiThrows = false;

function confirmDialog() { log.dialogs.push(1); return Promise.resolve(confirmAnswer); }
function api(path, opts) {
  log.api.push({ path: path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (apiThrows) return Promise.reject(new Error("network down"));
  return Promise.resolve(apiAnswer || { ok: true, message: "Environment updated" });
}
function flashToast(text) { log.toasts.push(text); }
function reload() { log.reloads += 1; return Promise.resolve(); }

// What EITHER page's Mode box does: hand the shared write its own plumbing and the mode it is
// showing, and let it decide. In the page this is one line plus the page's own reload.
const OUT = { env: "paper" };
function flip(env) {
  return flipEnv({ api, confirmDialog, flashToast, env: env, from: OUT.env, reload: reload });
}

(async function () {
  const out = {};
  function reset() { log.api = []; log.toasts = []; log.dialogs = []; log.reloads = 0; }

  // 1. Pointed at paper, a click moves orders to LIVE — after asking.
  reset();
  OUT.env = "paper";
  const first = await flip("live");
  out.toLive = {
    changed: first, api: log.api, dialogs: log.dialogs.length, reloads: log.reloads,
    toasts: log.toasts.slice(),
  };

  // 2. Pointed at live, a click moves them back to paper — and that asks nothing: going the safe
  //    way is not something to consent to.
  reset();
  OUT.env = "live";
  const second = await flip("paper");
  out.toPaper = {
    changed: second, api: log.api, dialogs: log.dialogs.length, reloads: log.reloads,
  };

  // 3. Declining the confirmation writes nothing at all.
  reset();
  OUT.env = "paper";
  confirmAnswer = false;
  const third = await flip("live");
  out.declined = { changed: third, api: log.api.length, reloads: log.reloads };
  confirmAnswer = true;

  // 4. A refusal from the server is not a change: nothing is re-read, and the reason is said.
  reset();
  apiAnswer = { ok: false, message: "Trading is ON — turn it off to change the environment" };
  const fourth = await flip("live");
  out.refused = {
    changed: fourth, api: log.api.length, reloads: log.reloads, toasts: log.toasts.slice(),
  };
  apiAnswer = null;

  // 5. A request that never landed is the same case, and must not leave either page claiming a
  //    mode the server does not have.
  reset();
  apiThrows = true;
  const fifth = await flip("live");
  out.failed = {
    changed: fifth, api: log.api.length, reloads: log.reloads, toasts: log.toasts.slice(),
  };
  apiThrows = false;

  // 6. The mode it is already on: nothing to do, and nothing to ask.
  reset();
  OUT.env = "paper";
  const sixth = await flip("paper");
  out.unchanged = { changed: sixth, api: log.api.length, dialogs: log.dialogs.length, reloads: log.reloads };

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

    assert got["changed"] is True
    assert [c["path"] for c in got["api"]] == ["/api/v1/execution/env"]
    assert got["api"][0]["body"] == {"env": "live"}
    # Live still costs a confirmation...
    assert got["dialogs"] == 1
    assert got["reloads"] == 1, "the page that made the change re-reads what it changed"


def test_going_back_to_paper_asks_nothing(mode_results):
    got = mode_results["toPaper"]

    assert got["changed"] is True
    assert got["api"][0]["body"] == {"env": "paper"}
    assert got["dialogs"] == 0, "the safe direction is not something to consent to"
    assert got["reloads"] == 1


def test_declining_the_confirmation_writes_nothing(mode_results):
    got = mode_results["declined"]

    assert got["changed"] is False
    assert got["api"] == 0
    assert got["reloads"] == 0


def test_a_refusal_leaves_the_page_on_the_account_it_had(mode_results):
    """A refused switch is not a completed one.

    The server refuses this while trading is ON (and while a position is open), and re-reading on a
    refusal would relabel the page with a mode the server never took — which is the exact confusion
    the Mode box exists to prevent.
    """
    got = mode_results["refused"]

    assert got["changed"] is False
    assert got["api"] == 1, "the write was attempted"
    assert got["reloads"] == 0, "nothing to re-read: nothing changed"
    assert any("Trading is ON" in t for t in got["toasts"]), "and the reason is said out loud"


def test_a_write_that_never_landed_says_so(mode_results):
    got = mode_results["failed"]

    assert got["changed"] is False
    assert got["api"] == 1
    assert got["reloads"] == 0
    assert any("network down" in t for t in got["toasts"])


def test_the_mode_it_is_already_on_is_a_noop(mode_results):
    """Nothing to write and nothing to ask: a box that toggles to where it already is is a click
    that must not become a confirmation."""
    got = mode_results["unchanged"]

    assert got["changed"] is False
    assert got["api"] == 0 and got["dialogs"] == 0 and got["reloads"] == 0


def test_the_write_is_one_function_with_one_caller_per_page():
    """The most dangerous write in the app exists ONCE, on two screens.

    Asserted by counting rather than by reading: one definition, and one call per page — the
    dashboard's box and the log page's. A second route into it (an extra caller, or a page that
    re-implements the confirmation) is the failure this is here to catch.
    """
    app = APP_JS.read_text(encoding="utf-8")
    log = (APP_JS.parent / "log.js").read_text(encoding="utf-8")
    shared = SWITCH_JS.read_text(encoding="utf-8")

    assert "onEnvChange" not in app, "the header dropdown's handler is gone"
    assert "exec-env" not in app, "and so is everything that served it"
    assert shared.count("async function flipEnv(") == 1, "one definition"
    assert app.count("TraiderSwitch.flipEnv(") == 1, "the dashboard's Mode box is one caller"
    assert log.count("TraiderSwitch.flipEnv(") == 1, "and the log page's is the other"
