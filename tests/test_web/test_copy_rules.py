"""Copy every rule of another strategy into this one — and never lose a rule doing it.

Two strategies on two symbols usually want the same conditions, so the Rules panel can
take the whole rulebook of another strategy: **all** of it, enabled and disabled alike,
appended to whatever the active strategy already has.

The three ways this can go wrong, each pinned below:

* a copy REPLACES instead of appends, and the rules the user had built disappear;
* the copy skips the DISABLED rules (they are the half nobody notices are missing until
  a backtest fires on a setup the panel no longer shows);
* both strategies end up sharing rule OBJECTS, so editing one silently rewrites the
  other and a later Save pushes the edit into a strategy the user never opened.

The copy is a local edit, like ``+ Buy``/``+ Sell``: it lands in the panel's in-memory
copy of the active strategy and is persisted by 💾 Save. That is why there is no
``/api/v1/rules/copy`` endpoint to test — and why the trading lock has to disable the
button, since a write it cannot save is not an edit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src" / "web"
APP_JS = WEB / "static" / "app.js"
STYLE_CSS = WEB / "static" / "style.css"
INDEX_HTML = WEB / "templates" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the copy in JS"
)


def _src() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """One top-level ``function name(...) { ... }`` from the shipped app.js."""
    src = _src()
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}", start) + len("\n}")] + "\n"


def _run(program: str):
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


# ---------------------------------------------------------------------------
# The button, and what it opens
# ---------------------------------------------------------------------------
def test_the_rules_panel_has_a_copy_button():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="rules-copy"' in html
    assert "Copy rules from" in html
    # It belongs to the Rules panel's own action row, beside + Buy / + Sell / Save.
    actions = html[html.index('id="rules-add-buy"') : html.index('id="save-rules"')]
    assert 'id="rules-copy"' in actions
    # …and the picker it opens is part of the panel body, above the list it extends.
    body = html[html.index('id="rules-body"') : html.index('id="rules-list"')]
    assert 'id="rules-copy-row"' in body
    assert 'id="rules-copy-list"' in body
    assert 'id="rules-copy-row" class="rules-copy-row" hidden' in body


def test_the_picker_is_hidden_until_the_button_is_pressed():
    src = _src()
    assert 'onclick="toggleCopyRules()"' in INDEX_HTML.read_text(encoding="utf-8")
    toggle = _function_source("toggleCopyRules")
    # Opening renders the sources; toggling again closes it. The row starts hidden in
    # the markup, so the button is the only way in.
    assert "renderCopySources()" in toggle
    assert "row.hidden = !row.hidden;" in toggle
    # A collapsed panel would hide the picker it just opened.
    assert "ensureRulesOpen();" in toggle
    assert 'body.hidden = false;' in _function_source("ensureRulesOpen")


def test_clicking_a_strategy_is_the_whole_interaction():
    """No select-then-confirm step: the choice IS the action."""
    src = _src()
    source = _function_source("renderCopySources")
    assert "data-src=" in source, "each source carries the strategy name"
    assert "copy-src" in source
    # The click is delegated off the container the picker is rebuilt into, so the
    # handler cannot go stale — and a user-typed name is never interpolated into an
    # onclick attribute.
    assert 'data-src="${escapeHtml(n)}"' in source
    assert "onclick=" not in source
    assert 'const btn = e.target.closest("button[data-src]");' in src
    assert "copyRulesFrom(btn.dataset.src);" in src


# ---------------------------------------------------------------------------
# What gets copied
# ---------------------------------------------------------------------------
def _copy(existing, incoming, nudge=False):
    """Run the shipped ``appendedRules`` and log what it produced.

    ``nudge`` edits the last MERGED rule afterwards, which is how the deep-clone test
    finds out whether the source strategy shares that object.
    """
    program = (
        _function_source("appendedRules")
        + "\nconst existing = %s;" % json.dumps(existing)
        + "\nconst source = %s;" % json.dumps(incoming)
        + "\nconst merged = appendedRules(existing, source);"
        + (
            "\nif (merged.length) merged[merged.length - 1].conditions.push("
            "{feature: 'rsi_14', op: '<', value: 1, ref: null});"
            if nudge
            else ""
        )
        + "\nconsole.log(JSON.stringify({merged, existing, source}));"
    )
    return _run(program)


_RULE_A = {
    "side": "BUY",
    "mode": "all",
    "enabled": True,
    "confidence": 0.75,
    "conditions": [{"feature": "close", "op": ">", "value": 1.0, "ref": None}],
}
_RULE_B = {
    "side": "SELL",
    "mode": "any",
    "enabled": False,
    "confidence": 0.4,
    "conditions": [{"feature": "rsi_14", "op": ">", "value": 70.0, "ref": None}],
}


def test_every_rule_is_copied_and_nothing_is_deleted():
    """Existing rules stay, in place; the copies follow them in the source's order."""
    got = _copy([_RULE_A], [_RULE_A, _RULE_B])["merged"]
    assert len(got) == 3, "1 kept + 2 copied"
    assert got[0] == _RULE_A, "the rule that was already here is untouched"
    assert got[1]["side"] == "BUY"
    assert got[2]["side"] == "SELL"


def test_disabled_rules_come_across_too():
    """A disabled rule is still a rule: it is kept, and it must not be silently dropped."""
    got = _copy([], [_RULE_A, _RULE_B])["merged"]
    assert [r["enabled"] for r in got] == [True, False]
    assert got[1]["mode"] == "any" and got[1]["confidence"] == 0.4


def test_the_copy_is_a_deep_clone():
    """Editing a copy must not reach into the strategy it came from.

    Shallow copies are the trap: both strategies would share rule objects, an edit in one
    would appear in the other, and a later Save would write it into a strategy the user
    never opened.
    """
    got = _copy([_RULE_A], [_RULE_B], nudge=True)
    # The harness edits the LAST copied rule after the fact. The source must be
    # untouched, and so must the rule that was already here.
    assert got["source"] == [_RULE_B], "the source strategy was mutated by editing the copy"
    assert len(got["source"][0]["conditions"]) == 1
    assert len(got["merged"][-1]["conditions"]) == 2, "the edit did land on the copy"
    assert len(got["merged"][0]["conditions"]) == 1, "the existing rule was not the target"
    assert got["existing"] == [_RULE_A]


def test_copying_into_an_empty_ruleset_just_works():
    got = _copy(None, [_RULE_A])["merged"]
    assert len(got) == 1
    assert got[0] == _RULE_A, "copied intact, with its conditions"
    assert _copy([], [])["merged"] == []


# ---------------------------------------------------------------------------
# Where the rules can come from, and when the button is available
# ---------------------------------------------------------------------------
def test_the_active_strategy_is_never_offered_as_its_own_source():
    source = _function_source("copySources")
    assert "filter((n) => n !== me && copiedCount(n) > 0)" in source
    assert "state.activeName" in source
    # Before renderRules has normalised activeName, the payload's own `active` is the
    # fallback — otherwise the first render offers to copy a strategy into itself.
    assert "state.rulesPayload.active" in source


def _sources(active, strategies, payload_active=None):
    """Run the shipped ``copySources`` over a strategy map."""
    stub = (
        "const state = { activeName: %s, rulesPayload: { active: %s, strategies: %s } };"
        % (
            json.dumps(active),
            json.dumps(payload_active if payload_active is not None else active),
            json.dumps(strategies),
        )
        + "\nfunction rulesStrategies() { return state.rulesPayload.strategies; }"
    )
    program = stub + _function_source("copiedCount") + _function_source("copySources")
    program += "\nconsole.log(JSON.stringify(copySources()));"
    return _run(program)


def test_a_strategy_with_no_rules_is_not_offered():
    """An empty rulebook is not a choice — it can only copy nothing.

    The picker used to list every strategy with a "(0)" beside it, so half the options
    were dead ends that answered "has no rules to copy".
    """
    strategies = {
        "mine": {"rules": [{"side": "BUY"}]},
        "empty": {"rules": []},
        "no-key": {},
        "full": {"rules": [{"side": "BUY"}, {"side": "SELL"}]},
        "disabled-only": {"rules": [{"side": "SELL", "enabled": False}]},
    }
    assert _sources("mine", strategies) == ["full", "disabled-only"]


def test_nothing_is_offered_when_no_other_strategy_has_rules():
    strategies = {"mine": {"rules": [{"side": "BUY"}]}, "empty": {"rules": []}}
    assert _sources("mine", strategies) == []
    # …and the button says so rather than opening an empty picker.
    source = _function_source("updateCopyButton")
    assert "No other strategy has rules to copy yet" in source
    assert 'No other strategy has rules yet' in _function_source("renderCopySources")


def test_the_default_rules_button_is_gone():
    """Removed rather than hidden: it was never used, and it overwrites a rulebook."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="rules-reset"' not in html
    assert "Default rules" not in html
    assert "resetRules" not in html
    src = _src()
    assert "function resetRules" not in src
    assert "rules/reset" not in src
    # …and it is no longer in the shared enabled/disabled list either.
    assert '"rules-reset"' not in src
    # The endpoint itself is untouched: the trading-gate tests pin it, and a strategy
    # still gets seeded from the default example when it is created.
    assert '/api/v1/rules/reset' not in src
    routes = (WEB / "routes" / "rules.py").read_text(encoding="utf-8")
    assert '@router.post("/reset"' in routes


def test_the_button_is_disabled_without_another_strategy_or_while_trading():
    source = _function_source("updateCopyButton")
    assert "btn.disabled = !names.length || !!state.tradingLocked;" in source
    # And it is refreshed from renderStrategyBar, which runs on load AND on every lock
    # change (applyConfigLock asks it to recompute) — so the lock can never leave a
    # clickable button behind.
    bar = _function_source("renderStrategyBar")
    assert "updateCopyButton();" in bar
    lock = _src()[_src().index("function applyConfigLock()") :][:1200]
    assert "renderStrategyBar()" in lock


def test_the_copy_says_what_it_did_and_what_is_left_to_do():
    source = _function_source("copyRulesFrom")
    assert "Press 💾 Save to keep them." in source
    assert "renderRules();" in source, "the panel redraws with the new rules"
    assert "syncBtRules();" in source, "the backtest gate is recomputed"
    # A source with nothing to copy says so instead of silently doing nothing.
    assert "has no rules to copy" in source


def test_the_picker_is_styled_for_the_rules_panel():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".rules-copy-row {" in css
    assert ".rules-copy-row[hidden] { display: none; }" in css
    assert ".rules-copy-list {" in css
    assert "button.copy-src {" in css


def test_no_new_endpoint_was_needed():
    """The copy edits one strategy's rules locally; 💾 Save persists them.

    A server round trip would have to write the store on a click, which is exactly what
    the panel's explicit Save (and the trading lock behind it) is there to prevent.
    """
    routes = (WEB / "routes" / "rules.py").read_text(encoding="utf-8")
    assert "/copy" not in routes
    assert "def copy" not in routes
