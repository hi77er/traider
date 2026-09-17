"""Behavioural tests for the historical period depending on the bar size.

A window is only useful if the bar size can fill it: a finer candle cannot reach
as far back, because the data provider stops serving intraday bars after a short
trailing window — and how short depends on the provider answering. So the period
is offered as a function of the bar size AND the configured provider: with the
default yfinance, 1-minute bars reach 7 days (not the 15/30 the static table
lists), hourly bars 1–2 years, daily bars 2–5 years, and the pair must be chosen
in that order.

The server sends the list for the current bar size AND the whole table, and the
real blocks are lifted out of `app.js` and run under node so the assertions are
about what the dropdown offers, not about how the function is written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

# The two blocks under test, plus the option-row helper they render with.
LABEL_START = "function barSizeLabel("
LABEL_END = "function barWord("
WIRE_START = "function wireDependentOptions("
WIRE_END = "\n/* ---------- Risk Management panel"
OPT_START = "function _optHtml("
OPT_END = "// A condition compares its `feature`"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the period dropdown"
)

# The table the feature is: what the server offers for each bar size, for the
# configured provider (yfinance by default — see history.PROVIDER_MAX_DAYS).
PERIODS_BY_BAR_SIZE = {
    "1m": ["6d"],
    "2m": ["30d"],
    "5m": ["30d", "60d"],
    "15m": ["30d", "60d"],
    "1h": ["1y", "2y"],
    "2h": ["1y", "2y"],
    "4h": ["1y", "2y"],
    "8h": ["1y", "2y"],
    "12h": ["1y", "2y"],
    "1d": ["2y", "3y", "4y", "5y"],
}
BAR_LABELS = {
    "1m": "1 minute", "2m": "2 minutes", "5m": "5 minutes", "15m": "15 minutes",
    "1h": "1 hour", "2h": "2 hours", "4h": "4 hours", "8h": "8 hours",
    "12h": "12 hours", "1d": "1 day",
}


def extract(start_marker: str, end_marker: str) -> str:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index(start_marker)
    return src[start:src.index(end_marker, start)]


HARNESS = r"""
// ---- fakes: a <select> whose behaviour matters, and the document lookup -----
/* The point of the test is what a browser would SHOW, so the fake reproduces the
   two browser rules that matter: assigning innerHTML re-parses the options, and a
   select with nothing marked selected falls back to its first option (which is
   why a value outside the list can never simply vanish). */
function parseOptions(html) {
  const out = [];
  const re = /<option value="([^"]*)"([^>]*)>([^<]*)<\/option>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    out.push({ value: m[1], textContent: m[3], selected: m[2].indexOf("selected") > -1 });
  }
  return out;
}

function makeSelect(id, values, labels) {
  const el = {
    id: id,
    _options: [],
    listeners: {},
    get options() { return this._options; },
    get selectedIndex() {
      const i = this._options.findIndex((o) => o.selected);
      if (i > -1) return i;
      return this._options.length ? 0 : -1;
    },
    get value() {
      const i = this.selectedIndex;
      return i > -1 ? this._options[i].value : "";
    },
    set value(v) {
      let hit = false;
      this._options.forEach((o) => {
        o.selected = String(o.value) === String(v);
        if (o.selected) hit = true;
      });
      if (!hit && this._options.length) this._options[0].selected = true;
    },
    set innerHTML(html) {
      this._options = parseOptions(html);
      if (this._options.length && !this._options.some((o) => o.selected)) {
        this._options[0].selected = true;
      }
    },
    addEventListener: function (name, cb) { this.listeners[name] = cb; },
    change: function () { if (this.listeners.change) this.listeners.change(); },
  };
  if (values) el._options = values.map((v, i) => ({
    value: v, textContent: labels ? labels[i] : v, selected: i === 0,
  }));
  return el;
}

/* The dropdown itself is the whole message: what is allowed is exactly what it
   lists, so nothing is written beside it. The fake therefore has no place to put
   a note — if the block tried to write one it would have to go looking for it. */
const document = { getElementById: function (id) { return ELEMENTS[id] || null; } };
const window = {};

// A dependent field's schema: the list for the CURRENT bar size (what the server
// sends as `options`), plus the whole table, plus the fact that an unlisted bar
// size falls back to every period rather than to none.
// Derived from the table itself, so adding or capping a period cannot leave this
// list (and the test that uses it) asserting a window no bar size offers.
const ALL_OPTIONS = (function () {
  const seen = {};
  Object.keys(OPTIONS_BY_BAR_SIZE).forEach(function (bar) {
    OPTIONS_BY_BAR_SIZE[bar].forEach(function (o) { seen[o.value] = o; });
  });
  return Object.keys(seen).map(function (v) { return seen[v]; });
})();
const FIELD = {
  key: "HISTORICAL_LOOKBACK",
  depends_on: "HISTORICAL_BAR_SIZE",
  options: null,
  options_by: OPTIONS_BY_BAR_SIZE,
};
const GROUP = { name: "Instrument", fields: [FIELD] };

function barSelect(value) {
  const codes = Object.keys(BAR_LABELS);
  return makeSelect("scfg-HISTORICAL_BAR_SIZE", codes, codes.map((c) => BAR_LABELS[c]));
}

function wire(barValue, periodValue) {
  // Start from what the server would have sent for barValue: the options are
  // already that bar size's list, which is what makes the initial render correct
  // even before any change event.
  const serverOptions = OPTIONS_BY_BAR_SIZE[barValue] || ALL_OPTIONS;
  FIELD.options = serverOptions;
  const bar = barSelect(barValue);
  bar.value = barValue;
  const period = makeSelect("scfg-HISTORICAL_LOOKBACK");
  period._options = serverOptions.map((o) => ({
    value: o.value, textContent: o.label, selected: String(o.value) === String(periodValue),
  }));
  if (!period._options.some((o) => o.selected)) period._options[0].selected = true;
  if (periodValue !== undefined) period.value = periodValue;
  ELEMENTS["scfg-HISTORICAL_BAR_SIZE"] = bar;
  ELEMENTS["scfg-HISTORICAL_LOOKBACK"] = period;
  wireDependentOptions([GROUP], "scfg");
  return { bar: bar, period: period };
}

function plainValues(period) {
  return period.options.map((o) => o.value);
}
function plainLabels(period) {
  return period.options.map((o) => o.textContent);
}

const ELEMENTS = {};
const out = {};

// 1. INITIAL RENDER: the list offered is the current bar size's own.
let w = wire("1d", "3y");
out.daily = { values: plainValues(w.period), labels: plainLabels(w.period),
              selected: w.period.value };

// 2. CHANGING THE BAR SIZE repopulates the list locally.
w.bar.value = "1m";
w.bar.change();
out.afterMinute = { values: plainValues(w.period), selected: w.period.value,
                    labels: plainLabels(w.period) };

// 3. A VALUE THE NEW BAR SIZE CANNOT USE moves the selection onto the list: the
//    control is what shows it, so there is nothing to explain beside it.
w = wire("1d", "5y");
w.bar.value = "1h";
w.bar.change();
out.narrowed = { selected: w.period.value, values: plainValues(w.period),
                 labels: plainLabels(w.period) };

// 4. A VALUE THE NEW BAR SIZE STILL ALLOWS is left alone (4h -> 8h keeps 2 years).
w = wire("4h", "2y");
w.bar.value = "8h";
w.bar.change();
out.preserved = { selected: w.period.value };

// 5. EVERY BAR SIZE the dropdown offers can actually be chosen, with its own list.
out.eachBarSize = {};
Object.keys(BAR_LABELS).forEach(function (code) {
  const one = wire("1d", "2y");
  one.bar.value = code;
  one.bar.change();
  out.eachBarSize[code] = plainValues(one.period);
});

// 6. A BAR SIZE THE TABLE DOES NOT DESCRIBE falls back to the list the server sent
//    with the field (every period) rather than to an empty select — an empty select
//    would silently replace the operator's window with nothing. (A bar size the
//    dropdown does not offer cannot be SELECTED at all, which is why the stored
//    value is what the schema has to handle, not the change event.)
delete OPTIONS_BY_BAR_SIZE["1d"];
w = wire("1d", "3y");
out.undescribedBar = { values: plainValues(w.period), selected: w.period.value };

// 7. LABELS: a duration reads as a duration, in both directions ("2y" years,
//    "30d" days), a legacy bare number still reads as years, and a minute bar is
//    not spelled as a month.
out.labels = {
  period: ["2y", "1y", "30d", "1d", "5", "", null].map(function (p) {
    return p === "" ? "empty:" + periodLabel(p) : p === null ? "null:" + periodLabel(p) : periodLabel(p);
  }),
  bars: Object.keys(BAR_LABELS).map(function (c) { return barSizeLabel(c); }),
};

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def period_results(tmp_path_factory) -> dict:
    """Run the real period block under node and return what it offered."""
    script = tmp_path_factory.mktemp("periods") / "periods.js"
    options_by_bar_size = {
        bar: [{"label": p[:-1] + (" days" if p.endswith("d") else " years"), "value": p}
              for p in periods]
        for bar, periods in PERIODS_BY_BAR_SIZE.items()
    }
    script.write_text(
        f"const OPTIONS_BY_BAR_SIZE = {json.dumps(options_by_bar_size)};\n"
        f"const BAR_LABELS = {json.dumps(BAR_LABELS)};\n"
        "function escapeHtml(s) { return String(s); }\n"
        + extract(LABEL_START, LABEL_END)
        + extract(OPT_START, OPT_END)
        + extract(WIRE_START, WIRE_END)
        + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "period harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_the_periods_offered_are_the_bar_sizes_own(period_results):
    got = period_results["daily"]
    assert got["values"] == PERIODS_BY_BAR_SIZE["1d"]
    assert got["labels"] == ["2 years", "3 years", "4 years", "5 years"]
    assert got["selected"] == "3y", "a usable stored value stays selected"


def test_changing_the_bar_size_swaps_the_periods(period_results):
    """No round trip: the whole table travels with the field."""
    got = period_results["afterMinute"]
    assert got["values"] == ["6d"]
    assert got["labels"] == ["6 days"]
    assert got["selected"] == "6d"


def test_a_period_the_new_bar_size_cannot_use_moves_onto_the_list(period_results):
    """5 years of 1-hour bars is not a pair the rule allows, and the provider would
    not serve it either. The selection moves to the first allowed period, which is
    visible in the control — the list IS the statement of what is allowed."""
    got = period_results["narrowed"]
    assert got["values"] == ["1y", "2y"]
    assert got["selected"] == "1y"


def test_a_period_that_is_still_allowed_is_left_alone(period_results):
    assert period_results["preserved"]["selected"] == "2y"


def test_a_bar_size_the_table_does_not_describe_keeps_every_period(period_results):
    """The schema is the source of truth for a stored pair; the client's table is
    only there to swap the list when the operator changes the bar size. If the two
    ever disagree, the field's own list must win — an empty dropdown would show the
    operator nothing and save nothing."""
    got = period_results["undescribedBar"]
    assert sorted(got["values"]) == sorted(
        {p for periods in PERIODS_BY_BAR_SIZE.values() for p in periods}
    )
    assert got["selected"] == "3y", "and the stored value survives"


def test_every_offered_bar_size_has_its_own_period_list(period_results):
    assert period_results["eachBarSize"] == PERIODS_BY_BAR_SIZE


def test_a_minute_bar_is_not_spelled_as_a_month(period_results):
    """The provider's notation collides here: "1m" is one MINUTE, "1M" is one
    MONTH. The label has to come from the minute side."""
    labels = period_results["labels"]["bars"]
    assert labels[0] == "1 minute"
    assert "1 month" not in labels


def test_a_duration_is_labelled_with_its_unit(period_results):
    got = period_results["labels"]["period"]
    assert got[0] == "2 years"
    assert got[1] == "1 year", "singular"
    assert got[2] == "30 days"
    assert got[3] == "1 day", "singular"
    assert got[4] == "5 years", "a legacy bare number still reads as years"
    assert got[5] == "empty:—" and got[6] == "null:—"
