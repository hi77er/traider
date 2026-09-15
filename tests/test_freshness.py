"""Can the running server be older than the gate it is supposed to enforce?

Yes, and it happened: a live switch armed trading on credentials the broker had
already revoked, because the process had loaded `trading_service.py` two seconds
before the fix landed on disk. Every test of the gate passed while the server was
running the previous version of it — the tests import the code fresh, the server
does not.

So the gate modules are watched by mtime and the trading payload carries the answer.
These tests run the comparison against temp files, because the point is the
comparison, not the two paths it happens to watch today.
"""

from __future__ import annotations

import os

from src.web.services import freshness


def _touch(path, when: float) -> None:
    path.write_text("x", encoding="utf-8")
    os.utime(path, (when, when))


def test_a_file_written_after_the_process_loaded_it_is_reported(tmp_path):
    gate = tmp_path / "gate.py"
    _touch(gate, 1000.0)
    loaded = {"gate.py": 1000.0}

    assert freshness.info(("gate.py",), tmp_path, loaded)["stale"] is False

    _touch(gate, 1002.0)  # e.g. the fix lands two seconds after uvicorn started
    got = freshness.info(("gate.py",), tmp_path, loaded)
    assert got["stale"] is True
    assert got["changed"] == ["gate.py"]
    assert "gate.py" in got["message"] and "Restart" in got["message"]


def test_an_older_or_identical_file_is_not_stale(tmp_path):
    """Clock skew, a restored backup, a `git checkout` of the same revision — none of
    them mean the process is running different code."""
    gate = tmp_path / "gate.py"
    _touch(gate, 900.0)
    assert freshness.info(("gate.py",), tmp_path, {"gate.py": 1000.0})["stale"] is False


def test_a_file_that_appeared_or_vanished_counts_as_changed(tmp_path):
    """A watch list can be edited too: a file it names now but did not before is a
    reason to restart, not a reason to crash."""
    assert freshness.info(("missing.py",), tmp_path, {})["changed"] == ["missing.py"]
    (tmp_path / "new.py").write_text("x", encoding="utf-8")
    assert freshness.info(("new.py",), tmp_path, {"new.py": None})["changed"] == ["new.py"]


def test_the_comparison_never_raises(tmp_path):
    """It runs on every trading payload, so an unreadable path must degrade to
    \"cannot tell\" rather than take the dashboard down."""
    got = freshness.info(("nope/deeper.py",), tmp_path, {})
    assert got["stale"] is True and got["watched"] == ["nope/deeper.py"]


def test_it_watches_the_gate_itself():
    """The list is the point: if someone moves the gate, this test asks them to
    re-point the watch rather than let it silently guard the wrong files."""
    watched = set(freshness.WATCHED)
    assert "src/execution/credentials.py" in watched
    assert "src/web/services/trading_service.py" in watched
    assert (freshness.ROOT / "src" / "execution" / "credentials.py").exists()
    assert (freshness.ROOT / "src" / "web" / "services" / "trading_service.py").exists()


def test_the_payload_carries_the_answer():
    """The UI can only warn about a stale gate if the server says so."""
    from fastapi.testclient import TestClient

    from src.web.app import app

    body = TestClient(app).get("/api/v1/trading").json()
    assert "freshness" in body
    assert body["freshness"]["stale"] is False
    assert body["freshness"]["message"] == ""


def test_the_switch_says_so_and_the_page_warns_once():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "fresh.stale && fresh.message" in js, "the tooltip carries it"
    assert "state.staleGateWarned" in js, "and the page says it once, not on every poll"
