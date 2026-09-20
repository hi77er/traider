"""Guards for the API tests in this package.

The same spirit as ``tests/conftest.py``'s two autouse guards, one level down: those stop a test
reaching the BROKER, and this stops one reading the DEVELOPER'S MACHINE.

Every test here goes through ``client = TestClient(app)``, and the app resolves its settings from
``get_effective_settings_dep`` — which, with no override, reads the real ``data/`` directory. That
is invisible until it is not: the moment trading is armed on the machine (a live loop, a switch
left on while looking at the dashboard) the config lock answers ``409`` to every endpoint test
that expects ``200``, and six of them fail with `assert 409 == 200`. Nothing about the code
changed; the suite was reading the state of a machine.

So the default is a tmp data root, and a test that wants to exercise the lock against a real file
installs its own ``state_file`` — which is what ``test_trading_gate.py`` does with its own
override, and what every test that cares already had.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tmp_data_root(tmp_path):
    """Point the app's settings at a throwaway data directory for the duration of one test.

    Autouse and restored in a ``finally``-shaped teardown, because the failure it prevents is
    silent and depends on what somebody was doing on their laptop an hour earlier.
    """
    from src.config.effective import get_effective_settings_dep
    from src.config.settings import Settings
    from src.web.app import app

    previous = app.dependency_overrides.get(get_effective_settings_dep)
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        live_dir=str(tmp_path / "data" / "live_results"),
        instrument="AAPL",
        historical_bar_size="1h",
        market_timezone="America/New_York",
    )
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        yield settings
    finally:
        # A test that installed its own override owns the key now; put back what was there
        # rather than clearing it, so this cannot erase somebody else's wiring.
        if previous is None:
            app.dependency_overrides.pop(get_effective_settings_dep, None)
        else:
            app.dependency_overrides[get_effective_settings_dep] = previous
