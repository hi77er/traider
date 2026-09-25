"""The time axis of every chart: a candle's label is the period that candle IS.

The library labels the time axis itself, and left alone it is wrong twice over here:

* **It does not know the bar size.** With ``timeVisible`` at its default the label is
  a DATE, so every candle in one session carries the same label and the axis says
  nothing about which minute or which hour a candle covers — the one thing that
  distinguishes a candle from its neighbour at maximum zoom.
* **It formats in UTC.** The bars are stamped in the exchange's own clock (09:30 means
  half past nine in New York) and handed over as an instant, so a UTC label renders the
  09:30 New York candle at 13:30 — a time that session never traded at, and four hours
  away from the number in the table beside the chart.

What is pinned here:

* the label is the bar's own period, taken from the bar's own stamp — consecutive bars
  get consecutive, distinct labels;
* it is read on the MARKET's clock, not UTC's and not the machine's;
* a calendar bar keeps a date and never grows a time of day (a daily candle's stamp is
  midnight, and "00:00" beside a day reads like a bar that traded at midnight);
* the bar-size taxonomy in the browser matches the server's, or one of the two would
  put a time on a daily axis;
* EVERY chart — the price chart, each indicator pane, the equity curve and the report
  page — builds its axis through this one module, so no two can disagree about what
  time a candle is.

The formatter is exercised by RUNNING the shipped ``chart_time.js`` in node, not by
searching it for strings: a grep would pass just as happily on a formatter that is
never wired to a chart.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from src.config.settings import Settings
from src.data.dataset import _CALENDAR_BARS, bar_label, chart_time
from src.web.services import dataset_service

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
CHART_TIME_JS = STATIC / "chart_time.js"
APP_JS = STATIC / "app.js"
REPORT_JS = STATIC / "report.js"

ET = "America/New_York"

# `tickMarkType`, as lightweight-charts numbers it.
YEAR, MONTH, DAY_OF_MONTH, TIME = 0, 1, 2, 3

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the time axis"
)


def _js(expr: str):
    """Evaluate ``expr`` against the shipped ``chart_time.js`` and return its JSON."""
    program = (
        "const fs = require('fs');"
        "eval(fs.readFileSync(" + json.dumps(str(CHART_TIME_JS)) + ", 'utf8'));"
        "console.log(JSON.stringify(" + expr + "));"
    )
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _axis_label(stamp: str, interval: str, tick_mark: int = TIME, tz: str = ET) -> str:
    """The label the axis shows for the bar stamped ``stamp``."""
    chart_key = chart_time(pd.Timestamp(stamp), interval, tz)
    return _js(
        "ChartTime.tickLabel("
        + json.dumps(chart_key)
        + f", {tick_mark}, "
        + json.dumps(tz)
        + ")"
    )


# ---------------------------------------------------------------------------
# the label is the bar's own period
# ---------------------------------------------------------------------------
def test_a_five_minute_candle_is_labelled_with_its_own_five_minutes():
    assert _axis_label("2026-09-17 09:30", "5m") == "09:30"
    assert _axis_label("2026-09-17 09:35", "5m") == "09:35"
    assert _axis_label("2026-09-17 15:55", "5m") == "15:55"


def test_consecutive_minute_candles_get_consecutive_distinct_labels():
    # The bug in one assertion: with time labels off, all six of these read the same.
    labels = [_axis_label(f"2026-09-17 09:3{m}", "1m") for m in range(6)]
    assert labels == ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35"]
    assert len(set(labels)) == 6


def test_an_hour_candle_is_labelled_with_the_hour_it_opens():
    assert _axis_label("2026-09-17 09:30", "1h") == "09:30"
    assert _axis_label("2026-09-17 10:30", "1h") == "10:30"


def test_the_axis_names_a_bar_exactly_as_the_data_table_does():
    # Both sides build from the same bar: the table's label is date + time, and the
    # axis label is that same time. They must not drift apart.
    shown = bar_label(pd.Timestamp("2026-09-17 09:35"), "5m")
    assert shown == "2026-09-17 09:35"
    assert _axis_label("2026-09-17 09:35", "5m") == shown.split(" ")[1]


# ---------------------------------------------------------------------------
# the market clock, not UTC and not the machine's
# ---------------------------------------------------------------------------
def test_a_time_stamp_is_read_on_the_market_s_clock_not_utc_s():
    seconds = chart_time(pd.Timestamp("2026-09-17 09:30"), "5m", ET)
    on_market_time = _js(
        f"ChartTime.tickLabel({seconds}, {TIME}, " + json.dumps(ET) + ")"
    )
    in_utc = _js(f"ChartTime.tickLabel({seconds}, {TIME}, " + json.dumps("UTC") + ")")
    assert on_market_time == "09:30"
    # September is EDT, so the market is four hours behind UTC: the naive UTC label
    # would put this candle at 13:30, mid-afternoon, hours after it actually opened.
    assert in_utc == "13:30"


def test_an_unrecognised_time_zone_still_labels_the_axis():
    # A bad setting must not blank every chart's axis.
    got = _js(f"ChartTime.tickLabel(1789652100, {TIME}, 'Nowhere/Nothing')")
    assert re.fullmatch(r"\d{2}:\d{2}", got), got


# ---------------------------------------------------------------------------
# calendar bars keep a date and nothing else
# ---------------------------------------------------------------------------
def test_a_daily_candle_is_labelled_with_its_date_and_no_time_of_day():
    key = chart_time(pd.Timestamp("2026-09-17"), "1d", ET)
    assert isinstance(key, str), "a calendar bar is keyed by its date alone"
    assert _js(f"ChartTime.tickLabel({json.dumps(key)}, {DAY_OF_MONTH}, " + json.dumps(ET) + ")") == "Sep 17"
    assert _js(f"ChartTime.tickLabel({json.dumps(key)}, {MONTH}, " + json.dumps(ET) + ")") == "Sep 2026"
    assert _js(f"ChartTime.tickLabel({json.dumps(key)}, {YEAR}, " + json.dumps(ET) + ")") == "2026"


def test_the_library_s_business_day_object_is_labelled_the_same_way():
    # Zoomed out, the library hands the formatter {year, month, day} rather than the
    # stored 'YYYY-MM-DD' string.
    expr = (
        "ChartTime.tickLabel({year: 2026, month: 9, day: 17}, "
        + str(DAY_OF_MONTH)
        + ", "
        + json.dumps(ET)
        + ")"
    )
    assert _js(expr) == "Sep 17"


def test_time_of_day_is_shown_for_intraday_bars_and_hidden_for_calendar_ones():
    for intraday in ("1m", "2m", "5m", "15m", "30m", "1h"):
        assert _js(f"ChartTime.timeScaleOptions('{intraday}', " + json.dumps(ET) + ").timeVisible") is True, intraday
    for calendar in _CALENDAR_BARS:
        assert _js(f"ChartTime.timeScaleOptions('{calendar}', " + json.dumps(ET) + ").timeVisible") is False, calendar


def test_the_bar_size_taxonomy_matches_the_server_s():
    # Two lists decide, one on each side, whether an axis may show a time of day.
    # They only have to disagree about one bar size to put a time on a daily axis.
    assert sorted(_js("ChartTime.CALENDAR_BARS")) == sorted(_CALENDAR_BARS)


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------
def test_the_chart_s_time_scale_calls_the_shared_formatter():
    seconds = chart_time(pd.Timestamp("2026-09-17 09:35"), "5m", ET)
    expr = (
        "ChartTime.timeScaleOptions('5m', "
        + json.dumps(ET)
        + f").tickMarkFormatter({seconds}, {TIME}, 'en-US')"
    )
    assert _js(expr) == "09:35"


def test_the_crosshair_names_the_bar_the_way_the_tables_do():
    seconds = chart_time(pd.Timestamp("2026-09-17 09:35"), "5m", ET)
    expr = (
        "ChartTime.localizationOptions("
        + json.dumps(ET)
        + f").timeFormatter({seconds})"
    )
    assert _js(expr) == bar_label(pd.Timestamp("2026-09-17 09:35"), "5m")

    # ...and a calendar bar keeps its date-only shape, with no invented time.
    daily = chart_time(pd.Timestamp("2026-09-17"), "1d", ET)
    assert _js(
        "ChartTime.localizationOptions(" + json.dumps(ET) + ").timeFormatter(" + json.dumps(daily) + ")"
    ) == "2026-09-17"


def test_every_chart_builds_its_time_axis_through_the_shared_module():
    for path in (APP_JS, REPORT_JS):
        src = path.read_text(encoding="utf-8")
        charts = len(re.findall(r"LightweightCharts\.createChart\(", src))
        axes = len(re.findall(r"timeScale: axisTimeScale\(", src))
        crosshairs = len(re.findall(r"localization: axisLocalization\(\)", src))
        assert charts > 0, path.name
        assert axes == charts, f"{path.name}: {charts} charts, {axes} labelled axes"
        assert crosshairs == charts, f"{path.name}: {charts} charts, {crosshairs} crosshairs"


def test_no_chart_still_hardcodes_the_time_axis_off():
    for path in (APP_JS, REPORT_JS):
        assert "timeVisible: false" not in path.read_text(encoding="utf-8"), path.name


def test_the_crosshair_keeps_its_own_time_label():
    # The axis names each candle's PERIOD. The boxed label the crosshair draws on the
    # axis is a different thing: the exact instant under the pointer, including between
    # bars and in the whitespace past the last one. It was turned off once as "a second
    # copy of the same time" and asked straight back, so no chart may hide it.
    for path in (APP_JS, REPORT_JS):
        src = path.read_text(encoding="utf-8")
        assert len(re.findall(r"LightweightCharts\.createChart\(", src)) > 0, path.name
        assert "labelVisible: false" not in src, f"{path.name} hides a crosshair label"


def test_the_dataset_status_says_which_clock_its_bars_are_stamped_on(tmp_path):
    # The browser cannot guess the exchange's zone, so the payload carries it.
    settings = Settings(
        historical_data_dir=str(tmp_path),
        instrument="AAPL",
        historical_bar_size="1m",
        market_timezone="America/New_York",
    )
    assert dataset_service.dataset_status(settings)["market_timezone"] == "America/New_York"


# ---------------------------------------------------------------------------
# the chart's own options, as app.js builds them
# ---------------------------------------------------------------------------
def _axis_source() -> str:
    """The real ``axisZone``/``axisTimeScale``/``axisLocalization`` from ``app.js``."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("/* The exchange's time zone, as the dataset status reports it.")
    end = src.index("function buildMainChart() {")
    return src[start:end]


def _run_axis(status_json: str, expr: str):
    """Build the axis options for a dataset status, then evaluate ``expr`` against them."""
    program = (
        "const fs = require('fs');"
        "eval(fs.readFileSync(" + json.dumps(str(CHART_TIME_JS)) + ", 'utf8'));"
        "const warnings = [];"
        "console.warn = function (m) { warnings.push(m); };"
        "const state = { status: " + status_json + " };"
        + _axis_source()
        + "console.log(JSON.stringify(" + expr + "));"
    )
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_screen_s_time_zone_reaches_the_axis():
    # What the page does with the dataset status it was handed.
    seconds = chart_time(pd.Timestamp("2026-09-17 09:35"), "5m", ET)
    got = _run_axis(
        json.dumps({"interval": "5m", "market_timezone": ET}),
        "(function () { const o = axisTimeScale({ borderColor: 'x' });"
        f" return [o.borderColor, o.timeVisible, o.tickMarkFormatter({seconds}, {TIME}, 'en-US'),"
        " axisLocalization().timeFormatter(" + str(seconds) + "), warnings.length]; })()",
    )
    # the chart's own option survives the merge, and the labels are the market's clock
    assert got == ["x", True, "09:35", "2026-09-17 09:35", 0]


def test_a_status_without_a_time_zone_still_labels_bars_and_says_so():
    # A server started before the payload carried the field: bars still get their own
    # label (so the axis is not silently date-only), and the operator is told why the
    # clock reads UTC instead of the exchange's.
    seconds = chart_time(pd.Timestamp("2026-09-17 09:35"), "5m", ET)
    got = _run_axis(
        json.dumps({"interval": "5m"}),
        "(function () { const first = axisTimeScale({});"
        f" const label = first.tickMarkFormatter({seconds}, {TIME}, 'en-US');"
        " axisTimeScale({}); axisTimeScale({});"  # still exactly one complaint
        " return [first.timeVisible, label, warnings.length]; })()",
    )
    assert got == [True, "13:35", 1]


def test_a_daily_axis_stays_a_date_even_with_the_zone_known():
    key = chart_time(pd.Timestamp("2026-09-17"), "1d", ET)
    got = _run_axis(
        json.dumps({"interval": "1d", "market_timezone": ET}),
        "(function () { const o = axisTimeScale({});"
        f" return [o.timeVisible, o.tickMarkFormatter({json.dumps(key)}, {DAY_OF_MONTH}, 'en-US')]; }})()",
    )
    assert got == [False, "Sep 17"]


# ---------------------------------------------------------------------------
# the same label in a table cell
# ---------------------------------------------------------------------------
def test_a_bar_cell_names_the_market_s_time_and_its_zone():
    """The loop records a bar as a UTC stamp — seconds and offset included — and both are facts
    about how the record is STORED rather than about the bar: every stamp is on the minute, and
    "+00:00" is not a zone anyone trades in. A cell wants the time the market was at, named by
    the zone's abbreviation."""
    assert _js(
        "ChartTime.stampCell('2026-09-21T16:25:00+00:00', " + json.dumps(ET) + ")"
    ) == "12:25 EDT"
    assert _js(
        "ChartTime.stampCell('2026-09-21T16:25:00+00:00', 'UTC')"
    ) == "16:25 UTC"


def test_the_zone_abbreviation_follows_the_date_through_a_dst_change():
    """Why an abbreviation and not an offset: the same wall-clock hour is a different instant
    either side of the change, and EST/EDT says which one this bar was."""
    january = _js("ChartTime.stampCell('2026-01-15T15:00:00+00:00', " + json.dumps(ET) + ")")
    july = _js("ChartTime.stampCell('2026-07-15T15:00:00+00:00', " + json.dumps(ET) + ")")

    assert january == "10:00 EST"
    assert july == "11:00 EDT"


def test_a_cell_falls_back_to_whatever_the_stamp_says():
    """A bar whose stamp cannot be read is shown as it came: the thing that wrote it had a
    reason, and a cell that blanks it hides the record that explains the oddity."""
    assert _js("ChartTime.stampCell('whenever', " + json.dumps(ET) + ")") == "whenever"
    assert _js("ChartTime.stampCell(null, " + json.dumps(ET) + ")") == "—"
    assert _js("ChartTime.stampCell('', " + json.dumps(ET) + ")") == "—"
