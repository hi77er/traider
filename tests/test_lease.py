"""The lease: one loop per account, enforced rather than intended.

Most of these tests are about a judgement call this module makes and the module docstring
argues for: the pid outranks the timestamps on the host the lease was written on, and only
the timestamps can speak for a different host. Getting that order wrong is not a small
thing — it either starts a second loop or refuses a legitimate start, and both are quiet.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.config.settings import Settings
from src.scheduler import lease as lease_mod

#: No process can have this pid: Linux caps ``pid_max`` at 4194304 and macOS below that, so
#: ``os.kill`` raises ProcessLookupError for it on any platform this runs on. Used instead of
#: monkeypatching the check, so the real POSIX behaviour is what the test exercises.
DEAD_PID = 2 ** 30


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        historical_data_dir=str(tmp_path / "data" / "historical"),
    )


def _at(offset_seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


def _record(**fields) -> dict:
    now = _at(0)
    record = {
        "pid": DEAD_PID,
        "host": lease_mod._this_host(),
        "started": now.isoformat(),
        "heartbeat": now.isoformat(),
        "next_wake": _at(3600).isoformat(),
        "expires_at": _at(3900).isoformat(),
        "strategy": "Alpha",
    }
    record.update(fields)
    # Stamped the way the module writes them, so a test can pass a datetime for legibility
    # and still produce the exact text a real file holds.
    return {
        key: lease_mod._stamp(value) if isinstance(value, datetime) else value
        for key, value in record.items()
    }


def _write(settings, record) -> None:
    path = lease_mod.lease_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


# ---------------------------------------------------------------------------
# where it lives
# ---------------------------------------------------------------------------
def test_the_lease_sits_beside_the_other_runtime_state(tmp_path):
    """``data/loop.lock``, not inside a strategy's tree: there is one loop, not one each."""
    settings = _settings(tmp_path)
    path = lease_mod.lease_path(settings)

    assert path.name == "loop.lock"
    assert path.parent == (tmp_path / "data").resolve()


def test_a_free_loop_is_claimed_and_written_down(tmp_path):
    settings = _settings(tmp_path)

    claimed = lease_mod.acquire(settings, strategy="Alpha")

    record = lease_mod.read(settings)
    assert record["pid"] == claimed.pid
    assert record["pid"] == os.getpid()
    assert record["host"] == lease_mod._this_host()
    assert record["strategy"] == "Alpha"
    assert record["expires_at"], "the claim declares when it goes stale"
    assert lease_mod.holder(settings) is not None


# ---------------------------------------------------------------------------
# a live holder is respected, always
# ---------------------------------------------------------------------------
def test_a_second_start_is_refused_while_the_first_is_alive(tmp_path):
    settings = _settings(tmp_path)
    lease_mod.acquire(settings, strategy="Alpha")

    with pytest.raises(lease_mod.LeaseHeld) as caught:
        lease_mod.acquire(settings, strategy="Alpha")

    assert caught.value.record["pid"] == os.getpid()
    assert "already running" in str(caught.value)


def test_a_LIVE_pid_outranks_a_stale_timestamp(tmp_path):
    """The case the ordering exists for: a suspended laptop is alive but has not ticked.

    Judging by the timestamp alone would start a second loop on a machine whose first loop
    is merely asleep — which is the whole failure this module is here to prevent.
    """
    settings = _settings(tmp_path)
    _write(settings, _record(pid=os.getpid(), expires_at=_at(-86400)))

    assert lease_mod.holder(settings) is not None


def test_a_holder_on_another_host_is_trusted_until_its_expiry(tmp_path):
    settings = _settings(tmp_path)
    _write(settings, _record(host="some-other-box", expires_at=_at(600)))

    assert lease_mod.holder(settings) is not None

    _write(settings, _record(host="some-other-box", expires_at=_at(-1)))
    assert lease_mod.holder(settings) is None


# ---------------------------------------------------------------------------
# a crashed loop needs no manual cleanup
# ---------------------------------------------------------------------------
def test_a_dead_pid_is_taken_over_at_once(tmp_path):
    """A supervisor restarting the loop must not wait out a grace period sized for a sync."""
    settings = _settings(tmp_path)
    _write(settings, _record(pid=DEAD_PID, expires_at=_at(3600)))

    assert lease_mod.holder(settings) is None

    claimed = lease_mod.acquire(settings)
    assert lease_mod.read(settings)["pid"] == claimed.pid


def test_an_unreadable_lock_does_not_stop_a_start(tmp_path):
    """Otherwise a torn write needs a human at 09:30, which is what this design refuses."""
    settings = _settings(tmp_path)
    path = lease_mod.lease_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{ not json at all", encoding="utf-8")
    assert lease_mod.read(settings) is None
    assert lease_mod.acquire(settings).pid == os.getpid()

    path.write_text("[]", encoding="utf-8")
    assert lease_mod.read(settings) is None


def test_a_record_whose_expiry_cannot_be_read_counts_as_expired(tmp_path):
    """A lock nobody can interpret must not hold the loop hostage."""
    assert lease_mod.is_expired({}) is True
    assert lease_mod.is_expired({"expires_at": "not a time"}) is True
    assert lease_mod.is_expired({"expires_at": _at(60).isoformat()}) is False


def test_a_naive_expiry_is_read_as_utc(tmp_path):
    """The form an older or hand-edited file may be in, rather than an unreadable one."""
    naive = (_at(600).replace(tzinfo=None)).isoformat()
    assert lease_mod.is_expired({"expires_at": naive}) is False


# ---------------------------------------------------------------------------
# saying when it will next do something
# ---------------------------------------------------------------------------
def test_refresh_declares_the_boundary_the_loop_is_sleeping_until(tmp_path):
    """What lets a reader tell "asleep until 14:30" from "died at 14:05"."""
    settings = _settings(tmp_path)
    claim = lease_mod.acquire(settings)
    boundary = _at(1800)

    lease_mod.refresh(claim, next_wake=boundary)

    record = lease_mod.read(settings)
    assert record["next_wake"] == boundary.astimezone(timezone.utc).isoformat()
    assert record["expires_at"] == (
        boundary + timedelta(seconds=lease_mod.LEASE_GRACE_SECONDS)
    ).astimezone(timezone.utc).isoformat()
    assert lease_mod.is_expired(record) is False
    assert lease_mod.holder(settings) is not None


def test_refresh_keeps_the_identity_of_the_holder(tmp_path):
    settings = _settings(tmp_path)
    claim = lease_mod.acquire(settings, strategy="Beta")

    record = lease_mod.refresh(claim)

    assert record["pid"] == claim.pid
    assert record["host"] == claim.host
    assert record["strategy"] == "Beta"


# ---------------------------------------------------------------------------
# giving it up
# ---------------------------------------------------------------------------
def test_release_removes_our_own_lease(tmp_path):
    settings = _settings(tmp_path)
    claim = lease_mod.acquire(settings)

    lease_mod.release(claim)

    assert lease_mod.read(settings) is None
    assert not lease_mod.lease_path(settings).exists()


def test_release_leaves_a_lease_that_is_no_longer_ours(tmp_path):
    """A takeover while this process was wedged hands the loop to someone still trading.

    Deleting the file here would leave that process with no claim at all, which is how a
    third one gets started.
    """
    settings = _settings(tmp_path)
    claim = lease_mod.acquire(settings)
    _write(settings, _record(pid=DEAD_PID + 1))

    lease_mod.release(claim)

    assert lease_mod.read(settings)["pid"] == DEAD_PID + 1


def test_describe_names_the_holder(tmp_path):
    assert lease_mod.describe(None) == "no loop"
    text = lease_mod.describe(_record())
    assert "pid" in text and "Alpha" in text
