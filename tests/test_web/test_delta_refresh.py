"""A fetch that changes the dataset must reload what is drawn from it.

Everything on the dashboard that is drawn from the bars — the price chart, its
indicator panes, the data table, the signal series, the position bands — is read once,
when the page loads. So after a fetch that writes anything, all of it is stale and has
to be redrawn.

The condition used to be "did the sync come back `synced`", which missed the common
case: a fetch that pulls in today's bars while an older gap remains. Only the provider
can settle a gap and it often cannot (an absent day is usually a holiday, and a bar the
provider never served cannot be conjured), so a successful fetch routinely returns
`synced: false` — and the chart kept showing the old window with no sign that anything
had been added.

Pinned by running the shipped predicate out of ``app.js`` in node, so a rewrite that
reintroduces the `synced` gate cannot pass.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the refresh rule"
)


def _predicate_source() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function fetchChangedDataset(before, after) {")
    end = src.index("\n}", start) + len("\n}")
    return src[start:end] + "\n"


def _run(expr: str):
    program = _predicate_source() + "\nconsole.log(JSON.stringify(" + expr + "));\n"
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_more_rows_reloads_the_chart():
    """The case the old `synced` gate missed: bars added, still not synced."""
    assert _run("fetchChangedDataset(3191, 3202)") is True


def test_nothing_written_does_not_reload():
    """No new bars means nothing is stale, and a reload would only cost requests."""
    assert _run("fetchChangedDataset(3191, 3191)") is False


def test_fewer_rows_reloads_too():
    """A rewrite that shortens the dataset is a change like any other."""
    assert _run("fetchChangedDataset(3191, 3000)") is True


def test_an_unknown_row_count_reloads():
    """No previous count (first load) or no count returned: reload, never assume."""
    assert _run("fetchChangedDataset(null, 3191)") is True
    assert _run("fetchChangedDataset(3191, null)") is True
    assert _run("fetchChangedDataset(undefined, undefined)") is True


def test_the_sync_no_longer_gates_the_reload_on_synced():
    """Source-level guard: the reload must not be conditioned on `synced` again."""
    src = APP_JS.read_text(encoding="utf-8")
    body = _function_body(src, "async function syncDelta()")
    assert "fetchChangedDataset(" in body
    assert "if (s.synced) refresh()" not in body, (
        "gating the reload on `synced` is the bug this pins"
    )


# ---------------------------------------------------------------------------
# the chart's cached copy of the bars
# ---------------------------------------------------------------------------
def _function_body(src: str, header: str) -> str:
    start = src.index(header)
    return src[start:src.index("\n}", start)]


def test_load_chart_records_the_size_of_the_copy_it_caches():
    """Without this the cache cannot know the file has grown.

    The recorded size is the FILE's own count (``total``, what ``/dataset/status``
    reports) rather than the length of the payload: the chart's bars include a filled
    bar for every session slot nobody traded in, and comparing that against the status
    endpoint's count would reload the page on every poll, forever.
    """
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "async function loadChart()")
    assert "state.datasetRows = d.rows" in body
    assert "state.chartRows" in body, "the cached copy must record its own size"
    assert "d.total" in body, "counted from the file, not from the filled payload"


def test_a_row_count_change_drops_the_cached_copy():
    """The reload has to refetch the series, not rebuild from the copy it already has.

    `loadChart` fetches `limit=0` only when `state.datasetRows` is null, so a cache that
    is never dropped means the chart is rebuilt from the OLD bars no matter how many
    times the page refreshes — which is how a fetch updated the summary, the table and
    the delta column while the chart stayed exactly where it was.
    """
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "function render(s)")
    assert "forgetDatasetRows()" in body, "render must drop the copy when it is stale"
    assert "chartRows" in body, "and it must decide that from the recorded size"


def test_forget_dataset_rows_clears_both_the_rows_and_their_size():
    """Clearing one without the other would leave a size that matches nothing."""
    body = _function_body(
        APP_JS.read_text(encoding="utf-8"), "function forgetDatasetRows()"
    )
    assert "state.datasetRows = null" in body
    assert "state.chartRows = null" in body


def test_switching_instrument_also_drops_the_copy():
    """A different instrument's bars are not this chart's copy."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "function switchDataset(symbol)")
    assert "forgetDatasetRows()" in body


# ---------------------------------------------------------------------------
# bars written while the page is open
# ---------------------------------------------------------------------------
# The reload above only happens when a render comes round, and a page whose data was taken
# before the open sits on it: the dashboard reads the dataset ONCE. Meanwhile the LOOP writes
# today's bars into the same file on every tick it runs — syncing the dataset is one of the
# steps of a tick — so the chart, the table, the summary and the delta panel can all be a
# session behind the file they describe, and the reader is shown a chart from Friday next to a
# delta panel saying there is nothing to fetch.
def _freshness_source() -> str:
    """The shipped row-count rule, plus the guard on what it triggers."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function datasetGrew(rows) {")
    end = src.index("\nasync function watchDatasetRows()")
    return src[start:end] + "\n"


def _run_freshness(body: str):
    program = (
        "const state = { chartRows: 3269 };\n"
        "let reloads = 0;\n"
        "async function refresh() { reloads += 1; }\n"
        "let clock = 1000000;\n"
        "Date.now = () => clock;\n"
        + _freshness_source()
        + "\n"
        + body
        + "\n"
    )
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_row_count_that_moved_counts_as_the_file_moving_on():
    """The rule the page reloads on, run out of the file rather than restated here."""
    assert _run_freshness(
        "console.log(JSON.stringify([datasetGrew(3300), datasetGrew(3269), datasetGrew(3000)]));"
    ) == [True, False, True]


def test_no_cached_copy_is_not_a_dataset_that_grew():
    """Before the first chart there is nothing to be stale, and reloading for it would loop:
    the reload is what loads the chart, and `chartRows` is only set once it has."""
    assert _run_freshness(
        "state.chartRows = null;\nconsole.log(JSON.stringify(datasetGrew(3300)));"
    ) is False


def test_a_status_without_a_row_count_is_not_grounds_to_reload():
    """The status payload is the only thing that knows, so if it does not say, do nothing."""
    assert _run_freshness(
        "console.log(JSON.stringify([datasetGrew(null), datasetGrew(undefined), datasetGrew('3300')]));"
    ) == [False, False, False]


def test_the_same_move_of_the_file_reloads_ONE_page():
    """Two reads decide this — the row count and then the bars — and they are taken moments
    apart, so a write landing between them would otherwise reload twice; a file being written
    continuously would reload for as long as it was written. The guard is what makes the fix
    cost one page load per move of the file rather than an open-ended chain of them.
    """
    result = _run_freshness(
        "(async () => {"
        " const first = await reloadForDatasetGrew(3300);"
        " const again = await reloadForDatasetGrew(3301);"
        " const unchanged = await reloadForDatasetGrew(3269);"
        " clock += 60 * 60 * 1000;"   # an hour on: no sane window still holds
        " const later = await reloadForDatasetGrew(3302);"
        " console.log(JSON.stringify({ first, again, unchanged, later, reloads }));"
        " })();"
    )
    assert result == {
        "first": True,
        "again": False,
        "unchanged": False,
        "later": True,
        "reloads": 2,
    }


def test_the_page_looks_at_the_dataset_on_its_own_slow_cadence():
    """Where the look is wired in. The lab has no broker poll any more — the panel that carried it
    lives on the Session monitor — so the dataset watcher has a cadence of its own, and the
    question it has to answer is the same one: a page left open must notice that the loop wrote
    new bars under it.
    """
    src = APP_JS.read_text(encoding="utf-8")

    watch = _function_body(src, "async function watchDatasetRows()")
    assert '"/api/v1/dataset/status"' in watch, "a local file read, never a provider call"
    assert "reloadForDatasetGrew(s.rows)" in watch
    assert "cache" not in watch.lower(), "and it must not fetch bars itself"

    look = _function_body(src, "async function runDatasetWatch()")
    assert "await watchDatasetRows();" in look
    assert "scheduleDatasetWatch();" in look, "self-scheduling, so a slow read cannot stack up"

    schedule = _function_body(src, "function scheduleDatasetWatch(delay)")
    assert "DATASET_WATCH_MS" in schedule, "a slow cadence of its own"
    assert "document.hidden" in schedule, "and no look at all in a tab nobody is reading"
    assert '$("dashboard")' in schedule, "nor before there is anything drawn to keep current"

    # A backgrounded tab is the other moment a snapshot has had time to go stale: it stops asking,
    # and coming back re-reads at once instead of waiting out the interval.
    vis = _function_body(src, "function handleVisibility()")
    assert "watchDatasetRows()" in vis
    assert "stopDatasetWatch()" in vis and "scheduleDatasetWatch()" in vis

    # And it is armed at boot, or the page would only ever look once.
    assert "scheduleDatasetWatch();" in src[src.index("showPendingToast();") :]


def test_a_manual_recheck_redraws_what_the_panel_had_outgrown():
    """⬇ Re-check reads the file's real state for itself, so it is the one place a stale chart
    can be left standing beside a current panel — the reading that sent the operator looking for
    a data bug in the first place."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "async function loadDelta()")

    assert "reloadForDatasetGrew(s.rows)" in body
    assert body.index("renderDelta(s)") < body.index("reloadForDatasetGrew(s.rows)"), (
        "the panel reports the read either way; the redraw is what follows from it"
    )
