"""The Risk section under the signals: every risk setting, as boxes.

The signal panel used to carry ONE risk box, saying "on" or "off". That answered whether the
settings exist, not what they are — and "what they are" is the whole question, because in this
project an EMPTY risk setting is NOT APPLIED rather than a gap to be filled in later. Five of the
eight settings ship empty on a new strategy, so a section that showed "—" five times would say
nothing about what the bot would do with the next signal.

So the box prints the schema's own words for empty ("no stop", "the whole account"), and the panel
that EDITS the settings (Risk Management) is left where it is.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# where it sits


def test_the_section_has_the_right_hand_slot_to_itself():
    """The two halves of the row balance: what the strategy would DO on the left (the rules,
    with the signals they produce under them) and what it would RISK on the right.

    Trading used to fill the left slot; it is on the Session monitor now, and the rules took its
    place — so the risk boxes face the rules they change rather than following them.
    """
    row = INDEX[INDEX.index('class="strategy-rules-row"') :]
    row = row[: row.index("</div>\n    </div>")]
    slots = row.split('class="strategy-slot"')
    assert len(slots) == 3, "two slots, in order"
    left, right = slots[1], slots[2]

    assert 'id="strategy-rules"' in left and 'id="signals"' in left
    assert 'id="signals"' not in right, "the signals are the left slot's"
    assert 'id="strategy-risk"' in right and 'id="strategy-rules"' not in right
    # The signals read the rules, so they follow them in the same column.
    assert left.index('id="strategy-rules"') < left.index('id="signals"')


def test_the_section_has_a_title_and_an_empty_host_of_its_own():
    """The heading is static markup, the boxes are filled in: the section must not be a div that
    looks empty until a payload arrives."""
    assert '<h3 class="strategy-section-title">Risk</h3>' in INDEX
    assert 'id="strategy-risk" class="risk-metrics"' in INDEX
    block = INDEX[INDEX.index("<h3 class=\"strategy-section-title\">Risk</h3>") :]
    block = block[: block.index("</div>")]
    assert "strategy-risk" in block, "the heading and the host are the whole section"
    assert block.count("<div") == 1, "one host, so nothing else is inside the section"


def test_the_risk_state_box_is_gone_from_the_signals():
    """The old fourth box, removed rather than hidden: two places reporting the risk layer is
    how they come to disagree."""
    assert 'liveTile("Risk"' not in APP_JS
    assert '"on" : "off"' not in _function(APP_JS, "signalTiles")
    tiles = _function(APP_JS, "signalTiles")
    assert "counts.BUY" in tiles and "counts.SELL" in tiles and "counts.HOLD" in tiles


def test_the_boxes_are_the_same_tiles_every_other_panel_uses():
    """`.bt-stat` via ``liveTile``, in a grid of its own — the shared grid rule is scoped to
    ``#bt-metrics``, and this section sits in a half-width slot."""
    body = _function(APP_JS, "strategyRiskTiles")
    assert 'statTile(f.label, riskValue(f, value)' in body
    assert ".risk-metrics {" in CSS and "display: grid" in CSS
    assert "repeat(auto-fill, minmax(" in CSS[CSS.index(".risk-metrics {") :][:200]


# ---------------------------------------------------------------------------
# what a box says


def test_an_empty_setting_prints_what_empty_MEANS():
    """The rule the risk layer is built on: blank is a decision, so the box says which one.

    "—" would be read as "not filled in yet" and invite someone to fill it in, when the honest
    answer is that the bot has no stop, or is sized by the whole account.
    """
    body = _function(APP_JS, "riskValue")
    assert "field.empty_means" in body
    assert 'trim()' in body, "a value of spaces is empty too"
    assert "escapeHtml" in body, "the schema's words are still text on a page"


def test_a_bool_says_on_or_off_and_a_token_loses_its_underscores():
    """ALLOW_SHORT is "False" and POSITION_SIZING_MODE is "fixed_risk": both are storage
    spellings, and neither is what to print in a box a person reads."""
    body = _function(APP_JS, "riskValue")
    assert 'field.type === "bool"' in body
    assert '"True" ? "on" : "off"' in body
    assert 'replace(/_/g, " ")' in body


def test_the_value_comes_from_the_same_place_the_editing_panel_reads_it():
    """Stored value, else the default it would fall back to — one resolution, so the box under
    the signals and the field in Risk Management cannot disagree."""
    body = _function(APP_JS, "strategyRiskTiles")
    assert "strategyRiskGroups()" in body, "the payload's own group list, not a copy of it"
    assert "activeRuleset()" in body and "rs.config" in body
    assert "stored[f.key] != null ? stored[f.key] : f.default_value" in body
    assert "(f.hints || []).join(\" \")" in body, "the hint stays the box's tooltip"


def test_a_missing_payload_says_so_instead_of_rendering_nothing():
    """An empty grid under a heading reads as "no risk settings", which is the one thing it must
    never appear to say."""
    body = _function(APP_JS, "renderStrategyRisk")
    assert '$("strategy-risk")' in body
    assert "No active strategy" in body
    assert "setIfChanged" in body, "re-rendered on every rules load; only the change is drawn"


def test_it_is_redrawn_when_the_rules_are():
    """The strategy's config is what these boxes report, so they follow the payload that carries
    it rather than a timer of their own."""
    load = APP_JS[APP_JS.index("async function loadRules()") :][:1400]
    assert "renderStrategyRisk()" in load
