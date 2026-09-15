"""When does the credential row show a verdict?

The badge is a RESULT, not a state. A stored verdict — including a failure from an
earlier session — is deliberately not painted when the popup opens, because the box
beside it is empty or masked, so "⚠ not valid" appears next to what looks like no
credentials at all. The row stays clean until a check is actually run: the Validate
button, or the check a save performs on a newly added pair.

That distinction is invisible in a string assertion, so the real `credentialRow`,
`applyCredentialBadge` and `validateCredentials` are lifted out of `app.js` and run
under node against a fake DOM and a fake API.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "function credWhen("
END_MARKER = "\nfunction switchDataset("

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the badge"
)

HARNESS = r"""
// ---- a DOM just big enough -------------------------------------------------
function el(tag) {
  return {
    tagName: tag, children: [], dataset: {}, hidden: false, className: "",
    textContent: "", title: "", type: "", id: "", onclick: null,
    appendChild(c) { this.children.push(c); return c; },
  };
}
const byId = {};
const document = {
  createElement: el,
  getElementById(id) { return byId[id] || null; },
};

// ---- fakes the block reaches for -------------------------------------------
const calls = { api: [], toasts: [], messages: [], loads: 0 };
let nextReply = {};
function api(path, opts) {
  calls.api.push({ path: path, body: JSON.parse(opts.body) });
  return Promise.resolve(nextReply);
}
function flashToast(text) { calls.toasts.push(text); }
function showAccountErrors(text, kind) { calls.messages.push({ text: text, kind: kind }); }
function loadTrading() { calls.loads += 1; return Promise.resolve(); }
const $ = (id) => byId[id] || null;

const SPEC = {
  env: "paper",
  key_key: "ALPACA_PAPER_API_KEY",
  secret_key: "ALPACA_PAPER_API_SECRET",
};
const LIVE_SPEC = {
  env: "live",
  key_key: "ALPACA_LIVE_API_KEY",
  secret_key: "ALPACA_LIVE_API_SECRET",
};

function badgeFor(env) {
  const b = byId["cred-badge-" + env];
  return { hidden: b.hidden, text: b.textContent, cls: b.className, title: b.title };
}

function freshBadges() {
  // Start CLEAN, the way a freshly rendered row is: anything the code paints after
  // this is the app's doing, and a row it leaves alone stays provably untouched.
  for (const env of ["paper", "live"]) {
    const b = el("span");
    b.id = "cred-badge-" + env;
    b.hidden = true;
    b.className = "cred-badge";
    byId["cred-badge-" + env] = b;
  }
}

async function pressValidate(spec) {
  const btn = el("button");
  await validateCredentials(spec, btn);
  return btn;
}

(async function () {
  const out = {};

  // 1. A brand-new row: nothing to report, whatever is on file.
  freshBadges();
  const row = credentialRow(SPEC);
  byId["cred-badge-paper"] = row.children[0]; // the row built its own element
  out.newRow = {
    children: row.children.length,
    badge: badgeFor("paper"),
    cleanRowHasNoText: row.children[0].textContent === "",
  };

  // 2. The buttons exist and offer the action for the right environment.
  out.button = { text: row.children[1].textContent, id: row.children[1].id };

  // 3. Opening the popup clears whatever a previous visit left behind.
  byId["cred-badge-paper"].hidden = false;
  byId["cred-badge-paper"].textContent = "⚠ not valid";
  byId["cred-badge-live"].hidden = false;
  byId["cred-badge-live"].textContent = "✓ verified";
  clearCredentialBadges();
  out.afterOpen = { paper: badgeFor("paper"), live: badgeFor("live") };

  // 4. A REJECTED pair: the row says so, and so does the message area.
  freshBadges();
  byId["acct-ALPACA_PAPER_API_KEY"] = { value: "PK-typed" };
  byId["acct-ALPACA_PAPER_API_SECRET"] = { value: "PS-typed" };
  calls.api = []; calls.messages = [];
  nextReply = {
    ok: false,
    result: { env: "paper", checked: true, ok: false, has_verdict: true, message: "Alpaca rejected these credentials (401)" },
    // The response also carries the STORED state of both pairs. It must not be
    // painted: that is what put "⚠ not valid" next to a row nobody had asked about.
    credentials: {
      paper: { has_verdict: true, verified: false, message: "stale" },
      live: { has_verdict: true, verified: false, message: "stale live failure" },
    },
  };
  await pressValidate(SPEC);
  out.rejected = {
    badge: badgeFor("paper"), other: badgeFor("live"),
    messages: calls.messages.slice(), api: calls.api.slice(),
  };

  // 5. Nothing to check: the press is answered in words, the row stays clean.
  freshBadges();
  calls.messages = [];
  nextReply = {
    ok: false,
    result: { env: "live", checked: false, ok: false, keys_set: false, message: "No LIVE API key or secret to verify." },
    credentials: { live: { has_verdict: false, verified: false, message: "" } },
  };
  await pressValidate(LIVE_SPEC);
  out.nothingToCheck = { badge: badgeFor("live"), messages: calls.messages.slice() };

  // 6. An accepted pair: the verdict, the account, and a nudge when it is not saved.
  freshBadges();
  calls.messages = [];
  nextReply = {
    ok: true,
    result: {
      env: "live", checked: true, ok: true, verified: true, has_verdict: true, saved: false,
      message: "Credentials accepted — account A1 (ACTIVE)", account_number: "A1",
      checked_at: "2026-09-15T12:00:00+00:00",
    },
    credentials: {},
  };
  await pressValidate(LIVE_SPEC);
  out.accepted = { badge: badgeFor("live"), messages: calls.messages.slice() };

  // 7. A pair that was already verified is a pass, not a fresh probe: the save path
  //    hands those through with checked=false, and the badge must still appear.
  freshBadges();
  applyCredentialBadge(byId["cred-badge-paper"], {
    has_verdict: true, verified: true, checked: false, account_number: "PA3",
    checked_at: "2026-09-15T09:30:00+00:00", message: "Already verified",
  });
  out.savedAlreadyVerified = badgeFor("paper");

  // 8. A pair with no verdict must never produce a badge, checked or not.
  freshBadges();
  applyCredentialBadge(byId["cred-badge-paper"], { has_verdict: false });
  out.noVerdict = badgeFor("paper");

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def badge_results(tmp_path_factory) -> dict:
    """Run the real badge code under node and return what each case produced."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    block = src[start:src.index(END_MARKER, start)]
    script = tmp_path_factory.mktemp("badge") / "badge.js"
    script.write_text(block + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "credential badge harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_a_new_row_reports_nothing(badge_results):
    """The reported bug: the row carried a stored verdict before anything was asked."""
    got = badge_results["newRow"]
    assert got["badge"] == {"hidden": True, "text": "", "cls": "cred-badge", "title": ""}
    assert got["cleanRowHasNoText"] is True
    assert got["children"] == 2, "a badge and a button, nothing else"


def test_the_row_offers_validate_for_its_own_environment(badge_results):
    got = badge_results["button"]
    assert got["text"] == "Validate paper credentials"
    assert got["id"] == "cred-verify-paper"


def test_opening_the_popup_clears_a_previous_verdict(badge_results):
    got = badge_results["afterOpen"]
    for env in ("paper", "live"):
        assert got[env]["hidden"] is True and got[env]["text"] == ""


def test_a_rejected_pair_is_reported(badge_results):
    got = badge_results["rejected"]
    assert got["badge"]["hidden"] is False
    assert got["badge"]["text"] == "⚠ not valid"
    assert got["badge"]["cls"] == "cred-badge bad"
    assert "401" in got["badge"]["title"], "the reason goes on hover"
    assert got["api"] == [
        {
            "path": "/api/v1/account/verify",
            "body": {"env": "paper", "key_id": "PK-typed", "secret": "PS-typed"},
        }
    ], "the values in the boxes are what gets checked"
    assert "401" in got["messages"][-1]["text"]


def test_another_pairs_stored_failure_is_not_painted(badge_results):
    """The response carries the stored state of every pair; only the checked one may
    reach the screen, or an untouched row inherits someone else's verdict."""
    assert badge_results["rejected"]["other"]["hidden"] is True
    assert badge_results["rejected"]["other"]["text"] == ""


def test_a_press_with_nothing_to_check_leaves_the_row_clean(badge_results):
    got = badge_results["nothingToCheck"]
    assert got["badge"]["hidden"] is True and got["badge"]["text"] == ""
    assert "No LIVE API key" in got["messages"][-1]["text"]
    # Information, not a failure: neither the warning nor the success colour.
    assert got["messages"][-1]["kind"] == ""


def test_an_accepted_pair_shows_the_verdict_and_the_account(badge_results):
    got = badge_results["accepted"]
    assert got["badge"]["hidden"] is False
    assert got["badge"]["cls"] == "cred-badge ok"
    assert "✓ verified" in got["badge"]["text"]
    assert "A1" in got["badge"]["text"]
    assert got["messages"][-1]["kind"] == "ok"
    assert "not the saved pair yet" in got["messages"][-1]["text"]


def test_a_save_time_check_still_shows_its_verdict(badge_results):
    """`verify_new` passes an already-verified pair through without a probe; that is
    still an answer to the save, so it is shown."""
    got = badge_results["savedAlreadyVerified"]
    assert got["hidden"] is False and got["cls"] == "cred-badge ok"


def test_no_verdict_means_no_badge(badge_results):
    assert badge_results["noVerdict"]["hidden"] is True
