"""Shared pytest fixtures / path setup for the TRAIDER test suite."""

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable (src package).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _offline_broker(monkeypatch):
    """No test may reach the broker, and no cached account read crosses tests.

    ``src.execution.positions.probe`` is the single door to ``GET /v2/positions`` for the
    exposure gates, and it is patched here rather than in the tests that need it, because
    getting this wrong is silent: a real request with dummy keys answers 401, and a 401 is
    indistinguishable from an empty account to everything downstream — so the accident
    would turn "a position is open" into "flat" and the test would pass for the wrong
    reason. Autouse means a future test cannot forget.

    A test that cares what an account holds patches this itself with a real answer; a test
    that does not gets the honest default for a suite with no broker configured.
    """
    from src.execution import positions

    positions.forget()
    monkeypatch.setattr(positions, "probe", lambda viewed, env: [])
    yield
    positions.forget()


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    """No test may make an HTTP request — at all, to anywhere.

    The suite's other guards are doors (``credentials.probe``, ``positions.probe``), and a
    door is only as good as the list of doors: the exposure gates added one, and a missed
    one does not fail loudly. It returns 401, which every caller reads as "empty", so a real
    request in a test looks like a pass with the wrong reason behind it.

    This closes the transport instead, so the class of mistake cannot recur whatever adds
    the next door.
    """
    import requests

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to make an HTTP request — the suite must never reach the network"
        )

    monkeypatch.setattr(requests.Session, "request", refuse, raising=False)
