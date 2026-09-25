"""The volume read-out: the amount of the bar under the crosshair, over that bar's own column.

Shared by the dashboard's main chart and the log page's session chart, and pinned here by RUNNING
the shipped ``chart_volume.js`` rather than searching it for strings:

* the amount is written ABSOLUTE — ``58,460`` — and not compacted to ``58.46K`` the way the
  library's own volume format labels its scale: the number over a bar is the bar's value, not a
  scale label, and an amount that reads differently on another machine's locale is not the value;
* it is placed at the top of the bar (the y of the value on that series' scale) and nudged in at
  the edges of the pane, where a centred label would hang over the price scale;
* it CLEARS on all three of the ways a crosshair can stop naming a bar — a time with no bar under
  it, the crosshair leaving, the pointer walking off the pane — because a stale amount sitting
  over a bar nobody is pointing at reads as a current one;
* it reads only the series it was HANDED. A relative-volume bar is a ratio; printing one as an
  amount would be a lie the page told by itself, which is why nothing is read implicitly.

Two pages attach it, and each is checked for the half it could get wrong: the dashboard hands over
the absolute-volume histogram and NOT the relative-volume one, and neither page passes the other
page's element.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "web" / "static"
VOLUME_JS = STATIC / "chart_volume.js"
APP_JS = STATIC / "app.js"
LOG_JS = STATIC / "log.js"
INDEX_HTML = ROOT / "src" / "web" / "templates" / "index.html"
LOG_HTML = ROOT / "src" / "web" / "templates" / "log.html"
CSS = STATIC / "style.css"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the read-out"
)

HARNESS = r"""
const fs = require('fs');
eval(fs.readFileSync(VOLUME_JS_PATH, 'utf8'));

function element(id, offsetWidth) {
  return {
    id: id, hidden: false, textContent: '', style: {}, offsetWidth: offsetWidth,
    clientWidth: 800, bound: 0, onmouseleave: null,
    addEventListener: function (type, cb) { this.bound += 1; this.onmouseleave = cb; },
  };
}
function chart() {
  return { crosshair: null, subscribeCrosshairMove: function (cb) { this.crosshair = cb; } };
}
function series() {
  // The library's own y for a value: linear and deliberately obvious (one thousand units to the
  // pixel), so a test can name the pixel it expects.
  return { priceToCoordinate: function (value) { return 300 - Number(value) / 1000; } };
}

const out = { format: [] };
[0, 1000, 58460, 1234567, -2500, null, 12.6, 'nonsense'].forEach((v) => {
  out.format.push(ChartVolume.format(v));
});

const label = element('label', 40);
const host = element('host', 800);
const pane = chart();
const vol = series();
const readout = ChartVolume.attach({ chart: pane, series: vol, host: host, label: label });

const bar = (value) => new Map([[vol, { value: value }]]);
const look = (param) => {
  pane.crosshair(param);
  return {
    text: label.textContent, hidden: label.hidden,
    left: label.style.left, top: label.style.top,
  };
};

out.overABar = look({ time: 1, point: { x: 400, y: 250 }, seriesData: bar(50000) });
out.nearTheLeftEdge = look({ time: 1, point: { x: 1, y: 250 }, seriesData: bar(25000) });
out.nearTheRightEdge = look({ time: 1, point: { x: 795, y: 250 }, seriesData: bar(25000) });
// A crosshair over a bar of a DIFFERENT series (a ratio, an oscillator) must read nothing.
out.anotherSeries = look({ time: 1, point: { x: 400, y: 250 }, seriesData: new Map([['x', { value: 9 }]]) });
out.overNoBar = look({ time: 1, point: { x: 400, y: 250 }, seriesData: new Map() });
out.leftTheChart = look({ time: undefined, point: undefined, seriesData: new Map() });
// The pointer walking off the pane: the page's own listener, not a crosshair event.
pane.crosshair({ time: 1, point: { x: 400, y: 250 }, seriesData: bar(1234) });
host.onmouseleave();
out.pointerLeft = { hidden: label.hidden };
// The page putting it away because the series stopped being drawn.
pane.crosshair({ time: 1, point: { x: 400, y: 250 }, seriesData: bar(1234) });
readout.hide();
out.hiddenByThePage = { hidden: label.hidden };
// And what it refuses to wire at all.
out.incomplete = [
  ChartVolume.attach({ chart: pane, series: vol, host: host, label: null }),
  ChartVolume.attach({ chart: null, series: vol, host: host, label: label }),
  ChartVolume.attach(null),
];

// A page that rebuilds its chart attaches again on the same host: one leave listener, and it puts
// away the read-out that is CURRENT rather than the one it was first bound to.
const second = element('second', 40);
ChartVolume.attach({ chart: pane, series: vol, host: host, label: second });
pane.crosshair({ time: 1, point: { x: 400, y: 250 }, seriesData: bar(77) });
const shown = { text: second.textContent, hidden: second.hidden };
host.onmouseleave();
out.rebuilt = { listeners: host.bound, shown: shown, hiddenAfterLeave: second.hidden };

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def readout() -> dict:
    program = f"const VOLUME_JS_PATH = {json.dumps(str(VOLUME_JS))};\n" + HARNESS
    proc = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "volume read-out harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# the amount itself
# ---------------------------------------------------------------------------
def test_the_amount_is_written_absolute(readout):
    """Not ``58.46K``: the library's volume format labels a scale, where a compacted number is
    what fits. Over a bar it is the value that is wanted, and it has to be the same value on every
    machine — hence separators by hand rather than ``toLocaleString``."""
    assert readout["format"] == [
        "0", "1,000", "58,460", "1,234,567", "-2,500", "", "13", "",
    ]


# ---------------------------------------------------------------------------
# where it appears, and when it goes
# ---------------------------------------------------------------------------
def test_it_sits_at_the_top_of_the_bar_under_the_crosshair(readout):
    over = readout["overABar"]

    assert over["hidden"] is False
    assert over["text"] == "50,000"
    assert over["top"] == "250px", "the y of that value on the series' scale — the bar's top"
    assert over["left"] == "400px", "centred over the column the pointer is on"


def test_it_is_nudged_in_at_the_edges_of_the_pane(readout):
    """The label is centred on the bar, so at the first and last bar half of it would sit outside
    the pane — over the price scale, where it would be read as a scale label."""
    assert readout["nearTheLeftEdge"]["left"] == "22px", "half the label plus a gap, in from the left"
    assert readout["nearTheRightEdge"]["left"] == "778px", "and in from the right"


def test_it_clears_whenever_no_bar_is_under_the_pointer(readout):
    assert readout["overNoBar"]["hidden"] is True, "a time with no bar (a gap in the session)"
    assert readout["leftTheChart"]["hidden"] is True, "the crosshair leaving"
    assert readout["pointerLeft"]["hidden"] is True, "the pointer walking off the pane"


def test_it_reads_only_the_series_it_was_handed(readout):
    """The dashboard draws more than one histogram in that band. A ratio labelled as an amount is
    worse than no label, so a bar of any other series reads out nothing."""
    assert readout["anotherSeries"]["hidden"] is True


def test_the_page_can_put_it_away_and_get_null_back(readout):
    """``hide`` is for the page that stops drawing the series; ``attach`` returns null rather than
    wiring half of a read-out, so a chart that failed to load cannot break the draw."""
    assert readout["hiddenByThePage"]["hidden"] is True
    assert readout["incomplete"] == [None, None, None]


def test_a_rebuilt_chart_rebinds_the_read_out_without_stacking_listeners(readout):
    """Both pages rebuild their chart — a day change, a dataset reload — while the host element
    lives on. One leave listener is registered for its lifetime, and it puts away the read-out
    that is current, not the one it was first bound to."""
    assert readout["rebuilt"]["listeners"] == 1
    assert readout["rebuilt"]["shown"] == {"text": "77", "hidden": False}
    assert readout["rebuilt"]["hiddenAfterLeave"] is True


# ---------------------------------------------------------------------------
# the two pages that attach it
# ---------------------------------------------------------------------------
def test_the_dashboard_labels_the_absolute_volume_bars_and_only_those():
    """The overlay it must read is ``volume_abs`` (the settings' absolute per-candle volume). The
    relative-volume overlay shares the band, and its bars are ratios."""
    body = APP_JS.read_text(encoding="utf-8")
    draw = body[body.index("function drawVolumeBars(chart)"):]
    draw = draw[: draw.index("\n}\n")]

    assert 'o.key === "volume_abs"' in draw, "the absolute series is picked out of the overlays"
    assert "ChartVolume.attach({" in draw
    assert "series: volumeAbs," in draw, "and it is that series the read-out reads"
    assert 'label: $("chart-volume-label")' in draw
    # Every draw puts the old read-out away first: the series it was reading is being replaced,
    # and when the overlay is switched off there is nothing to replace it with.
    assert draw.index("state.volumeReadout = null") < draw.index("ChartVolume.attach({")
    assert "volumeReadout.hide()" in draw


def test_the_log_page_labels_its_own_volume_bars():
    """One read-out per page, and each names its own element — the log page's label is not the
    dashboard's."""
    body = LOG_JS.read_text(encoding="utf-8")
    build = body[body.index("function buildCharts()"):][:2600]

    assert "ChartVolume.attach({" in build
    assert "series: charts.volume," in build
    assert 'label: $("lg-vol-label")' in build


def test_both_pages_have_the_frame_the_label_is_positioned_in():
    """The frame is what makes the absolute position meaningful, and the label is a SIBLING of the
    chart host: the library owns that element's contents and has taken back everything put there."""
    css = CSS.read_text(encoding="utf-8")
    assert ".chart-volume-frame { position: relative; }" in css
    label_rule = css[css.index(".chart-volume-label {"):][:800]
    assert "pointer-events: none;" in label_rule, "or it steals the hover that opened it"
    assert "translate(-50%, calc(-100% - 6px))" in label_rule, "centred, and above the bar"
    assert "position: absolute;" in label_rule

    for path, host, label in (
        (INDEX_HTML, 'id="chart-canvas"', 'id="chart-volume-label"'),
        (LOG_HTML, 'id="lg-chart"', 'id="lg-vol-label"'),
    ):
        html = path.read_text(encoding="utf-8")
        frame = html[html.index('class="chart-volume-frame"'):]
        frame = frame[: frame.index("</div>\n", frame.index(label))]
        assert host in frame, f"{path.name}: the chart host is inside the frame"
        assert f'class="chart-volume-label" {label} hidden' in frame, f"{path.name}: {label}"


def test_both_pages_load_the_shared_module_before_the_page_script():
    """One implementation, wired twice. A page that loaded it after its own script would attach
    nothing at all, silently."""
    for path in (INDEX_HTML, LOG_HTML):
        html = path.read_text(encoding="utf-8")
        order = [
            html.index("/static/chart_volume.js"),
            html.index("/static/app.js") if path is INDEX_HTML else html.index("/static/log.js"),
        ]
        assert order == sorted(order), f"{path.name}: the module loads first"


def test_the_read_out_has_exactly_one_implementation():
    """The two pages draw the same band under the same crosshair. The subtle parts — the edge
    nudge, the three ways to clear it, the leave listener — are not worth getting right twice."""
    assert 'subscribeCrosshairMove((param)' not in LOG_JS.read_text(encoding="utf-8").split(
        "function buildCharts"
    )[-1]
    app = APP_JS.read_text(encoding="utf-8")
    assert "priceToCoordinate" not in app, "the pages place the label; the module does"
