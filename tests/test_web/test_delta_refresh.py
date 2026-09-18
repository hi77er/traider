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
    """Without this the cache cannot know the file has grown."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "async function loadChart()")
    assert "state.datasetRows = d.rows" in body
    assert "state.chartRows" in body, "the cached copy must record its own size"


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
