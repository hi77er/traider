"""Behavioural tests for the Strategy lab's switch confirmation.

Changing the active strategy is ALLOWED while positions are open, and this is the half of that
decision the server cannot make: every strategy trades the instrument its own configuration names
and nothing else, so a position is either taken over by the new strategy (the same instrument) or
left exactly as it was (a different one). Which of the two is about to happen is what the operator
is asked to confirm, so the tests are about the ASK — the two cases must not read the same —
rather than about the wording of either.

``onStrategySelect`` and the helpers it calls are lifted verbatim out of ``app.js`` and run under
node with a fake page (its ``api``, its ``confirmDialog``, its state), the way the master switch
is tested. The real ``trading_switch.js`` comes too: the sentence naming what is open is built
there, once, for both pages.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
APP_JS = STATIC / "app.js"
SWITCH_JS = STATIC / "trading_switch.js"

#: The functions under test, lifted whole. ``setStrategyMsg``/``strategyRefused`` come along
#: because the refusal path is part of the behaviour: a declined or bounced switch has to say so.
LIFTED = (
    "onStrategySelect",
    "readOpenPositions",
    "strategyTarget",
    "switchMessage",
    "strategyRefused",
    "setStrategyMsg",
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the switch confirmation"
)


def _function_source(name: str) -> str:
    """One top-level ``[async] function name(...) { ... }`` from the shipped app.js.

    The ``async`` keyword is part of the source that gets lifted: ``onStrategySelect`` awaits the
    read that decides whether to warn, and a copy that starts at ``function`` instead of ``async
    function`` is not the function under test at all — it does not even parse.
    """
    src = APP_JS.read_text(encoding="utf-8")
    marker = f"async function {name}("
    if marker not in src:
        marker = f"function {name}("
    start = src.index(marker)
    return src[start : src.index("\n}", start) + len("\n}")] + "\n"


def app_module() -> str:
    """The real switch module plus the lab's own block, verbatim."""
    return SWITCH_JS.read_text(encoding="utf-8") + "\n" + "".join(
        _function_source(name) for name in LIFTED
    )


HARNESS = r"""
// ---- fakes: a page with a picker, a dialog and no DOM ---------------------
const log = { api: [], dialogs: [], toasts: [], reloads: 0, bars: 0 };
let confirmAnswer = true;
let tradingRead = { open_count: 0, positions: [] };
let readFails = false;

const els = {
  "strategy-select": { value: "beta" },
  "strategy-msg": { textContent: "", classList: { toggle: function () {} } },
};
function $(id) { return els[id] || null; }
function escapeHtml(s) { return String(s === null || s === undefined ? "" : s); }
function flashToast(text, kind) { log.toasts.push({ text: text, kind: kind }); }
function renderStrategyBar() { log.bars += 1; }
function confirmDialog(opts) { log.dialogs.push(opts); return Promise.resolve(confirmAnswer); }
function api(path, opts) {
  log.api.push({ path: path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (path === "/api/v1/trading") {
    return readFails
      ? Promise.reject(new Error("400 Bad Request"))
      : Promise.resolve(tradingRead);
  }
  return Promise.resolve({ ok: true, message: "done" });
}
const window = { location: { reload: function () { log.reloads += 1; } } };

const RULES = {
  active: "alpha",
  strategies: {
    alpha: { config: { INSTRUMENT: "NVDA", EXECUTION_ENV: "paper" } },
    beta: { config: { INSTRUMENT: "NVDA", EXECUTION_ENV: "paper" } },
    gamma: { config: { INSTRUMENT: "GPRO", EXECUTION_ENV: "paper" } },
  },
};
const state = { rulesPayload: RULES, trading: { execution: { env: "paper" } } };

const NVDA_HELD = {
  open_count: 1,
  positions: [{
    env: "paper", count: 1, known: true, flat: false,
    positions: [{ symbol: "NVDA", qty: "4", side: "long" }],
  }],
};

function reset() { log.api = []; log.dialogs = []; log.toasts = []; log.reloads = 0; log.bars = 0; }
function pick(name) { els["strategy-select"].value = name; }

(async function () {
  const out = {};

  // 1. Nothing open: the switch is a plain write — no dialog, no friction.
  readFails = false;
  tradingRead = { open_count: 0, positions: [] };
  pick("beta");
  reset();
  await onStrategySelect();
  out.quiet = { dialogs: log.dialogs.length, api: log.api, reloads: log.reloads };

  // 2. Same instrument held: it is ADOPTED and closed by the new strategy's own rules.
  tradingRead = NVDA_HELD;
  pick("beta");
  reset();
  confirmAnswer = true;
  await onStrategySelect();
  out.sameInstrument = { dialogs: log.dialogs, api: log.api, reloads: log.reloads };

  // 3. Another instrument held: the new strategy ignores it and it stays where it is.
  pick("gamma");
  reset();
  confirmAnswer = true;
  await onStrategySelect();
  out.otherInstrument = { dialogs: log.dialogs, api: log.api };

  // 4. Declining writes nothing at all, and puts the picker back.
  reset();
  confirmAnswer = false;
  await onStrategySelect();
  out.declined = {
    dialogs: log.dialogs.length, api: log.api, reloads: log.reloads,
    bars: log.bars, msg: els["strategy-msg"].textContent, toasts: log.toasts.length,
  };

  // 5. A payload that could not be read is not a warning: nothing is known, nothing is claimed.
  pick("beta");
  reset();
  readFails = true;
  await onStrategySelect();
  out.unreadable = { dialogs: log.dialogs.length, api: log.api, reloads: log.reloads };

  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.fixture(scope="module")
def switch_results(tmp_path_factory) -> dict:
    """Run the real lab handler under node and return what it did."""
    script = tmp_path_factory.mktemp("switch") / "switch.js"
    script.write_text(app_module() + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "strategy switch harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def _calls(got: dict):
    """What the switch did over HTTP, split into the read it always makes and the write.

    The read is not incidental: it is what the warning quotes, taken at the moment of the click
    rather than from the page's last poll. Both halves are asserted, so a switch that stopped
    asking the server what is open would not pass as one that asked and was told nothing.
    """
    reads = [call for call in got["api"] if call["path"] == "/api/v1/trading"]
    writes = [call for call in got["api"] if call["path"] == "/api/v1/rules/select"]
    return reads, writes


def test_a_switch_with_nothing_open_asks_nothing(switch_results):
    got = switch_results["quiet"]
    reads, writes = _calls(got)
    assert got["dialogs"] == 0
    assert len(reads) == 1, "what is open is read at the click, not remembered"
    assert writes == [{"path": "/api/v1/rules/select", "body": {"name": "beta"}}]
    assert got["reloads"] == 1, "a switch that went through re-renders the whole page"


def test_the_same_instrument_is_said_to_be_taken_over(switch_results):
    """Scenario two: the position becomes the new strategy's, and its own rules close it."""
    got = switch_results["sameInstrument"]
    _, writes = _calls(got)
    assert len(got["dialogs"]) == 1
    dialog = got["dialogs"][0]
    assert dialog["title"] == "Switch to \u201cbeta\u201d with a position still open?"
    assert "4 NVDA in the paper account" in dialog["messageHtml"], "what is open is named"
    assert "takes the position over" in dialog["messageHtml"]
    assert "closes it when its own rules say so" in dialog["messageHtml"]
    assert dialog["confirmText"] == "Switch strategy"
    assert dialog["kind"] == "warn", "a switch that leaves a position open is a warning"
    assert writes == [{"path": "/api/v1/rules/select", "body": {"name": "beta"}}]
    assert got["reloads"] == 1


def test_another_instrument_is_said_to_be_ignored(switch_results):
    """Scenario one: the position is not in what the new strategy trades, so nothing touches it."""
    got = switch_results["otherInstrument"]
    _, writes = _calls(got)
    assert len(got["dialogs"]) == 1
    message = got["dialogs"][0]["messageHtml"]
    assert "trades <b>GPRO</b>" in message, "the instrument it WILL trade is named"
    assert "will ignore that position" in message
    assert "stays open until you flatten it" in message, "and that it is left alone, not closed"
    assert "takes the position over" not in message, "the other case must not be described here"
    assert writes == [{"path": "/api/v1/rules/select", "body": {"name": "gamma"}}]


def test_declining_the_switch_changes_no_strategy(switch_results):
    got = switch_results["declined"]
    _, writes = _calls(got)
    assert got["dialogs"] == 1
    assert writes == [] and got["reloads"] == 0, "nothing was written"
    assert got["bars"] == 1, "the picker is put back to the active strategy"
    assert "not changed" in got["msg"]


def test_a_payload_that_could_not_be_read_is_not_a_warning(switch_results):
    """Nothing known is not something open: the read failing must not invent a prompt, and it
    must not freeze the picker either — switching spends no money on its own."""
    got = switch_results["unreadable"]
    reads, writes = _calls(got)
    assert got["dialogs"] == 0
    assert len(reads) == 1, "the read was attempted and failed"
    assert writes == [{"path": "/api/v1/rules/select", "body": {"name": "beta"}}]
    assert got["reloads"] == 1
