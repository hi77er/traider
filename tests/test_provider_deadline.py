"""The data stack cannot park a tick.

On 2026-09-25 the loop woke for a bar, blocked on a single open socket inside the provider call,
and stayed there for the rest of the session: no tick written, no order, no error, and a pid that
kept the dashboard reporting "running". Nothing above the provider could cut it off — OpenBB
builds the request, yfinance decides how often to re-ask, and ``curl_cffi`` waits indefinitely
unless it is told otherwise (for curl_cffi ``timeout=None`` means "for ever").

So the bound is set twice, and both halves are pinned here: an explicit timeout on the sessions
the shims hand to the provider, and a deadline around the call — because making a CALLER stop
waiting is not the same thing as closing a socket, and the caller is the one that has a bar to
decide.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.config.settings import Settings
from src.data import openbb_session, screener
from src.data.deadline import bounded
from src.data.openbb_client import PROVIDER_DEADLINE_SECONDS, OpenBBClient, OpenBBError


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="AAPL",
        # The cache would answer before the provider is ever asked.
        data_cache_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


# A socket that never speaks, as the loop found it. The wait is short because the thread is
# abandoned: nothing in the test waits on it, and it is a daemon, so it cannot hold the suite up.
_NEVER = threading.Event()


def _never_answers(*_args, **_kwargs):
    _NEVER.wait(5)
    return None


class _HangingProvider:
    """OpenBB's attribute tree, with a fetch and a quote that never return."""

    class equity:  # noqa: N801 - stands in for OpenBB's own namespace
        class price:
            historical = staticmethod(_never_answers)
            quote = staticmethod(_never_answers)


def _returns(value):
    def _call():
        return value

    return _call


def test_a_call_that_never_answers_gives_up_at_the_deadline():
    started = time.monotonic()

    with pytest.raises(TimeoutError) as caught:
        bounded(lambda: _never_answers(), 0.05, what="the provider")

    assert "did not answer within" in str(caught.value)
    assert time.monotonic() - started < 5, "the deadline ends the wait, not the call"


def test_the_callers_own_failure_comes_back_unchanged():
    """The deadline must not change what a caller sees when the provider DOES answer — or when
    it fails on its own terms, which the tick's own error handling is written against."""
    def broken():
        raise ValueError("the provider said something unusable")

    with pytest.raises(ValueError):
        bounded(broken, 1.0, what="the provider")


def test_an_answer_that_arrives_is_returned_as_it_is():
    assert bounded(_returns({"close": 101.5}), 1.0, what="the provider") == {"close": 101.5}


def test_a_hanging_provider_fails_the_fetch_instead_of_holding_the_tick(tmp_path):
    """The whole point: a dead socket becomes a failed fetch the tick can report and move past,
    rather than a loop that never ticks again."""
    client = OpenBBClient(_settings(tmp_path), deadline_seconds=0.05)
    client._obb = _HangingProvider()

    started = time.monotonic()
    with pytest.raises(OpenBBError) as caught:
        client.fetch_historical("AAPL", "2026-09-01", "2026-09-25", interval="1d")

    assert time.monotonic() - started < 5
    assert "did not answer" in str(caught.value)


def test_a_hanging_quote_fails_rather_than_holding_the_caller(tmp_path):
    client = OpenBBClient(_settings(tmp_path), deadline_seconds=0.05)
    client._obb = _HangingProvider()

    started = time.monotonic()
    with pytest.raises(OpenBBError) as caught:
        client.fetch_quote("AAPL")

    assert time.monotonic() - started < 5
    assert "did not answer" in str(caught.value)


def test_the_sessions_handed_to_the_provider_cannot_wait_for_ever():
    """Both shims set the timeout themselves, because nothing above them does.

    Read from the source rather than run: applying either shim replaces a module-global factory
    for the rest of the process, and a test that left the real provider patched with a stub
    session would be a failure waiting for the next test that uses the provider.
    """
    for module, constant in ((openbb_session, "PROVIDER_TIMEOUT_SECONDS"),
                             (screener, "SCREENER_TIMEOUT_SECONDS")):
        timeout = getattr(module, constant)
        assert isinstance(timeout, tuple), constant
        assert all(float(value) > 0 for value in timeout), constant
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert f"timeout={constant}" in source, f"{constant} never reaches the session"


def test_the_deadline_fits_inside_the_bar_the_loop_trades():
    """A bound is only useful if it fits the schedule the loop keeps: a call allowed to run
    longer than the bar interval is a loop that is late before it has decided anything."""
    assert 0 < PROVIDER_DEADLINE_SECONDS <= 120
    assert 0 < screener.SCREEN_DEADLINE_SECONDS <= PROVIDER_DEADLINE_SECONDS


def test_the_deadline_is_settable_so_a_test_can_make_it_small(tmp_path):
    """It is a parameter of the client, not a constant read at the call site — otherwise this
    file would have to wait out the real one."""
    client = OpenBBClient(_settings(tmp_path), deadline_seconds=3)

    assert client.deadline_seconds == 3
    assert OpenBBClient(_settings(tmp_path)).deadline_seconds == PROVIDER_DEADLINE_SECONDS
