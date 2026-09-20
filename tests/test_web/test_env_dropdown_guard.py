"""Behavioural tests for the paper/live dropdown's user-gesture guard.

The header dropdown writes the active strategy's `EXECUTION_ENV` — the field that
decides which account receives REAL orders. It hangs off a plain `change` event,
and a browser will happily fire `change` on its own: restoring a form's value from
the bfcache, from back/forward history, or from a crash-recovery session restart.
A restore that happened to restore `live` used to be persisted as if the operator
had chosen it, because a restored value and a deliberate selection are
indistinguishable at the event level.

So the handler requires a real gesture ON THE SELECT before it will write, and
otherwise puts the display back to what the server said.

String assertions cannot show that: what matters is whether `api()` is called. So
these tests lift the real block out of `app.js`, run it under node with fakes that
record every effect, and assert the calls — the same technique the crosshair tests
use.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "let _envGesture = false;"
END_MARKER = "\nasync function toggleTrading()"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the dropdown guard"
)

PAYLOAD = {
    "execution": {"env": "paper", "live": False, "ok": True, "broker": "alpaca"},
    "env_options": [],
}


def extract_guard_block() -> str:
    """The real gesture guard + handler, lifted verbatim out of `app.js`."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    end = src.index(END_MARKER, start)
    return src[start:end]


HARNESS = r"""
// ---- fakes: record every effect the handler can have ----------------------
const log = { api: [], toasts: [], renders: [], dialogs: [], loads: 0, refreshes: [] };
let confirmAnswer = true;

// The guard arms a safety timeout. These tests are about the flags, not the clock,
// and a pending timer would keep node alive for 15 seconds after the last case.
const scheduled = [];
function setTimeout(cb, ms) { scheduled.push({ cb: cb, ms: ms }); return scheduled.length; }
function clearTimeout() {}

function makeEl(id) {
  const listeners = {};
  const registered = [];
  return {
    id: id,
    value: "paper",
    className: "",
    title: "",
    options: [],
    registered: registered,
    addEventListener: function (type, cb, opts) {
      registered.push(type);
      (listeners[type] = listeners[type] || []).push({ cb: cb, once: !!(opts && opts.once) });
    },
    fire: function (type) {
      // Fire every listener, exactly as the DOM does, and drop the ones registered
      // with {once: true} afterwards. A fake that only ever followed the `once` path
      // would stop calling the listener the moment one lost that option — which is
      // how a broken guard can look tested.
      const entries = listeners[type] || [];
      listeners[type] = entries.filter(function (entry) { return !entry.once; });
      entries.forEach(function (entry) { entry.cb({}); });
    },
  };
}

const sel = makeEl("exec-env");
// document.addEventListener must NOT be used: a click anywhere on the page is not
// a decision about which account gets the orders.
const docAdds = [];
const document = {
  activeElement: null,
  addEventListener: function (type) { docAdds.push(type); },
};

function $(id) { return id === "exec-env" ? sel : null; }

const state = { tradingPayload: PAYLOAD, executionStatus: PAYLOAD.execution };

function renderEnvSelect(d, force) {
  log.renders.push({ env: (d.execution || {}).env, live: !!force });
  if (force) sel.value = (d.execution || {}).env || "paper";
}
function api(path, opts) {
  log.api.push({ path: path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return Promise.resolve({ ok: true, message: "Environment updated" });
}
function flashToast(text) { log.toasts.push(text); }
function confirmDialog() { log.dialogs.push(1); return Promise.resolve(confirmAnswer); }
function loadTrading() { log.loads += 1; return Promise.resolve(); }
// The immediate broker re-read. Deliberately NOT counted as a `load`: that counter is what the
// cases below assert against, and folding two effects into it would make them indistinguishable.
// It is a cumulative list, so its length at the end counts every switch in the harness.
function refreshLiveNow() { log.refreshes.push(1); return Promise.resolve(); }

(async function () {
  const out = {};

  // 1. A RESTORED value (no gesture on the select) must write nothing.
  watchEnvSelect();
  _envGesture = false;              // a fresh page load
  sel.value = "live";               // what the browser restored
  await onEnvChange();
  out.noGesture = {
    api: log.api.length,
    restoredTo: sel.value,
    force: log.renders.length ? log.renders[log.renders.length - 1].live : null,
    loads: log.loads,
  };

  // 2. A real selection does write it.
  log.api = []; log.renders = []; log.dialogs = []; log.loads = 0;
  _envGesture = false;
  watchEnvSelect();
  sel.fire("pointerdown");
  sel.value = "live";
  confirmAnswer = true;
  await onEnvChange();
  out.pointerGesture = {
    api: log.api,
    dialogs: log.dialogs.length,
    loads: log.loads,
  };

  // 3. Declining the confirmation writes nothing and puts the value back.
  log.api = []; log.renders = []; log.dialogs = []; log.loads = 0;
  sel.value = "live";
  confirmAnswer = false;
  await onEnvChange();
  out.declined = { api: log.api.length, value: sel.value, loads: log.loads };

  // 4. The keyboard counts as a gesture too (arrow keys + Enter).
  log.api = []; log.renders = []; log.dialogs = []; log.loads = 0;
  _envGesture = false;
  sel.value = "paper";
  watchEnvSelect();
  sel.fire("keydown");
  sel.value = "live";
  confirmAnswer = true;
  await onEnvChange();
  out.keyGesture = { api: log.api.length, loads: log.loads };

  // 5. Selecting the value that is already stored is a no-op.
  log.api = []; log.renders = []; log.dialogs = []; log.loads = 0;
  sel.value = "paper";              // the server's value
  await onEnvChange();
  out.unchanged = { api: log.api.length, loads: log.loads };

  // 6. Once a change has been dealt with, the guard is released — so a restore that
  //    arrives later is refused again, and a second deliberate switch still works.
  //    (The first version latched `true` forever and armed its listeners once.)
  log.api = []; log.renders = []; log.dialogs = []; log.loads = 0;
  sel.value = "paper";              // a restore of the stored value
  await onEnvChange();
  out.afterHandled = { api: log.api.length, restoredTo: sel.value };

  log.api = []; log.dialogs = []; log.loads = 0;
  sel.fire("pointerdown");
  sel.value = "live";
  confirmAnswer = true;
  await onEnvChange();
  out.secondSelection = { api: log.api.length, loads: log.loads };

  // 7. The keyboard works for a SECOND switch too: with `{once: true}` listeners the
  //    guard was already disarmed by then, and the choice was silently thrown away.
  log.api = []; log.dialogs = []; log.loads = 0;
  sel.value = "paper";
  sel.fire("keydown");
  sel.value = "live";
  await onEnvChange();
  out.secondKeySelection = { api: log.api.length };

  // 8. Nothing is registered on the document: only the select can arm the guard.
  out.documentListeners = docAdds.slice();
  out.selectRegistrations = sel.registered.slice();
  // One immediate re-read per WRITTEN switch — cases 2, 4, 6b and 7, and none for the restore,
  // the decline, the no-op or the second restore.
  out.refreshes = log.refreshes.length;

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def guard_results(tmp_path_factory) -> dict:
    """Run the real guard block under node and return what it did."""
    script = tmp_path_factory.mktemp("envguard") / "guard.js"
    script.write_text(
        "const PAYLOAD = " + json.dumps(PAYLOAD) + ";\n" + extract_guard_block() + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "env dropdown guard harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_a_restored_value_is_never_written(guard_results):
    """The regression: a browser-restored `live` used to be persisted silently."""
    got = guard_results["noGesture"]
    assert got["api"] == 0, "a restored value must not reach the server"
    assert got["loads"] == 0
    # ...and the display goes back to what the server actually said.
    assert got["restoredTo"] == "paper"
    assert got["force"] is True, "the restore must overwrite the select's value"


def test_a_real_pointer_selection_is_written(guard_results):
    got = guard_results["pointerGesture"]
    assert [c["path"] for c in got["api"]] == ["/api/v1/execution/env"]
    assert got["api"][0]["body"] == {"env": "live"}
    # Live still costs a confirmation, and the state is re-read afterwards.
    assert got["dialogs"] == 1
    assert got["loads"] == 1


def test_declining_the_confirmation_writes_nothing(guard_results):
    got = guard_results["declined"]
    assert got["api"] == 0
    assert got["loads"] == 0
    assert got["value"] == "paper", "the select must snap back to the stored value"


def test_a_keyboard_selection_is_written(guard_results):
    got = guard_results["keyGesture"]
    assert got["api"] == 1 and got["loads"] == 1


def test_reselecting_the_stored_value_is_a_noop(guard_results):
    got = guard_results["unchanged"]
    assert got["api"] == 0 and got["loads"] == 0


def test_only_the_dropdown_itself_can_arm_the_guard(guard_results):
    """A click elsewhere on the page is not a decision about the account."""
    assert guard_results["documentListeners"] == []
    # pointerdown/keydown arm it; blur releases it when the menu closes with no choice.
    assert set(guard_results["selectRegistrations"]) == {"pointerdown", "keydown", "blur"}


def test_a_switch_rereads_the_account_at_once(guard_results):
    """Asked for: switching mode must not wait for the poll.

    Only the MODE box comes from the switch's own read; the account boxes beside it (equity, the
    day, cash, buying power, status) come from the broker half of the live poll, which runs on a
    minute cadence. So a switch relabelled the panel and left every figure in it belonging to the
    account the operator had just left, until the poll came round.

    One immediate re-read per written switch (cases 2, 4, 6b and 7 above) — and none for a
    restored value, a declined confirmation or a no-op selection, because none of those changed
    which account the panel is about.
    """
    assert guard_results["refreshes"] == 4
    # The switch's own state is still re-read on every one of them...
    assert guard_results["pointerGesture"]["loads"] == 1
    # ...and a decline never reaches the broker at all.
    assert guard_results["declined"]["loads"] == 0
    assert guard_results["unchanged"]["loads"] == 0


def test_the_guard_is_released_after_each_change(guard_results):
    """It must not become a latch: a restore arriving after a handled change is
    still refused, and the next deliberate switch still gets through."""
    after = guard_results["afterHandled"]
    assert after["api"] == 0, "a restore after a switch must not be written either"
    assert after["restoredTo"] == "paper"

    assert guard_results["secondSelection"]["api"] == 1
    assert guard_results["secondSelection"]["loads"] == 1


def test_a_second_keyboard_switch_also_works(guard_results):
    """Regression: the listeners were registered with `{once: true}`, so after the
    first interaction nothing armed the guard and every later choice was discarded as
    if a browser had restored it."""
    assert guard_results["secondKeySelection"]["api"] == 1
