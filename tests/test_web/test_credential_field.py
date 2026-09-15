"""Is there a credential saved? The form has to answer that at a glance.

A stored secret used to arrive as the field's *placeholder* with an empty value, so
the box looked exactly like an empty one — "the form for paper trading credentials is
still empty" after a pair had been saved, verified, and was in force. The mask is now
the field's value: what the server sent is visibly in the box, and submitting the mask
back already means "unchanged" on every write path, so nothing about the behaviour
changed.

The real `fieldInput` is lifted out of `app.js` and run under node against a small
fake DOM, because the difference between a value and a placeholder is not something a
string assertion can see.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

START_MARKER = "function fieldInput("
END_MARKER = "\n/* ---------- Alpaca credential verification"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the field"
)

HARNESS = r"""
function el(tag) {
  return {
    tagName: tag, children: [], dataset: {}, style: {}, classList: {
      _c: [],
      add(c) { this._c.push(c); },
      toggle(c, on) { if (on) this._c.push(c); },
      contains(c) { return this._c.indexOf(c) >= 0; },
    },
    hidden: false, className: "", textContent: "", title: "", type: "", id: "", name: "",
    htmlFor: "", value: "", placeholder: "", onclick: null,
    appendChild(c) { this.children.push(c); return c; },
  };
}
const document = { createElement: el, getElementById: () => null };
const $ = () => null;
const escapeHtml = (s) => String(s == null ? "" : s);

// The input a field rendered, whatever else came with it.
function controlOf(field, prefix) {
  const wrap = fieldInput(field, prefix);
  const control = wrap.children.find((c) => c.tagName === "input");
  const label = wrap.children.find((c) => c.tagName === "label");
  return {
    value: control.value,
    placeholder: control.placeholder,
    type: control.type,
    id: control.id,
    labelSecret: label.classList.contains("secret"),
  };
}

const stored = {
  key: "ALPACA_PAPER_API_KEY", label: "Alpaca paper API key", type: "str",
  value: "********", default_value: "********", set: true, sensitive: true,
  readonly: false, readonly_note: null, description: "", options: null, hints: [],
};
const unset = { ...stored, key: "ALPACA_LIVE_API_KEY", value: "", default_value: "", set: false };
const plain = {
  key: "DATA_DIR", label: "Data folder", type: "str", value: "data",
  set: true, sensitive: false, readonly: false, readonly_note: null,
  description: "", options: null, hints: [],
};

process.stdout.write(JSON.stringify({
  stored: controlOf(stored, "acct"),
  unset: controlOf(unset, "acct"),
  plain: controlOf(plain, "acct"),
}));
"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    block = src[start:src.index(END_MARKER, start)]
    script = tmp_path_factory.mktemp("field") / "field.js"
    script.write_text(block + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "field harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_a_stored_secret_is_visible_in_the_field(rendered):
    """The reported bug: a saved, verified pair looked like an empty box."""
    got = rendered["stored"]
    assert got["value"] == "********", "the mask IS the value, not a ghost behind it"
    assert got["type"] == "password", "still not the secret itself"
    assert got["labelSecret"] is True


def test_a_missing_secret_says_so_without_pretending(rendered):
    got = rendered["unset"]
    assert got["value"] == ""
    assert got["placeholder"] == "(unset)"


def test_ordinary_fields_are_untouched(rendered):
    got = rendered["plain"]
    assert got["value"] == "data"
    assert got["placeholder"] == ""
    assert got["labelSecret"] is False


def test_the_field_is_still_named_for_its_form(rendered):
    assert rendered["stored"]["id"] == "acct-ALPACA_PAPER_API_KEY"
