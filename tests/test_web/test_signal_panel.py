"""What the Strategy Signals panel says, and how it reads.

The panel is a badge, a date and then three lines of prose under it. Read as prose, a line that
starts lowercase looks like a fragment of the one above it — the same complaint the log page's
clock caption answered — so every sentence in it starts with a capital, including the ones that
begin after an em dash inside a line.

The capital is applied at RENDER, and that split is deliberate: the model's own reasons ("no rule
fired", "conflicting BUY and SELL at equal confidence") are reused as table cells in the report
and the log, where the house style is a lowercase fragment. Changing the model's strings to fix a
panel would have carried the capital into those cells.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    """The body of ``name``, balanced out to its closing brace."""
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces after {name} in the source")


def test_the_model_reasons_are_left_alone():
    """The source strings stay lowercase — the panel capitalises them on the way out.

    Asserted because "fix the capital" is most obviously done by editing ``simple_model.py``, and
    that would quietly change every table cell that shows the same reason.
    """
    model = (ROOT / "src" / "model" / "simple_model.py").read_text(encoding="utf-8")
    assert '"no rule fired"' in model, "the model's reason is unchanged"

    assert "function sentenceCase(" in APP_JS, "the capital comes from the renderer"
    helper = _function(APP_JS, "sentenceCase")
    assert "charAt(0).toUpperCase()" in helper
    assert "s.slice(1)" in helper, "only the first character is touched"


def test_every_sentence_in_the_signals_panel_starts_with_a_capital():
    """The sentences that remain, and the unavailable note — which is a line of prose too.

    Checked on the START of each rendered line rather than on its words: this file is about the
    capital, and pinning the sentences themselves would fail every time they are reworded (which
    is often — they have been shortened twice).
    """
    body = _function(APP_JS, "renderSignals")

    assert "sentenceCase(reason)" in body, "the signal's own reason: 'No rule fired'"
    assert "sentenceCase((d && d.reason) ||" in body, "and the reason when signals are unavailable"
    for line in ('signal-counts">Model:', "· Rule signals not shown"):
        assert line in body, f"{line} opens with a capital"


def test_the_counts_are_boxes_and_the_fills_sentence_is_gone():
    """Asked for as info boxes: BUY, SELL and HOLD as the same `.bt-stat` tiles every other
    screen uses. The risk STATE that used to be the fourth box is its own section under this one
    now, because a box saying "on"/"off" reported that the settings exist while the settings
    themselves are what decides whether these are the numbers the bot would act on.

    The sentence they replace is removed ENTIRELY, not shortened — including the fills line, the
    exit breakdown and the shading legend that lived in it. The legend explained the coloured
    bands on the price chart and now has no home on the dashboard; the chart's own notes are
    where it would go if it is wanted back.
    """
    body = _function(APP_JS, "signalTiles")

    for value in ('statTile("Buy"', 'statTile("Sell"', 'statTile("Hold"'):
        assert value in body, value
    assert 'liveTile("Risk"' not in body, "the risk state is the Risk section's now"
    assert "signal-metrics" in body, "in a grid of its own"

    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".signal-metrics {" in css and "display: grid" in css, (
        "the shared grid is scoped to #bt-metrics, so this panel declares its own"
    )

    assert "fillsNote" not in APP_JS, "the sentence is gone, not merely unused"
    for legacy in ("Actual fills", "Shading marks", "round-trip(s)"):
        assert legacy not in APP_JS, f"{legacy!r} is still rendered"


def test_the_panel_prose_is_matched_by_the_lowercase_originals():
    """A guard against the obvious false positive: the lowercase versions must be GONE from the
    rendered strings, or the capital was added somewhere harmless."""
    for legacy in ('signal-counts">model', '"over the dataset:', '"actual fills:',
                   "— the shading", "<b>risk layer off</b>"):
        assert legacy not in APP_JS, f"{legacy!r} is still rendered lowercase"


def test_the_labels_are_left_lowercase():
    """A label is not a sentence: "as of" and "conf" head a value, and the two toggle captions
    name a control. Capitalising those would be the rule applied to the wrong thing."""
    body = _function(APP_JS, "renderSignals")

    assert 'class="label">as of</span>' in body
    assert 'class="label">conf</span>' in body
    # The toggles moved to the Price History panel (they decide what is drawn on that chart), so
    # their captions are read from the template now — still lowercase, for the same reason.
    html = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"markers on chart\s*</label>", html), "the toggle captions are unchanged"
    assert re.search(r"hide unexecuted signals\s*</label>", html)


def test_which_rule_fired_sits_on_the_line_of_the_reading_it_explains():
    """Asked for: the reason goes to the RIGHT of "as of … conf", not under the badge.

    It was a paragraph of its own, which made it read as a remark about the panel rather than
    the explanation of that reading — and the eye had to leave the badge to find it. Same string,
    same sentence case, one line."""
    body = _function(APP_JS, "renderSignals")
    latest = body[body.index('class="signal-latest"'):body.index("signalTiles")]

    assert '<span class="muted signal-reason">' in latest
    assert "sentenceCase(reason)" in latest, "the capital it already had comes with it"
    assert '<p class="muted signal-reason">' not in latest
