"""The held-period band colour: four states, four colours.

The chart shades each round trip's held period by how it turned out, and the states
have to stay distinct because two of them were once conflated:

* **green** — the round trip made money,
* **red** — it lost,
* **grey** — it changed the equity by exactly nothing,
* **neutral blue** — no outcome recorded (an older payload).

Calling a zero a loss is what painted a profitable strategy entirely red and sent the
operator hunting for a charting bug: the trades were fine, the position SIZE was
zero. The colours are pinned by RUNNING the shipped ``shadeFor`` — extracted from
``app.js`` and executed in node — rather than by grepping for strings, which would
pass just as happily on a mis-wired branch.
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
    shutil.which("node") is None, reason="node is required to exercise the shade logic"
)

WIN = "rgba(38, 166, 154, 0.18)"
LOSS = "rgba(239, 83, 80, 0.18)"
FLAT = "rgba(158, 158, 158, 0.20)"
NEUTRAL = "rgba(110, 160, 255, 0.15)"


def _shade_source() -> str:
    """The four shade constants plus the real ``shadeFor`` body, verbatim."""
    src = APP_JS.read_text(encoding="utf-8")
    constants = "\n".join(
        m.group(0) for m in re.finditer(r'^const POSITION_SHADE\w* = "[^"]+";', src, re.M)
    )
    assert constants.count("const POSITION_SHADE") == 4, "one constant per state"
    start = src.index("  const shadeFor = (f) => {")
    end = src.index("\n  };", start) + len("\n  };")
    return constants + "\n" + src[start:end] + "\n"


def _run(expr: str):
    """Evaluate ``expr`` against the shipped shade source and return its JSON."""
    program = _shade_source() + "\nconsole.log(JSON.stringify(" + expr + "));\n"
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_four_states_have_four_distinct_colours():
    assert _run(
        "[POSITION_SHADE, POSITION_SHADE_WIN, POSITION_SHADE_LOSS, POSITION_SHADE_FLAT]"
    ) == [NEUTRAL, WIN, LOSS, FLAT]


def test_break_even_is_grey_and_differs_from_both_a_win_and_an_unknown():
    colours = _run(
        "[shadeFor({outcome:'win'}), shadeFor({outcome:'loss'}),"
        " shadeFor({outcome:'flat'}), shadeFor({})]"
    )
    win, loss, flat, neutral = colours
    assert win == WIN and loss == LOSS and neutral == NEUTRAL
    assert flat == FLAT
    assert len(set(colours)) == 4, "a zero must not read as a loss, or as unknown"
    # Grey really is grey — equal channels — so it reads as neither good nor bad.
    channels = [int(c) for c in re.findall(r"\d+", flat)[:3]]
    assert channels[0] == channels[1] == channels[2], flat


def test_an_older_payload_carrying_only_the_boolean_still_colours_correctly():
    """``win`` predates ``outcome``; a cached payload must not lose its colours."""
    assert _run("[shadeFor({win:true}), shadeFor({win:false})]") == [WIN, LOSS]


def test_a_fill_with_no_outcome_recorded_stays_neutral():
    assert _run("shadeFor({kind:'close'})") == NEUTRAL
    assert _run("shadeFor({win:null})") == NEUTRAL
