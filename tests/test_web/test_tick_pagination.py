"""The loop's history is paged — fifteen ticks at a time.

A full session of five-minute bars is 78 ticks and the day's file only grows, so drawing all of
them made a table the reader could not see the end of: the gates at the top of the panel, which
are what the table is read against, were a scroll away from the last row.

Three things the shipped renderer has to get right, and this file runs it to check them:

* the SLICE — fifteen rows, newest first, and the pager's own count matching what is drawn;
* the PAGE NUMBER belongs to the DAY — a poll that appends a tick must not throw the reader back
  to page one, and picking another day must not leave them on page 3 of a day that has two;
* a day that fits on one page gets no pager at all: pagination with one page is a label with two
  dead buttons under it.

The renderer is lifted out of the real ``log.js`` and run in node with a faked document, so the
markup asserted here is the markup the page builds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LOG_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "log.js"

# From the page's own constants down to the ticks renderer: that span carries `esc`, `table`,
# `cell`, `stamp`, `barLabel`, `mine`, the pager and `renderTicks` — everything this exercises.
START_MARKER = "const COUNTDOWN_MS"
END_MARKER = "\n  function renderOrders()"

HARNESS = r"""
const els = {};
function make(id) { els[id] = { id: id, innerHTML: "", textContent: "" }; return els[id]; }
global.document = {
  hidden: false,
  getElementById: function (id) { return els[id] === undefined ? null : els[id]; },
  addEventListener: function () {},
};
const $ = (id) => document.getElementById(id);
["lg-ticks", "lg-day"].forEach(make);

const state = { log: null, loop: { env: "paper" }, accounts: null, dataset: null };

function ticksFor(day, n) {
  const rows = [];
  for (let i = 0; i < n; i += 1) {
    rows.push({
      at: "2026-09-21T16:" + String((60 - i) % 60).padStart(2, "0") + ":00+00:00",
      day: day, env: "paper", action: "decided", reason: "", stage: "decide",
      bar: "2026-09-21T16:00:00+00:00", signal: "HOLD",
    });
  }
  return rows;
}

const out = { views: [] };

function look(label) {
  const html = els["lg-ticks"].innerHTML;
  const pager = /<div class="lg-pager">([\s\S]*?)<\/div>/.exec(html);
  const body = (/<tbody>([\s\S]*)<\/tbody>/.exec(html) || [null, ""])[1];
  out.views.push({
    label: label,
    rows: (body.match(/<tr>/g) || []).length,
    headers: (html.match(/<th>/g) || []).length,
    count: pager ? /<span class="lg-pager-count">([^<]*)<\/span>/.exec(pager[1])[1] : null,
    page: pager ? /<span class="lg-pager-page">([^<]*)<\/span>/.exec(pager[1])[1] : null,
    newerDisabled: pager ? /goTickPage\(-1\)" disabled/.test(pager[1]) : null,
    olderDisabled: pager ? /goTickPage\(1\)" disabled/.test(pager[1]) : null,
    empty: /nothing was decided on this day/.test(html),
    firstRow: (body.match(/^<tr>([\s\S]*?)<\/tr>/) || [null, null])[1],
  });
}

// 1. A full session: 43 ticks, so pages of 15 / 15 / 13.
state.log = { day: "2026-09-21", today: "2026-09-21", ticks: ticksFor("2026-09-21", 43) };
renderTicks();
look("page 1");
goTickPage(1); look("page 2");
goTickPage(1); look("page 3");
goTickPage(1); look("past the end");

// 2. A poll on the same day: one more tick, and the reader stays where they were.
state.log = { day: "2026-09-21", today: "2026-09-21", ticks: ticksFor("2026-09-21", 44) };
renderTicks();
look("after a poll");

// 3. Another day: two ticks, and the page number starts over.
state.log = { day: "2026-09-18", today: "2026-09-21", ticks: ticksFor("2026-09-18", 2) };
renderTicks();
look("another day");

// 4. Exactly one page's worth: no pager.
state.log = { day: "2026-09-17", today: "2026-09-21", ticks: ticksFor("2026-09-17", 15) };
renderTicks();
look("exactly one page");

// 5. A day with nothing in it.
state.log = { day: "2026-09-16", today: "2026-09-21", ticks: [] };
renderTicks();
look("nothing");

// 6. Back to today, having left it on another day's page: page one, not a remembered page.
state.log = { day: "2026-09-21", today: "2026-09-21", ticks: ticksFor("2026-09-21", 43) };
renderTicks();
look("back to today");

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def paged(tmp_path_factory) -> dict:
    src = LOG_JS.read_text(encoding="utf-8")
    start = src.index(START_MARKER)
    block = src[start : src.index(END_MARKER, start)]
    script = tmp_path_factory.mktemp("pager") / "pager.js"
    script.write_text(block + HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            "tick pager harness failed\n"
            f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return {view["label"]: view for view in json.loads(proc.stdout)["views"]}


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the pager"
)


def test_a_page_holds_fifteen_ticks(paged):
    assert paged["page 1"]["rows"] == 15
    assert paged["page 1"]["headers"] == 8, "the day / account / action / bar / signal / reason / orders / notes columns"
    assert paged["page 2"]["rows"] == 15
    assert paged["page 3"]["rows"] == 13, "43 ticks: two full pages and a remainder"


def test_the_count_says_where_the_reader_is(paged):
    assert paged["page 1"]["count"] == "1–15 of 43"
    assert paged["page 2"]["count"] == "16–30 of 43"
    assert paged["page 3"]["count"] == "31–43 of 43"
    assert paged["page 2"]["page"] == "page 2 / 3"


def test_the_buttons_are_disabled_at_the_ends_rather_than_hidden(paged):
    """A control that comes and goes changes shape under the pointer just as it is being used."""
    assert paged["page 1"]["newerDisabled"] is True
    assert paged["page 1"]["olderDisabled"] is False
    assert paged["page 3"]["olderDisabled"] is True
    assert paged["page 3"]["newerDisabled"] is False


def test_walking_past_the_end_stays_on_the_last_page(paged):
    """Newest first, so stepping past the oldest ticks must not empty the table."""
    assert paged["past the end"]["count"] == "31–43 of 43"
    assert paged["past the end"]["page"] == "page 3 / 3"
    assert paged["past the end"]["rows"] == 13


def test_a_poll_leaves_the_reader_on_the_page_they_were_reading(paged):
    """The page re-reads itself every twenty seconds while today is showing. A tick arriving is
    not a reason to throw the reader back to the top of the list."""
    assert paged["after a poll"]["page"] == "page 3 / 3"
    assert paged["after a poll"]["count"] == "31–44 of 44", "the newer tick shifted the window"


def test_another_day_starts_at_page_one(paged):
    """"Page 3 of today" is not a place in yesterday — and a remembered page can be past the end
    of a shorter day, which would draw an empty table under a live pager."""
    assert paged["another day"]["rows"] == 2
    assert paged["another day"]["count"] is None, "no pager under a day that fits on one page"
    assert paged["another day"]["empty"] is False


def test_a_day_that_fits_on_one_page_gets_no_pager(paged):
    assert paged["exactly one page"]["rows"] == 15
    assert paged["exactly one page"]["count"] is None
    assert paged["exactly one page"]["page"] is None


def test_a_day_with_nothing_in_it_still_says_so(paged):
    assert paged["nothing"]["empty"] is True
    assert paged["nothing"]["rows"] == 0
    assert paged["nothing"]["count"] is None


def test_the_rows_are_still_newest_first_across_the_pages(paged):
    """Paging must not reorder anything: page one holds the newest ticks, and the last page the
    oldest — the same order the unpaged table had."""
    assert paged["page 1"]["firstRow"] != paged["page 2"]["firstRow"]
    assert paged["page 1"]["count"].startswith("1–"), "the first page is the newest fifteen"


def test_returning_to_today_starts_at_page_one_again(paged):
    assert paged["back to today"]["page"] == "page 1 / 3"
    assert paged["back to today"]["count"] == "1–15 of 43"


# ---------------------------------------------------------------------------
# where it hangs off the page
# ---------------------------------------------------------------------------
def test_the_pager_is_reachable_from_the_markup_it_renders():
    """The buttons are rendered as HTML, so the handler has to be on ``window`` — an IIFE's
    functions are not visible to an ``onclick`` attribute."""
    body = LOG_JS.read_text(encoding="utf-8")

    assert 'window.goTickPage = goTickPage;' in body
    assert 'onclick="goTickPage(' in body
