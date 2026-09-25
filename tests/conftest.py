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


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch, tmp_path_factory):
    """No test may see a REAL ``data/auth.json`` — the lock must never leak into the suite.

    The gate reads the store at the path the settings name, and the settings are the machine's: if
    the portal's owner runs the CLI on this checkout, every test would start answering 401 and the
    failure would look like a broken endpoint. Pointing the gate at a throwaway directory makes
    that impossible; a test that wants the lock ON turns it on deliberately (see
    ``test_web/test_auth_gate.py``).
    """
    from src.config.settings import Settings
    from src.web import middleware

    store = tmp_path_factory.mktemp("auth-guard")
    settings = Settings(
        _env_file=None,
        data_dir=str(store),
        historical_data_dir=str(store / "historical"),
    )
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    yield


@pytest.fixture(autouse=True)
def _no_account_file(monkeypatch, tmp_path_factory):
    """No test may read or WRITE the real ``data/account/account.json``.

    That file is a BOOTSTRAP document: its path is fixed and CWD-relative (it holds ``DATA_DIR``,
    so it cannot live inside it) and it ignores the settings object a test passes in. A test that
    writes account settings therefore overwrites the operator's real file — which is where the
    Alpaca key pairs live. Autouse because the mistake is silent and destructive: it does not
    fail a test, it empties the credentials the bot trades with, and nothing notices until the
    next order.

    A test that needs account values writes them to this throwaway file, which is the same code
    path with the same callers — the redirection is the only difference.
    """
    from src.config import account as account_mod

    store = tmp_path_factory.mktemp("account-guard") / "account.json"
    monkeypatch.setattr(account_mod, "account_file_path", lambda settings: store)
    yield
