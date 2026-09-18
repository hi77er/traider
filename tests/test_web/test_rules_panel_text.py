"""No description in the Rules panel — the panel has to explain itself.

It carried a paragraph, then one sentence, then nothing. The progression is the point:
the paragraph explained that rules are stored in the strategy file (the status line under
it already prints that path on every load), and that a condition compares a feature with a
number or another feature (the condition rows ARE that — three dropdowns and an input). A
line that restates the controls on either side of it is read once and skipped forever
after, and it pushed the rules themselves down the panel.

What must NOT be deleted with it is the STATUS line. It looks similar — one muted line at
the top of the body — but it is output, not description: "Editing <path>", "Copied 2 rules
from X — press 💾 Save", "Save failed: …". Dropping that would leave a panel that can fail
silently, so it is pinned here as firmly as the description's absence.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")


def _rules_card() -> str:
    return HTML[HTML.index('id="rules-card"') : HTML.index('id="risk-card"')]


def _paragraphs(card: str) -> list:
    return re.findall(r"<p\b[^>]*>.*?</p>", card, re.S)


def _code(js: str) -> str:
    """``js`` with its comments removed — prose about a rule is not the rule."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))


def test_the_rules_panel_has_no_description():
    """One paragraph in the whole card, and it is the status line, not prose."""
    card = _rules_card()
    paragraphs = _paragraphs(card)
    assert len(paragraphs) == 1, f"the panel has {len(paragraphs)} paragraphs: {paragraphs}"
    assert paragraphs[0].startswith('<p id="rules-msg"')
    assert 'class="muted note"' not in card
    # The path is not printed here either — see test_the_store_path_is_not_printed_at_rest.
    assert "data/strategies/store.json" not in card


def test_no_leftover_copy_of_the_old_description():
    """Gone from the panel, and not parked somewhere else in the page."""
    assert "Rule-based conditions" not in HTML
    assert "comparing a feature" not in HTML
    assert 'id="rules-file"' not in HTML
    # Nothing in the JS reaches for that element either.
    assert "rules-file" not in APP_JS


def test_the_status_line_survives_and_starts_empty():
    """A status line that ships with text in it is a description wearing a status alias."""
    card = _rules_card()
    assert '<p id="rules-msg" class="muted"></p>' in card
    msg = APP_JS[APP_JS.index("function setMsg(") :][:120]
    assert '$("rules-msg")' in msg
    # A successful load clears it, rather than leaving whatever was there before.
    load = _code(APP_JS[APP_JS.index("async function loadRules()") :][:1400])
    assert 'setMsg(d.error ? `⚠ ${d.error} — no file written.` : "");' in load


def test_the_store_path_is_not_printed_at_rest():
    """The panel does not say where the rules are stored — not while it is working anyway.

    "Editing data/strategies/store.json" was the first line under the header on every
    load: a description of the file rather than news about the run. The API still reports
    the path (the payload carries ``file``); the panel just does not recite it.
    """
    assert "Editing ${d.file}" not in APP_JS
    assert "New file will be created at" not in APP_JS
    assert "data/strategies/store.json" not in APP_JS
    # …and nothing else in the panel echoes it either.
    load = _code(APP_JS[APP_JS.index("async function loadRules()") :][:1400])
    assert "Editing" not in load
    assert "${d.file}" not in load


def test_the_panel_still_reports_a_failure():
    """A load that FAILED has to say so — that is the one non-static thing on the line.

    Silence on success is only safe because failure is not silent: a store that cannot be
    read leaves the panel looking empty-but-fine, which is the one reading nobody can
    recover from.
    """
    load = _code(APP_JS[APP_JS.index("async function loadRules()") :][:1400])
    assert "d.error ? `⚠ ${d.error} — no file written.` : \"\"" in load, (
        "the error branch is the reason the success branch may say nothing"
    )
    # The phrase the sentence used to be is gone; the one that must survive is not.
    assert "setMsg(`Failed to load rules: ${err.message}`)" in APP_JS


def test_the_panel_still_reports_what_an_action_did():
    """What the status line carries, so it is not quietly emptied one day."""
    for phrase in (
        "Copied ",  # the copy reports what it took, and what is left to do
        "Press 💾 Save to keep them.",
        "has no rules to copy",
    ):
        assert phrase in APP_JS


def test_the_panel_keeps_its_controls_and_its_list():
    """Brevity must not have taken the panel apart."""
    card = _rules_card()
    for control in ("rules-add-buy", "rules-add-sell", "rules-copy", "save-rules"):
        assert f'id="{control}"' in card
    assert 'id="rules-copy-row"' in card
    assert 'id="rules-list"' in card
    # Order inside the body: status, the picker (when open), then the rules themselves.
    body = card[card.index('id="rules-body"') :]
    assert body.index('id="rules-msg"') < body.index('id="rules-copy-row"') < body.index('id="rules-list"')
